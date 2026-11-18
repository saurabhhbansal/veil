"""IMAP mailbox reader for account runs: find a person's mail server from their address, parse a raw
message, pull one-time codes and confirmation links out of it, and poll the inbox for them.

The parsing side (:func:`provider_for`, :func:`mailbox_for`, :func:`message_text`,
:func:`extract_code`, :func:`extract_link`) is pure and unit-testable without network.  The
:class:`MailReader` is the blocking IMAP client: callers run it in a worker thread.  It never marks
mail as read and never logs a password or an extracted code.
"""
from __future__ import annotations

import imaplib
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timezone
from email import message_from_bytes
from email.header import decode_header
from email.utils import parsedate_to_datetime
from html import unescape
from typing import Any

log = logging.getLogger(__name__)

WAIT_SECONDS = 90  # how long a caller waits for a code/link before asking the person
POLL_SECONDS = 5  # between IMAP polls
SKEW_SECONDS = 120  # mail servers and this PC disagree a little; a code sent just before `since` counts
FETCH_LIMIT = 20  # newest messages inspected per poll
HEADER_CHARS = 400  # a rendered message starts with Subject/From; a site mention there is a strong hint

__all__ = [
	'Mailbox',
	'MailReader',
	'POLL_SECONDS',
	'WAIT_SECONDS',
	'extract_code',
	'extract_link',
	'mailbox_for',
	'message_text',
	'provider_for',
]


@dataclass(frozen=True)
class Mailbox:
	"""An IMAP server for one address: the provider it belongs to, the host, and the TLS port."""

	provider: str  # gmail | yahoo | outlook | icloud | custom
	host: str
	port: int = 993


# --- which server serves this address ----------------------------------------------------------

_PROVIDER_HOSTS: dict[str, tuple[str, int]] = {
	'gmail': ('imap.gmail.com', 993),
	'yahoo': ('imap.mail.yahoo.com', 993),
	'outlook': ('outlook.office365.com', 993),
	'icloud': ('imap.mail.me.com', 993),
}

_DOMAIN_RE = re.compile(r'^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*$')
_HOST_RE = re.compile(r'^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*$')
_IPV6_RE = re.compile(r'^[0-9A-Fa-f:.]+$')


def _parse_address(value: str) -> tuple[str, str]:
	"""``(local_part, domain)`` of an address or a ``Name <address>`` header; ``('', '')`` when it is neither."""
	text = (value or '').strip()
	if '<' in text and '>' in text:
		text = text[text.rfind('<') + 1:text.rfind('>')].strip()
	if '@' not in text:
		return '', ''
	local, _, domain = text.rpartition('@')
	local, domain = local.strip(), domain.strip().lower().rstrip('.')
	if not local or not _DOMAIN_RE.match(domain):
		return '', ''
	return local, domain


def provider_for(email: str) -> str:
	"""The mail provider behind an address: gmail | yahoo | outlook | icloud | custom | unknown.

	'custom' means a valid address whose domain is not one of the free providers (the caller must
	supply its IMAP host to :func:`mailbox_for`); 'unknown' means it does not look like an address.
	"""
	_, domain = _parse_address(email)
	if not domain:
		return 'unknown'
	if domain in {'gmail.com', 'googlemail.com'}:
		return 'gmail'
	if domain == 'yahoo.com' or domain.startswith('yahoo.'):
		return 'yahoo'
	if domain in {'ymail.com', 'rocketmail.com'}:
		return 'yahoo'
	if domain.startswith(('outlook.', 'hotmail.', 'live.')) or domain == 'msn.com':
		return 'outlook'
	if domain in {'icloud.com', 'me.com', 'mac.com'}:
		return 'icloud'
	return 'custom'


def _coerce_port(value: int | str | None) -> int | None:
	"""A usable TCP port, or None."""
	if isinstance(value, bool):
		return None
	if isinstance(value, int):
		port = value
	elif isinstance(value, str) and value.strip().isdigit():
		port = int(value.strip())
	else:
		return None
	return port if 1 <= port <= 65535 else None


def _parse_imap(value: str) -> tuple[str | None, int | None]:
	"""``host:port`` (port optional, IPv6 in brackets) -> ``(host, port)``; ``(None, None)`` when empty.

	Raises ValueError for something that is not an IMAP host, so the caller can refuse it politely.
	"""
	text = (value or '').strip()
	if not text:
		return None, None
	if '://' in text or any(char in text for char in ' /@\\'):
		raise ValueError(f'not an IMAP host: {value!r}')
	port: int | None = None
	if text.startswith('['):  # [2001:db8::1]:993
		close = text.find(']')
		if close < 0:
			raise ValueError(f'not an IMAP host: {value!r}')
		host = text[1:close]
		rest = text[close + 1:].strip()
		if rest:
			if not rest.startswith(':'):
				raise ValueError(f'not an IMAP host: {value!r}')
			port = _coerce_port(rest[1:])
			if port is None:
				raise ValueError(f'bad IMAP port in {value!r}')
		if ':' not in host or not _IPV6_RE.match(host):
			raise ValueError(f'not an IMAP host: {value!r}')
		return host.lower(), port
	if ':' in text:
		host, _, port_text = text.rpartition(':')
		if not host:
			raise ValueError(f'not an IMAP host: {value!r}')
		port = _coerce_port(port_text)
		if port is None:
			raise ValueError(f'bad IMAP port in {value!r}')
	else:
		host = text
	if not _HOST_RE.match(host):
		raise ValueError(f'not an IMAP host: {value!r}')
	return host.lower(), port


def mailbox_for(email: str, imap: str | None = None, port: int | None = None) -> Mailbox | None:
	"""The IMAP server for an address, or None when it cannot be known.

	Known providers get their default host; ``imap`` (a host, optionally ``host:port``) overrides it.
	A custom domain needs ``imap``.  ``port`` overrides the port in either case.
	"""
	provider = provider_for(email)
	try:
		host, imap_port = _parse_imap(imap or '')
	except ValueError:
		return None
	forced_port = None
	if port is not None:
		forced_port = _coerce_port(port)
		if forced_port is None:
			return None
	if provider in _PROVIDER_HOSTS:
		default_host, default_port = _PROVIDER_HOSTS[provider]
		return Mailbox(provider, host or default_host, forced_port or imap_port or default_port)
	if provider == 'custom' and host:
		return Mailbox('custom', host, forced_port or imap_port or 993)
	return None


# --- raw message -> readable text --------------------------------------------------------------


def _decode_bytes(data: bytes, charset: str | None = None) -> str:
	"""Bytes to text, never raising: unknown or wrong charsets fall back to latin-1."""
	for candidate in ((charset or '').strip().strip('"').lower(), 'utf-8'):
		if not candidate:
			continue
		try:
			return data.decode(candidate)
		except (LookupError, UnicodeDecodeError):
			continue
	return data.decode('latin-1')


def _header_text(value: Any) -> str:
	"""A header value with RFC 2047 encoded words decoded (``=?utf-8?B?...?=``)."""
	if value is None:
		return ''
	try:
		chunks = decode_header(str(value))
	except (TypeError, ValueError, LookupError):
		return str(value)
	out: list[str] = []
	for chunk, charset in chunks:
		if isinstance(chunk, (bytes, bytearray)):
			out.append(_decode_bytes(bytes(chunk), charset))
		else:
			out.append(str(chunk))
	return ''.join(out)


def _part_text(part: Any) -> str:
	"""The decoded body of a non-multipart MIME part."""
	try:
		payload = part.get_payload(decode=True)
	except Exception:  # a malformed transfer encoding must not lose the whole message
		payload = None
	if payload is None:
		raw = part.get_payload()
		if isinstance(raw, bytes):
			payload = raw
		elif isinstance(raw, str):
			return raw
		else:
			return ''
	return _decode_bytes(payload, part.get_content_charset())


_SCRIPT_RE = re.compile(r'(?is)<(script|style)\b[^>]*>.*?</\1\s*>')
_ANCHOR_RE = re.compile(
	r'(?is)<a\b[^>]*?\bhref\s*=\s*(?:"(?P<double>[^"]*)"|\'(?P<single>[^\']*)\'|(?P<bare>[^\s>]+))[^>]*>'
	r'(?P<label>.*?)</a\s*>'
)
_BREAK_RE = re.compile(r'(?is)<br\s*/?>')
_BLOCK_END_RE = re.compile(
	r'(?is)</(?:p|div|tr|li|h[1-6]|table|tbody|thead|tfoot|td|th|section|article|header|footer|'
	r'blockquote|pre|dl|dt|dd|ul|ol)\s*>'
)
_TAG_RE = re.compile(r'(?is)<[^>]+>')


def _html_to_text(markup: str) -> str:
	"""Readable text from an HTML mail body, keeping the URLs of links (that is where the buttons point)."""
	text = _SCRIPT_RE.sub(' ', markup)

	def keep_link(match: re.Match[str]) -> str:
		href = unescape((match.group('double') or match.group('single') or match.group('bare') or '').strip())
		label = ' '.join(unescape(_TAG_RE.sub(' ', match.group('label') or '')).split())
		if href.lower().startswith(('http://', 'https://')):
			return f' {label} {href} ' if label else f' {href} '
		return f' {label} '

	text = _ANCHOR_RE.sub(keep_link, text)
	text = _BREAK_RE.sub(' ', text)
	text = _BLOCK_END_RE.sub(' ', text)
	text = _TAG_RE.sub(' ', text)
	return unescape(text)


def message_text(raw: bytes) -> str:
	"""A raw RFC 822 message as normalised text: ``Subject: ... From: ...`` and the readable body.

	Prefers the ``text/plain`` parts; when a message is HTML-only the HTML is stripped and the URLs of
	its links are kept.  Unknown or broken charsets fall back to latin-1 instead of raising.
	"""
	if not raw:
		return ''
	message = message_from_bytes(raw)
	plain: list[str] = []
	markup: list[str] = []
	for part in message.walk():
		if part.is_multipart():
			continue
		if part.get_filename():
			continue  # an attachment is not the message
		disposition = str(part.get('Content-Disposition') or '').lower()
		if disposition.startswith('attachment'):
			continue
		content_type = part.get_content_type()
		if content_type == 'text/plain':
			text = _part_text(part)
			if text.strip():
				plain.append(text)
		elif content_type == 'text/html':
			text = _part_text(part)
			if text.strip():
				markup.append(text)
	body = '\n'.join(plain).strip()
	if not body:
		body = '\n'.join(_html_to_text(part) for part in markup).strip()
	header = f'Subject: {_header_text(message.get("Subject"))} From: {_header_text(message.get("From"))}'
	return ' '.join(f'{header} {body}'.split())


# --- one-time codes -----------------------------------------------------------------------------

_KEYWORD_FAMILIES = (
	'verif(?:y|ies|ying|ied|ication)',
	'security',
	'one[- ]?time',
	'otp',
	'pass[- ]?code',
	'confirm(?:ation|ed|ing|s)?',
	'code[s]?',
	'sign[- ]?in',
	'log[- ]?in',
	'activ(?:ation|ate|ating|ated)',
	'2fa',
	'two[- ]?factor',
	'auth(?:entication|enticate)',
	'valid(?:ation|ate)',
	'pin',
)
_STRONG_FAMILIES = (
	'verif(?:y|ies|ying|ied|ication)',
	'security',
	'one[- ]?time',
	'otp',
	'pass[- ]?code',
	'activ(?:ation|ate|ating|ated)',
	'2fa',
	'two[- ]?factor',
	'auth(?:entication|enticate)',
	'valid(?:ation|ate)',
	'pin',
)
_KEYWORD_RE = re.compile(r'(?i)\b(?:' + '|'.join(_KEYWORD_FAMILIES) + r')\b')
_STRONG_RE = re.compile(r'(?i)\b(?:' + '|'.join(_STRONG_FAMILIES) + r')\b')

# Digits (optionally split in two groups: "123 456", "1234 5678"), or an uppercase mixed code.
_CANDIDATE_RE = re.compile(
	r'(?<![0-9A-Za-z])'
	r'(?:'
	r'(?P<first>\d{2,4})[ -](?P<second>\d{2,4})'
	r'|(?P<digits>\d{4,8})'
	r'|(?P<alnum>[A-Z0-9]{6,8})'
	r')'
	r'(?![0-9A-Za-z])'
)
_URL_SPAN_RE = re.compile(r'https?://[^\s<>"\'`]+', re.I)
_CURRENCY_RE = re.compile(r'[$€£¥₹]')
_MONEY_WORD_RE = re.compile(
	r'(?i)\b(?:usd|eur|gbp|inr|jpy|cad|aud|dollars?|euros?|pounds?|rupees?|price|total|amount|'
	r'cost|subtotal|discount|tax)\b'
)
_PHONE_RE = re.compile(r'\+?\(?\d[\d\s().-]{6,}\d')
_NEGATIVE_NEAR_RE = re.compile(
	r'(?i)\b(?:orders?|tracking|invoice|receipts?|reference|tickets?|shipment|deliver(?:y|ies)|'
	r'transactions?|payments?|promo|coupons?|discounts?|zip|postal|serial|'
	r'(?:user|customer|account|order|invoice|ticket|reference)\s*(?:id|number|no)\b)'
)
_SITE_TLDS = {'co', 'com', 'org', 'net', 'gov', 'ac', 'edu'}


@dataclass(frozen=True)
class _Candidate:
	value: str
	start: int
	end: int
	digits: bool


def _candidates(text: str) -> list[_Candidate]:
	found: list[_Candidate] = []
	for match in _CANDIDATE_RE.finditer(text):
		first = match.group('first')
		if first is not None:
			second = match.group('second') or ''
			if not 4 <= len(first) + len(second) <= 8:
				continue
			found.append(_Candidate(first + second, match.start(), match.end(), True))
			continue
		digits = match.group('digits')
		if digits is not None:
			found.append(_Candidate(digits, match.start(), match.end(), True))
			continue
		alnum = match.group('alnum') or ''
		if not any(char.isdigit() for char in alnum) or not any(char.isalpha() for char in alnum):
			continue  # a plain word ("VERIFY") or a plain number, not a mixed code
		found.append(_Candidate(alnum, match.start(), match.end(), False))
	return found


def _min_gap(spans: list[tuple[int, int]], start: int, end: int) -> int:
	"""Chars between a candidate and the nearest span (0 when they touch/overlap); a big number when no spans."""
	best = 10 ** 9
	for span_start, span_end in spans:
		gap = max(span_start - end, start - span_end, 0)
		if gap < best:
			best = gap
	return best


def _inside_spans(spans: list[tuple[int, int]], start: int, end: int) -> bool:
	return any(span_start < end and start < span_end for span_start, span_end in spans)


def _site_terms(site: str) -> list[str]:
	"""Lowercase names a message could use for the site: full host, registrable domain, bare name."""
	text = (site or '').strip().lower()
	if not text:
		return []
	if '://' in text:
		text = text.split('://', 1)[1]
	text = text.split('/', 1)[0].split('?', 1)[0].split('#', 1)[0]
	if '@' in text:
		text = text.rsplit('@', 1)[1]
	text = text.split(':', 1)[0].strip('.')
	text = text.removeprefix('www.')
	if not text:
		return []
	terms = {text}
	labels = [label for label in text.split('.') if label]
	if len(labels) >= 2:
		terms.add(labels[-2] + '.' + labels[-1])
		name_index = -2
		if len(labels) >= 3 and labels[-2] in _SITE_TLDS:
			name_index = -3
		terms.add(labels[name_index])
	elif labels:
		terms.add(labels[0])
	return [term for term in terms if len(term) >= 3]


def _site_domain(site: str) -> str:
	"""The site's host without scheme, path or ``www.``; '' when there is none."""
	text = (site or '').strip().lower()
	if not text:
		return ''
	if '://' in text:
		text = text.split('://', 1)[1]
	text = text.split('/', 1)[0].split('?', 1)[0].split('#', 1)[0]
	if '@' in text:
		text = text.rsplit('@', 1)[1]
	return text.split(':', 1)[0].strip('.').removeprefix('www.')


def _nearest_term_gap(lower_text: str, terms: list[str], start: int, end: int) -> int | None:
	"""Chars between a candidate and the nearest occurrence of any site term."""
	best: int | None = None
	for term in terms:
		position = lower_text.find(term)
		while position >= 0:
			gap = max(position - end, start - (position + len(term)), 0)
			if gap == 0:
				return 0
			if best is None or gap < best:
				best = gap
			position = lower_text.find(term, position + 1)
	return best


def _looks_like_money(text: str, candidate: _Candidate) -> bool:
	"""A price/amount rather than a code: money next to the candidate, or money words plus a currency."""
	window_start = max(0, candidate.start - 25)
	window = text[window_start:candidate.end + 25]
	currency = _CURRENCY_RE.search(window)
	money_word = _MONEY_WORD_RE.search(window)
	if not currency and not money_word:
		return False
	before = text[max(0, candidate.start - 3):candidate.start]
	after = text[candidate.end:candidate.end + 8]
	if _CURRENCY_RE.search(before):
		return True
	if _MONEY_WORD_RE.match(after.lstrip()):
		return True
	return bool(currency and money_word)


def _looks_like_phone(text: str, candidate: _Candidate) -> bool:
	"""Part of a phone number: a run of 10+ digits with the usual separators overlapping the candidate."""
	window_start = max(0, candidate.start - 30)
	window = text[window_start:candidate.end + 30]
	for match in _PHONE_RE.finditer(window):
		if sum(char.isdigit() for char in match.group()) < 10:
			continue
		start = window_start + match.start()
		end = window_start + match.end()
		if start < candidate.end and candidate.start < end:
			return True
	return False


def _has_negative_near(text: str, candidate: _Candidate, radius: int = 40) -> bool:
	window = text[max(0, candidate.start - radius):candidate.end + radius]
	return bool(_NEGATIVE_NEAR_RE.search(window))


def _candidate_score(
	candidate: _Candidate,
	keyword_gap: int,
	strong_gap: int,
	lower_text: str,
	site_terms: list[str],
	header_lower: str,
) -> float:
	score = 60.0 + max(0.0, 40.0 - keyword_gap * 0.5)
	if strong_gap < 10 ** 9:
		score += max(0.0, 20.0 - strong_gap * 0.15)
	if candidate.digits:
		score += 8.0
		if len(candidate.value) == 6:
			score += 4.0
		elif len(candidate.value) == 8:
			score += 2.0
	else:
		score -= 6.0
	if site_terms:
		term_gap = _nearest_term_gap(lower_text, site_terms, candidate.start, candidate.end)
		if term_gap is not None and term_gap <= 120:
			score += max(5.0, 40.0 - term_gap * 0.15)
		elif any(term in header_lower for term in site_terms):
			score += 10.0
		elif any(term in lower_text for term in site_terms):
			score += 4.0
	return score


def extract_code(text: str, site: str = '') -> str | None:
	"""The most likely one-time code in an email, or None when nothing has code-like context.

	Digs out 4-8 digit codes (including formats such as ``G-123 456`` -> ``123456``) and, only when
	clearly next to a verification word, uppercase mixed codes such as ``AB12CD``.  Conservative: a
	number with no verification/security/code keyword within ~120 chars, a year, a price, a phone
	number, a tracking/order/serial number or a number inside a URL is not a code.
	"""
	if not text:
		return None
	keyword_spans = [(match.start(), match.end()) for match in _KEYWORD_RE.finditer(text)]
	if not keyword_spans:
		return None
	strong_spans = [(match.start(), match.end()) for match in _STRONG_RE.finditer(text)]
	url_spans = [(match.start(), match.end()) for match in _URL_SPAN_RE.finditer(text)]
	site_terms = _site_terms(site)
	lower_text = text.lower()
	header_lower = lower_text[:HEADER_CHARS]
	best_value: str | None = None
	best_key: tuple[float, int, int] | None = None
	for candidate in _candidates(text):
		keyword_gap = _min_gap(keyword_spans, candidate.start, candidate.end)
		if keyword_gap > 120:
			continue  # no code context at all
		if not candidate.digits and keyword_gap > 60:
			continue  # an alphanumeric code must be clearly near the keyword
		if _inside_spans(url_spans, candidate.start, candidate.end):
			continue  # a number/token in a link is not the code the site wants typed
		if candidate.digits and len(candidate.value) == 4 and 1900 <= int(candidate.value) <= 2099:
			continue  # a year
		if _looks_like_money(text, candidate) or _looks_like_phone(text, candidate):
			continue
		strong_gap = _min_gap(strong_spans, candidate.start, candidate.end)
		if strong_gap > 40 and _has_negative_near(text, candidate):
			continue  # order/tracking/invoice/... number, with only generic "code" context
		score = _candidate_score(candidate, keyword_gap, strong_gap, lower_text, site_terms, header_lower)
		key = (score, -keyword_gap, -candidate.start)
		if best_key is None or key > best_key:
			best_key, best_value = key, candidate.value
	return best_value


# --- confirmation links -------------------------------------------------------------------------

_LINK_VERB_RE = re.compile(r'(?i)(?:verif|confirm|activat|validat|magic|sign[-_]?in|token)')
_LINK_STRONG_RE = re.compile(r'(?i)(?:verif|confirm|activat|validat|magic)')
_LINK_BAD_RE = re.compile(
	r'(?i)(?:unsubscribe|opt[-_]?out|preferences|manage[-_]?(?:subscriptions?|preferences|notifications?)'
	r'|privacy|/terms?\b|/help\b|/support\b|tracking|/track\b|\btrack\b|pixel|beacon|marketing|survey|newsletter)'
)
_LINK_BAD_HOST_RE = re.compile(r'^(?:help|support|privacy|terms|preferences|tracking|track)\.')
_LINK_ASSET_RE = re.compile(r'(?i)\.(?:png|jpe?g|gif|svg|webp|ico|css|js|woff2?)(?:[?#]|$)')
_URL_TRAILING = '.,;:!?\'"`)]}>'


def _clean_url(url: str) -> str:
	url = url.strip()
	while url and url[-1] in _URL_TRAILING:
		url = url[:-1]
	return url


def _url_host(url: str) -> str:
	rest = url.split('://', 1)[1] if '://' in url else url
	host = rest.split('/', 1)[0].split('?', 1)[0].split('#', 1)[0]
	if '@' in host:
		host = host.rsplit('@', 1)[1]
	if host.startswith('['):
		host = host[1:host.find(']')] if ']' in host else host[1:]
	else:
		host = host.split(':', 1)[0]
	return host.lower().strip('.')


def _url_path(url: str) -> str:
	"""The part of a URL after the host, lowercased (where verification links carry their verb)."""
	rest = url.split('://', 1)[1] if '://' in url else url
	slash = rest.find('/')
	return rest[slash:].lower() if slash >= 0 else ''


def extract_link(text: str, site: str = '') -> str | None:
	"""The best HTTP(S) verification/confirmation link in an email, or None.

	Prefers a link on the site's own domain whose path says verify/confirm/activate/validate/magic/
	sign-in/token.  Rejects unsubscribe, preferences, help, support, privacy, terms, tracking,
	marketing and image links, and strips trailing punctuation.
	"""
	if not text:
		return None
	site_terms = _site_terms(site)
	domain = _site_domain(site)
	lower_text = text.lower()
	best: tuple[float, int, str] | None = None
	seen: set[str] = set()
	for match in _URL_SPAN_RE.finditer(text):
		url = _clean_url(match.group(0))
		if not url or url in seen:
			continue
		seen.add(url)
		low = url.lower()
		if _LINK_ASSET_RE.search(low):
			continue
		if _LINK_BAD_RE.search(low) or _LINK_BAD_HOST_RE.match(_url_host(url)):
			continue
		path = _url_path(url)
		if not _LINK_VERB_RE.search(path):
			continue
		score = 10.0
		if _LINK_STRONG_RE.search(path):
			score += 5.0
		host = _url_host(url)
		if domain and (host == domain or host.endswith('.' + domain)):
			score += 20.0
		elif site_terms and any(term in low for term in site_terms):
			score += 12.0
		context = lower_text[max(0, match.start() - 120):match.start()]
		if re.search(r'\b(?:click|button|link|verify|confirm|activate|validate)\b', context):
			score += 4.0
		if len(url) > 300:
			score -= 3.0
		if best is None or (score, -match.start()) > (best[0], -best[1]):
			best = (score, match.start(), url)
	return best[2] if best else None


# --- IMAP ---------------------------------------------------------------------------------------

_MONTHS = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')


def _format_imap_date(since: float) -> str:
	"""Unix seconds -> the ``%d-%b-%Y`` (e.g. ``18-Nov-2026``) date IMAP's SINCE takes."""
	when = time.gmtime(float(since) - SKEW_SECONDS)
	return f'{when.tm_mday:02d}-{_MONTHS[when.tm_mon - 1]}-{when.tm_year:04d}'


def _status_of(result: Any) -> tuple[str | None, Any]:
	"""``('OK', data)`` from either ``('OK', [...])`` or a bare data list."""
	if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], (str, bytes)):
		status = result[0].decode('ascii', 'replace') if isinstance(result[0], bytes) else result[0]
		if status.strip().upper() in {'OK', 'NO', 'BAD', 'BYE', 'PREAUTH'}:
			return status.strip().upper(), result[1]
	return None, result


def _ids_from_search(data: Any) -> list[str]:
	"""The message ids in a SEARCH response, in server order (oldest first)."""
	if data is None:
		return []
	items = list(data) if isinstance(data, (list, tuple)) else [data]
	ids: list[str] = []
	for item in items:
		if item is None:
			continue
		if isinstance(item, (bytes, bytearray)):
			ids.extend(piece.decode('ascii', 'replace') for piece in bytes(item).split())
		else:
			ids.extend(str(item).split())
	return [value for value in ids if value]


def _id_from_head(head: Any) -> str:
	"""The message id in a FETCH response head: ``b'6 (UID 42 BODY[] {123}'`` -> ``'6'`` (or the UID)."""
	text = head.decode('ascii', 'replace') if isinstance(head, (bytes, bytearray)) else str(head or '')
	uid = re.search(r'\bUID\s+(\d+)', text)
	if uid:
		return uid.group(1)
	sequence = re.match(r'\s*(\d+)', text)
	return sequence.group(1) if sequence else ''


def _looks_like_fetch_head(value: Any) -> bool:
	text = value.decode('ascii', 'replace') if isinstance(value, (bytes, bytearray)) else str(value or '')
	return 'BODY' in text.upper() or 'FETCH' in text.upper() or text.rstrip().endswith(('{', '}'))


def _fetch_payloads(result: Any) -> list[tuple[str, bytes]]:
	"""Message bodies from a FETCH response, tolerating the shapes imaplib and test fakes return.

	Supported: ``('OK', [(head, body), b')'])``, ``[(head, body)]``, ``(head, body)`` and a flat
	``('OK', [head, body, b')'])``.
	"""
	_, data = _status_of(result)
	if data is None:
		return []
	if isinstance(data, (bytes, bytearray)):
		data = [data]
	try:
		items = list(data)
	except TypeError:
		return []
	payloads: list[tuple[str, bytes]] = []
	index = 0
	while index < len(items):
		item = items[index]
		if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], (bytes, bytearray)):
			payloads.append((_id_from_head(item[0]), bytes(item[1])))
			index += 1
			continue
		if isinstance(item, (bytes, bytearray)) and not _status_of(item)[0]:
			next_item = items[index + 1] if index + 1 < len(items) else None
			if isinstance(next_item, (bytes, bytearray)) and _looks_like_fetch_head(item):
				payloads.append((_id_from_head(item), bytes(next_item)))
				index += 2
				continue
		index += 1
	return payloads


def _map_payloads(payloads: list[tuple[str, bytes]], ids: list[str]) -> dict[str, bytes]:
	"""Map fetched bodies to the ids that were asked for, by response head or by position."""
	out: dict[str, bytes] = {}
	for index, (head_id, raw) in enumerate(payloads):
		if head_id and head_id in ids and head_id not in out:
			out[head_id] = raw
		elif index < len(ids) and ids[index] not in out:
			out[ids[index]] = raw
	return out


def _received_after(raw: bytes, since: float) -> bool:
	"""True unless the message's Date header says it is older than ``since`` (2 minutes of skew allowed)."""
	message = message_from_bytes(raw)
	value = message.get('Date')
	if not value:
		return True  # no Date: trust the server-side SINCE search, which already bounded the day
	try:
		sent = parsedate_to_datetime(str(value))
	except (TypeError, ValueError, OverflowError):
		return True
	if sent is None:
		return True
	if sent.tzinfo is None:
		sent = sent.replace(tzinfo=timezone.utc)
	return sent.timestamp() >= float(since) - SKEW_SECONDS


class MailReader:
	"""Polls an IMAP inbox for the newest verification code or confirmation link.

	Blocking (callers run it in a thread).  Every poll opens a fresh read-only connection, inspects
	the newest ``FETCH_LIMIT`` messages of the INBOX with ``BODY.PEEK[]`` (never marking them read),
	and remembers the ids it has seen so later polls only look at new mail.  A failed login/search is
	logged once and retried until ``timeout``; the result is None when nothing arrives in time, and
	the caller can then ask the person.
	"""

	def __init__(
		self,
		mailbox: Mailbox,
		email: str,
		password: str,
		*,
		imap_factory: Callable[..., Any] = imaplib.IMAP4_SSL,
		timeout: float = WAIT_SECONDS,
		poll: float = POLL_SECONDS,
		sleep: Callable[[float], Any] = time.sleep,
		clock: Callable[[], float] = time.monotonic,
	) -> None:
		self.mailbox = mailbox
		self.email = email
		self._password = password
		self._imap_factory = imap_factory
		self.timeout = timeout
		self.poll = poll
		self._sleep = sleep
		self._clock = clock
		self._seen: set[str] = set()
		self._logged_errors: set[str] = set()

	def code(self, site: str, *, since: float) -> str | None:
		"""The first one-time code found in mail received after ``since`` (newest message first), or None."""
		return self._wait(extract_code, site, since)

	def link(self, site: str, *, since: float) -> str | None:
		"""The first confirmation link found in mail received after ``since`` (newest first), or None."""
		return self._wait(extract_link, site, since)

	def _wait(self, finder: Callable[[str, str], str | None], site: str, since: float) -> str | None:
		deadline = self._clock() + max(0.0, float(self.timeout))
		attempts = 0
		while True:
			attempts += 1
			found = self._poll_once(finder, site, since)
			if found is not None:
				log.debug('mailbox %s: found after %d poll(s)', self.mailbox.provider, attempts)
				return found
			remaining = deadline - self._clock()
			if remaining <= 0:
				log.debug('mailbox %s: nothing after %d poll(s)', self.mailbox.provider, attempts)
				return None
			step = min(max(0.0, float(self.poll)), remaining)
			if step <= 0:  # a zero poll interval would otherwise spin forever
				return None
			self._sleep(step)
			if self._clock() >= deadline:
				return None

	def _poll_once(self, finder: Callable[[str, str], str | None], site: str, since: float) -> str | None:
		client = None
		try:
			client = self._imap_factory(self.mailbox.host, self.mailbox.port)
			client.login(self.email, self._password)
			self._select_inbox(client)
			ids = self._search_ids(client, since)
			if not ids:
				return None
			pending = [message_id for message_id in ids if message_id not in self._seen]
			if not pending:
				return None
			raws = self._fetch(client, pending)
			self._seen.update(raws)  # only mail we actually read is considered; a failed fetch is retried
			log.debug('mailbox %s: %d new message(s)', self.mailbox.provider, len(pending))
			for message_id in pending:
				raw = raws.get(message_id)
				if raw is None or not _received_after(raw, since):
					continue
				found = finder(message_text(raw), site)
				if found is not None:
					return found
			return None
		except Exception as error:  # a bad poll must never break the run; keep trying until the deadline
			self._log_failure(error)
			return None
		finally:
			if client is not None:
				self._close(client)

	@staticmethod
	def _select_inbox(client: Any) -> None:
		try:
			result = client.select('INBOX', readonly=True)
		except TypeError:
			try:
				result = client.select('INBOX')
			except TypeError:
				try:
					result = client.select('INBOX', False)
				except TypeError:
					result = client.select()
		status, _ = _status_of(result)
		if status is not None and status != 'OK':
			raise imaplib.IMAP4.error(f'SELECT INBOX failed: {status}')

	@staticmethod
	def _search_ids(client: Any, since: float) -> list[str]:
		# A UID SEARCH would be cleaner, but plain SEARCH returns the ids every server also FETCHes here.
		result = client.search(None, f'(SINCE {_format_imap_date(since)})')
		status, data = _status_of(result)
		if status is not None and status != 'OK':
			raise imaplib.IMAP4.error(f'SEARCH failed: {status}')
		ids = _ids_from_search(data)
		return ids[-FETCH_LIMIT:][::-1]  # newest first

	def _fetch(self, client: Any, ids: list[str]) -> dict[str, bytes]:
		"""Bodies for the ids, one batch fetch when the client supports it and per-message otherwise."""
		try:
			result = client.fetch(','.join(ids), '(BODY.PEEK[])')
		except Exception as error:
			log.debug('mailbox %s: batch fetch failed (%s), fetching one by one', self.mailbox.provider, type(error).__name__)
			return self._fetch_each(client, ids)
		raws = _map_payloads(_fetch_payloads(result), ids)
		missing = [message_id for message_id in ids if message_id not in raws]
		if missing and len(missing) == len(ids):
			return self._fetch_each(client, ids)
		if missing:
			raws.update(self._fetch_each(client, missing))
		return raws

	@staticmethod
	def _fetch_each(client: Any, ids: list[str]) -> dict[str, bytes]:
		raws: dict[str, bytes] = {}
		for message_id in ids:
			try:
				result = client.fetch(message_id, '(BODY.PEEK[])')
			except Exception:
				continue
			for _, raw in _fetch_payloads(result):
				raws[message_id] = raw
				break
		return raws

	def _log_failure(self, error: BaseException) -> None:
		"""One warning per kind of failure, then debug; the message never contains the password."""
		kind = type(error).__name__
		message = str(error)
		if self._password:
			message = message.replace(self._password, '***')
		if kind in self._logged_errors:
			log.debug('mailbox %s: %s again: %s', self.mailbox.provider, kind, message)
			return
		self._logged_errors.add(kind)
		log.warning('mailbox %s: %s: %s (will keep trying)', self.mailbox.provider, kind, message)

	@staticmethod
	def _close(client: Any) -> None:
		for method in ('close', 'logout'):
			try:
				getattr(client, method)()
			except Exception as error:
				log.debug('mailbox: %s failed: %s', method, type(error).__name__)
