"""Detect and solve CAPTCHA / human-verification widgets.

Two independent layers, both usable without a browser:

* ``detect`` is a pure function over a page's HTML: which widget is on the page and the sitekey
  (or image selector) that a solver and the page form need. It is deliberately conservative: a
  wrong widget wastes a paid solve and still does not produce a token the site accepts.
* ``injection_js`` / ``image_entry_js`` return JavaScript for the caller to evaluate in the page;
  the token / answer is written where the site's form validation reads it, with ``input`` and
  ``change`` events. The token is embedded through ``json.dumps`` so it can never break out of
  the JavaScript string, and page-derived selectors go through ``json.dumps`` too.

``TwoCaptcha`` (2captcha.com and the resellers speaking its API) and ``CapSolver``
(api.capsolver.com) are async clients for the two hosted solving services. An
``httpx.AsyncClient`` can be injected, so tests run them against ``httpx.MockTransport`` with no
network; a client the solver did not create is never closed by the solver. Provider failures and
timeouts return ``None`` (logged at debug) instead of raising to the caller.

The module imports nothing from veil: the browser/agent layer calls ``detect(html, url)``, gets a
token from a ``Solver``, then evaluates ``injection_js(widget, token)`` in the page.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import httpx

log = logging.getLogger('veil.captcha')

__all__ = [
	'CapSolver',
	'Solver',
	'SolverError',
	'TwoCaptcha',
	'Widget',
	'detect',
	'image_entry_js',
	'injection_js',
	'make_solver',
]

MAX_HTML = 200_000  # only the first ~200k chars are inspected; long pages are mostly boilerplate
_HTTP_TIMEOUT = 30.0  # per HTTP request; `timeout=` on a solver is the whole solve deadline

# Widget kinds the rest of veil knows about.
KINDS = ('recaptcha_v2', 'recaptcha_v3', 'hcaptcha', 'turnstile', 'funcaptcha', 'geetest', 'image')
TOKEN_KINDS = KINDS[:-1]


@dataclass(frozen=True)
class Widget:
	"""One CAPTCHA / human-verification widget found on a page."""

	kind: str  # 'recaptcha_v2' | 'recaptcha_v3' | 'hcaptcha' | 'turnstile' | 'funcaptcha' | 'geetest' | 'image'
	sitekey: str | None
	page_url: str
	vendor: str = ''
	selector: str | None = None  # image captchas: CSS selector for the <img>
	data: dict = field(default_factory=dict)  # e.g. geetest gt/challenge, funcaptcha public key


# --- HTML helpers ---------------------------------------------------------------------------------

# A tag, quoted-attribute regexes and small scanners. Regexes are used instead of an HTML parser so
# detect() stays a cheap pure function; the first ~200k chars are all that is ever inspected.
_TAG_RE = re.compile(r'<[^<>]{1,4000}>')
_IFRAME_RE = re.compile(r'<iframe\b[^<>]{0,4000}>', re.I)
_IMG_RE = re.compile(r'<img\b[^<>]{0,4000}>', re.I)

_RECAPTCHA_MARK = re.compile(r'g-recaptcha(?!-response)|recaptcha/(?:api2|enterprise)', re.I)
# The interactive widget (not the api.js loader): used to prefer v2 over v3 when both appear.
_RECAPTCHA_INTERACTIVE = re.compile(r'g-recaptcha(?!-response)|recaptcha/api2/|recaptcha/enterprise/(?:anchor|bframe)', re.I)
_RECAPTCHA_V3 = re.compile(r'grecaptcha\s*(?:\.\s*enterprise\s*)?\.\s*(?:execute|ready)\s*\(', re.I)
_RECAPTCHA_V3_EXECUTE = re.compile(r"""grecaptcha\s*(?:\.\s*enterprise\s*)?\.\s*execute\s*\(\s*(['"])([^'"\n]{4,512})\1""", re.I)
_RECAPTCHA_SCRIPT = re.compile(r'recaptcha/(?:api|enterprise)\.js|google\.com/recaptcha', re.I)
_HCAPTCHA_MARK = re.compile(r'h-captcha(?!-response)|hcaptcha\.com', re.I)
_HCAPTCHA_TAG = re.compile(r'h-captcha(?!-response)', re.I)
_TURNSTILE_MARK = re.compile(r'''cf-turnstile(?!-response)|challenges\.cloudflare\.com/(?:[^"'\s>]*/)?turnstile''', re.I)
_TURNSTILE_TAG = re.compile(r'cf-turnstile(?!-response)', re.I)
_FUNCAPTCHA_MARK = re.compile(r'arkoselabs|funcaptcha|arkoseenforcement|data-pkey', re.I)
_GEETEST_MARK = re.compile(r'geetest', re.I)
_CAPTCHA_WORD = re.compile(r'captcha', re.I)
_SAFE_ID = re.compile(r'[A-Za-z][A-Za-z0-9_-]*')
_SAFE_CLASS = re.compile(r'[A-Za-z_][A-Za-z0-9_-]*')


def _next_group(match: re.Match[str]) -> str:
	return next((group for group in match.groups() if group is not None), '')


def _attr(tag: str, name: str) -> str | None:
	"""The value of attribute `name` inside one tag string, or None."""
	match = re.search(
		rf'''(?<![-\w]){re.escape(name)}\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))''',
		tag,
		re.I,
	)
	if not match:
		return None
	value = _next_group(match).strip()
	return value or None


def _first_attr(html: str, name: str) -> str | None:
	"""The first value of attribute `name` anywhere in the HTML."""
	match = re.search(
		rf'''(?<![-\w]){re.escape(name)}\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))''',
		html,
		re.I,
	)
	if not match:
		return None
	value = _next_group(match).strip()
	return value or None


def _tag_attr(html: str, marker: re.Pattern[str], name: str) -> str | None:
	"""The first `name` attribute on a tag whose text also matches `marker`."""
	for match in _TAG_RE.finditer(html):
		tag = match.group(0)
		if not marker.search(tag):
			continue
		value = _attr(tag, name)
		if value:
			return value
	return None


def _tag_src_attr(html: str, marker: re.Pattern[str], name: str) -> str | None:
	"""The query parameter `name` from the src of the first matching tag (e.g. api.js?render=KEY)."""
	for match in _TAG_RE.finditer(html):
		tag = match.group(0)
		if not marker.search(tag):
			continue
		src = _attr(tag, 'src')
		if not src:
			continue
		value = _query_value(src, name)
		if value:
			return value
	return None


def _query_value(value: str, name: str) -> str | None:
	"""A parameter from a URL-like string; works for query strings and hash fragments."""
	match = re.search(rf'''[?&#]{re.escape(name)}=([^&#"'\s>]{{1,512}})''', value)
	if not match:
		return None
	parameter = match.group(1).strip()
	return parameter or None


def _js_value(html: str, *names: str) -> str | None:
	"""The first `name: "value"` / `name = 'value'` in inline JavaScript or a JSON blob."""
	for name in names:
		match = re.search(
			rf'''(?<![A-Za-z0-9_$-])['"]?{re.escape(name)}['"]?\s*[:=]\s*(?:"([^"\n]{{1,512}})"|'([^'\n]{{1,512}})')''',
			html,
			re.I,
		)
		if not match:
			continue
		value = _next_group(match).strip()
		if value:
			return value
	return None


def _iframe_value(html: str, needle: str, name: str) -> str | None:
	"""A query parameter from the src of an iframe whose src mentions `needle`."""
	for match in _IFRAME_RE.finditer(html):
		src = _attr(match.group(0), 'src')
		if not src or needle not in src.lower():
			continue
		value = _query_value(src, name)
		if value:
			return value
	return None


# --- detection ------------------------------------------------------------------------------------

def _recaptcha_v2_sitekey(html: str) -> str | None:
	return (
		_tag_attr(html, _RECAPTCHA_INTERACTIVE, 'data-sitekey')
		or _iframe_value(html, 'recaptcha', 'k')
		or _js_value(html, 'sitekey')
		or _first_attr(html, 'data-sitekey')
	)


def _recaptcha_v3_sitekey(html: str) -> str | None:
	execute = _RECAPTCHA_V3_EXECUTE.search(html)
	if execute:
		return execute.group(2).strip() or None
	render = _tag_src_attr(html, _RECAPTCHA_SCRIPT, 'render')
	if render and render.lower() not in ('explicit', 'onload', 'true', '1'):
		return render
	return _first_attr(html, 'data-sitekey')


def _hcaptcha_sitekey(html: str) -> str | None:
	return (
		_tag_attr(html, _HCAPTCHA_TAG, 'data-sitekey')
		or _iframe_value(html, 'hcaptcha', 'sitekey')
		or _js_value(html, 'sitekey')
		or _first_attr(html, 'data-sitekey')
	)


def _turnstile_sitekey(html: str) -> str | None:
	return (
		_tag_attr(html, _TURNSTILE_TAG, 'data-sitekey')
		or _iframe_value(html, 'turnstile', 'sitekey')
		or _iframe_value(html, 'cloudflare', 'sitekey')
		or _js_value(html, 'sitekey')
		or _first_attr(html, 'data-sitekey')
	)


def _funcaptcha_sitekey(html: str) -> str | None:
	return (
		_first_attr(html, 'data-pkey')
		or _js_value(html, 'public_key', 'publicKey', 'pkey', 'websitePublicKey')
		or _first_attr(html, 'data-sitekey')
	)


def _detect_image(html: str, page_url: str) -> Widget | None:
	"""A plain image CAPTCHA: an <img> whose id/name/class/src says captcha, with a usable selector."""
	candidates = []
	for match in _IMG_RE.finditer(html):
		tag = match.group(0)
		id_ = _attr(tag, 'id') or ''
		name = _attr(tag, 'name') or ''
		classes = _attr(tag, 'class') or ''
		src = _attr(tag, 'src') or ''
		if _CAPTCHA_WORD.search(' '.join((id_, name, classes, src))):
			candidates.append((id_, classes))
	if not candidates:
		return None

	# How often each class occurs on an <img>, so a class selector is only used when it is unambiguous.
	class_counts: dict[str, int] = {}
	for match in _IMG_RE.finditer(html):
		for class_name in (_attr(match.group(0), 'class') or '').split():
			class_counts[class_name] = class_counts.get(class_name, 0) + 1

	for id_, classes in candidates:
		if id_ and _SAFE_ID.fullmatch(id_):
			return Widget('image', None, page_url, selector=f'#{id_}')
		for class_name in classes.split():
			if not _CAPTCHA_WORD.search(class_name) or not _SAFE_CLASS.fullmatch(class_name):
				continue
			if class_counts.get(class_name) == 1:
				return Widget('image', None, page_url, selector=f'img.{class_name}')
	return None


def detect(html: str, url: str = '') -> Widget | None:
	"""The most likely CAPTCHA widget on a page, or None.

	Pure and conservative: a token widget without a sitekey is reported as None (there is nothing to
	solve), and an image CAPTCHA needs a selector the caller can hand to ``image_entry_js``. Only the
	first ~200k chars of the HTML are inspected. Token widgets win over image widgets when both are
	present (image CAPTCHA pages often load vendor logos that contain "captcha").
	"""
	page = (html or '')[:MAX_HTML]
	page_url = url or ''

	v3_marker = _RECAPTCHA_V3.search(page)
	if _RECAPTCHA_MARK.search(page) or v3_marker:
		if v3_marker and not _RECAPTCHA_INTERACTIVE.search(page):
			sitekey = _recaptcha_v3_sitekey(page)
			return Widget('recaptcha_v3', sitekey, page_url) if sitekey else None
		sitekey = _recaptcha_v2_sitekey(page)
		return Widget('recaptcha_v2', sitekey, page_url) if sitekey else None

	if _HCAPTCHA_MARK.search(page):
		sitekey = _hcaptcha_sitekey(page)
		return Widget('hcaptcha', sitekey, page_url) if sitekey else None

	if _TURNSTILE_MARK.search(page):
		sitekey = _turnstile_sitekey(page)
		if not sitekey:
			return None
		data: dict[str, Any] = {}
		callback = _tag_attr(page, _TURNSTILE_TAG, 'data-callback')
		if callback:
			data['callback'] = callback
		return Widget('turnstile', sitekey, page_url, data=data)

	if _FUNCAPTCHA_MARK.search(page):
		sitekey = _funcaptcha_sitekey(page)
		if not sitekey:
			return None
		data = {'public_key': sitekey}
		blob = _first_attr(page, 'data-blob') or _js_value(page, 'blob')
		if blob:
			data['blob'] = blob
		return Widget('funcaptcha', sitekey, page_url, data=data)

	if _GEETEST_MARK.search(page):
		gt = _js_value(page, 'gt') or _query_value(page, 'gt') or _first_attr(page, 'data-gt')
		challenge = (
			_js_value(page, 'challenge')
			or _query_value(page, 'challenge')
			or _first_attr(page, 'data-challenge')
		)
		if gt or challenge:
			data = {}
			if gt:
				data['gt'] = gt
			if challenge:
				data['challenge'] = challenge
			return Widget('geetest', None, page_url, data=data)
		return None

	return _detect_image(page, page_url)


# --- token injection ------------------------------------------------------------------------------

# Where each vendor's page reads the solved token from. Sites are inconsistent: hCaptcha pages often
# mirror the token into a g-recaptcha-response field, and reCAPTCHA sometimes renders extra ids like
# "g-recaptcha-response-100000", hence the prefix selectors.
_RECAPTCHA_FIELDS = (
	'textarea[name="g-recaptcha-response"]',
	'#g-recaptcha-response',
	'textarea[id^="g-recaptcha-response"]',
)
_HCAPTCHA_FIELDS = (
	'textarea[name="h-captcha-response"]',
	'textarea[name="g-recaptcha-response"]',
	'textarea[id^="h-captcha-response"]',
)
_TURNSTILE_FIELDS = (
	'input[name="cf-turnstile-response"]',
	'textarea[name="cf-turnstile-response"]',
)
_FUNCAPTCHA_FIELDS = (
	'#FunCaptcha-Token',
	'input[name="fc-token"]',
)

# Static selector lists are quoted with single quotes (readable in the page). Only values that come
# from the Widget (token, callback name, image selector) are embedded through json.dumps.
_SET_FIELD_JS = (
	'const setField = (element, value) => {'
	' const descriptor = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(element), "value");'
	' if (descriptor && descriptor.set) { descriptor.set.call(element, value); } else { element.value = value; }'
	' element.dispatchEvent(new Event("input", {bubbles: true}));'
	' element.dispatchEvent(new Event("change", {bubbles: true}));'
	'};'
)


def _js_string(value: str) -> str:
	"""Single-quoted JS string literal. Only used for module-constant selectors; page data uses json.dumps."""
	escaped = (
		value.replace('\\', '\\\\')
		.replace("'", "\\'")
		.replace('\n', '\\n')
		.replace('\r', '\\r')
		.replace('\u2028', '\\u2028')
		.replace('\u2029', '\\u2029')
	)
	return f"'{escaped}'"


def _fill_js(token: str, selectors: tuple[str, ...], callback: str | None = None) -> str:
	"""JS that writes `token` into every element matching `selectors` and fires input+change."""
	selector_list = '[' + ', '.join(_js_string(selector) for selector in selectors) + ']'
	lines = [
		'(() => {',
		'\ttry {',
		f'\t\tconst token = {json.dumps(token)};',
		f'\t\t{_SET_FIELD_JS}',
		f'\t\tconst selectors = {selector_list};',
		'\t\tlet filled = 0;',
		'\t\tfor (const selector of selectors) {',
		'\t\t\tfor (const element of document.querySelectorAll(selector)) {',
		'\t\t\t\ttry {',
		'\t\t\t\t\tsetField(element, token);',
		'\t\t\t\t\tfilled += 1;',
		'\t\t\t\t} catch (error) {}',
		'\t\t\t}',
		'\t\t}',
	]
	if callback:
		lines.extend(
			[
				'\t\ttry {',
				f'\t\t\tconst notify = window[{json.dumps(callback)}];',
				'\t\t\tif (typeof notify === "function") { notify(token); }',
				'\t\t} catch (error) {}',
			]
		)
	lines.extend(
		[
			'\t\treturn filled;',
			'\t} catch (error) {',
			'\t\treturn 0;',
			'\t}',
			'})()',
		]
	)
	return '\n'.join(lines)


_IMAGE_INPUT_SELECTORS = (
	'input[name*="captcha" i]',
	'input[id*="captcha" i]',
	'input[name*="code" i]',
	'input[id*="code" i]',
	'input[name*="answer" i]',
	'input[id*="answer" i]',
	'input[type="text"]',
	'input:not([type])',
	'input[type="search"]',
	'input[type="tel"]',
	'input[type="number"]',
)
_IMAGE_INPUT_SELECTOR_JS = '[' + ', '.join(_js_string(selector) for selector in _IMAGE_INPUT_SELECTORS) + ']'


def image_entry_js(widget: Widget, answer: str) -> str:
	"""JS that puts an image-CAPTCHA answer into the nearest visible text input and fires input+change.

	Defensive by design: it walks up from the <img> at most a few levels (so it finds a sibling answer
	field before unrelated form fields), falls back to any captcha/code-named input on the page, and
	returns false instead of throwing when nothing usable is found.
	"""
	selector = widget.selector or ''
	return '\n'.join(
		[
			'(() => {',
			'\ttry {',
			f'\t\tconst answer = {json.dumps(answer)};',
			f'\t\tconst selector = {json.dumps(selector)};',
			'\t\tconst visible = (element) => {',
			'\t\t\ttry { return !element.disabled && !element.readOnly && (element.offsetWidth > 0 || element.offsetHeight > 0 || element.getClientRects().length > 0); } catch (error) { return false; }',
			'\t\t};',
			'\t\tconst textish = (element) => { const type = (element.getAttribute("type") || "text").toLowerCase(); return ["text", "search", "tel", "number"].indexOf(type) !== -1; };',
			f'\t\tconst selectors = {_IMAGE_INPUT_SELECTOR_JS};',
			'\t\tconst setValue = (element, value) => {',
			'\t\t\tconst descriptor = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(element), "value");',
			'\t\t\tif (descriptor && descriptor.set) { descriptor.set.call(element, value); } else { element.value = value; }',
			'\t\t\telement.dispatchEvent(new Event("input", {bubbles: true}));',
			'\t\t\telement.dispatchEvent(new Event("change", {bubbles: true}));',
			'\t\t};',
			'\t\tconst nearestInput = (root) => {',
			'\t\t\tif (!root || !root.querySelectorAll) { return null; }',
			'\t\t\tfor (const item of selectors) {',
			'\t\t\t\tfor (const candidate of root.querySelectorAll(item)) {',
			'\t\t\t\t\tif (candidate.tagName === "INPUT" && textish(candidate) && visible(candidate)) { return candidate; }',
			'\t\t\t\t}',
			'\t\t\t}',
			'\t\t\treturn null;',
			'\t\t};',
			"""\t\tconst image = (selector ? document.querySelector(selector) : null) || document.querySelector('img[src*="captcha" i], img[id*="captcha" i], img[name*="captcha" i], img[class*="captcha" i]');""",
			'\t\tif (!image) { return false; }',
			'\t\tlet input = null;',
			"\t\tlet node = image.parentElement || image.closest('form');",
			'\t\tfor (let depth = 0; node && depth < 8 && !input; depth += 1) {',
			'\t\t\tinput = nearestInput(node);',
			'\t\t\tnode = node.parentElement;',
			'\t\t}',
			'\t\tif (!input) { input = nearestInput(document); }',
			'\t\tif (!input) { return false; }',
			'\t\ttry { input.focus(); } catch (error) {}',
			'\t\tsetValue(input, answer);',
			'\t\treturn true;',
			'\t} catch (error) {',
			'\t\treturn false;',
			'\t}',
			'})()',
		]
	)


def injection_js(widget: Widget, token: str) -> str:
	"""JS that delivers a solved token to the page's own form validation.

	Returns '' for unsupported kinds (GeeTest tokens cannot be injected; the caller falls back), and
	delegates image CAPTCHAs to :func:`image_entry_js`. The token is embedded with ``json.dumps``.
	"""
	kind = widget.kind
	if kind in ('recaptcha_v2', 'recaptcha_v3'):
		return _fill_js(token, _RECAPTCHA_FIELDS)
	if kind == 'hcaptcha':
		return _fill_js(token, _HCAPTCHA_FIELDS)
	if kind == 'turnstile':
		callback = widget.data.get('callback')
		return _fill_js(token, _TURNSTILE_FIELDS, str(callback) if callback else None)
	if kind == 'funcaptcha':
		return _fill_js(token, _FUNCAPTCHA_FIELDS)
	if kind == 'geetest':
		return ''  # GeeTest validates a provider-side challenge/validate pair, not an injectable token
	if kind == 'image':
		return image_entry_js(widget, token)
	return ''


# --- solving providers ----------------------------------------------------------------------------

class SolverError(Exception):
	"""A solving provider refused the task, answered unreadably, or timed out."""


@runtime_checkable
class Solver(Protocol):
	"""What the browser layer needs from a solving service."""

	async def solve(self, widget: Widget) -> str | None: ...

	async def solve_image(self, png_base64: str) -> str | None: ...


def _new_http_client(solve_timeout: float) -> httpx.AsyncClient:
	"""The client made when none is injected; the caller of `_run` closes it after the solve."""
	request_timeout = min(_HTTP_TIMEOUT, max(1.0, solve_timeout))
	return httpx.AsyncClient(timeout=request_timeout)


def _json_object(response: httpx.Response, provider: str) -> dict[str, Any]:
	try:
		data = response.json()
	except ValueError as error:
		raise SolverError(f'{provider} returned non-JSON (HTTP {response.status_code})') from error
	if not isinstance(data, dict):
		raise SolverError(f'{provider} returned {type(data).__name__}, expected an object')
	return data


def _message(data: dict[str, Any]) -> str:
	for key in ('errorDescription', 'errorCode', 'request', 'error', 'message'):
		value = data.get(key)
		if isinstance(value, str) and value.strip():
			return value.strip()
	return str(data)[:200]


def _status_ok(value: Any) -> bool:
	return value in (1, '1', True)


# Provider responses nest the solved token differently; these are the keys the APIs are documented to
# use, checked breadth-first so `solution.gRecaptchaResponse` is found as well as a top-level token.
_TOKEN_KEYS = ('gRecaptchaResponse', 'token', 'text', 'validate', 'seccode', 'value', 'answer', 'code')


def _solution_token(value: Any) -> str | None:
	if value is None:
		return None
	queue: list[Any] = [value]
	inspected = 0
	while queue and inspected < 200:
		node = queue.pop(0)
		inspected += 1
		if isinstance(node, dict):
			for key in _TOKEN_KEYS:
				candidate = node.get(key)
				if isinstance(candidate, str) and candidate.strip():
					return candidate.strip()
			queue.extend(item for item in node.values() if isinstance(item, (dict, list)))
		elif isinstance(node, list):
			queue.extend(item for item in node if isinstance(item, (dict, list)))
	return None


def _strip_data_url(png_base64: str) -> str:
	"""`data:image/png;base64,...` -> `...`; providers want the body alone."""
	value = (png_base64 or '').strip()
	if value.lower().startswith('data:'):
		comma = value.find(',')
		value = value[comma + 1:].strip() if comma >= 0 else ''
	return value


_TWO_CAPTCHA_NOT_READY = ('CAPCHA_NOT_READY', 'CAPTCHA_NOT_READY')


def _two_captcha_task(widget: Widget) -> dict[str, Any] | None:
	"""in.php parameters (method plus method params) for a widget, or None when it cannot be solved here."""
	page = widget.page_url
	sitekey = widget.sitekey
	kind = widget.kind
	if kind in ('recaptcha_v2', 'recaptcha_v3'):
		if not sitekey:
			return None
		task: dict[str, Any] = {'method': 'userrecaptcha', 'googlekey': sitekey, 'pageurl': page}
		if kind == 'recaptcha_v3':
			task['version'] = 'v3'
			task['action'] = str(widget.data.get('action') or 'verify')
			min_score = widget.data.get('min_score')
			if min_score not in (None, ''):
				task['min_score'] = str(min_score)
		if widget.data.get('invisible'):
			task['invisible'] = 1
		return task
	if kind == 'hcaptcha':
		if not sitekey:
			return None
		return {'method': 'hcaptcha', 'sitekey': sitekey, 'pageurl': page}
	if kind == 'turnstile':
		if not sitekey:
			return None
		task = {'method': 'turnstile', 'sitekey': sitekey, 'pageurl': page}
		for name in ('action', 'cdata'):
			value = widget.data.get(name)
			if value:
				task[name] = str(value)
		return task
	if kind == 'funcaptcha':
		public_key = sitekey or widget.data.get('public_key')
		if not public_key:
			return None
		task = {'method': 'funcaptcha', 'publickey': public_key, 'pageurl': page}
		blob = widget.data.get('blob')
		if blob:
			task['data'] = str(blob)
		return task
	if kind == 'geetest':
		gt = widget.data.get('gt') or sitekey
		challenge = widget.data.get('challenge')
		if not gt:
			return None
		task = {'method': 'geetest', 'gt': str(gt), 'pageurl': page}
		if challenge:
			task['challenge'] = str(challenge)
		return task
	return None


def _two_captcha_task_id(data: dict[str, Any]) -> str | None:
	"""The task id (in.php) or answer (some resellers answer inline), from a 2captcha response."""
	if not _status_ok(data.get('status')):
		return None
	request_value = data.get('request')
	if isinstance(request_value, (int, float)) and not isinstance(request_value, bool):
		return str(request_value)
	if not isinstance(request_value, str):
		return None
	value = request_value.strip()
	if value and value not in _TWO_CAPTCHA_NOT_READY:
		return value
	return None


class _PollingSolver:
	"""Shared client lifecycle and deadline/poll loop for the hosted solvers."""

	provider = 'solver'

	def __init__(
		self,
		api_key: str,
		*,
		base_url: str,
		timeout: float = 120,
		poll: float = 5,
		client: httpx.AsyncClient | None = None,
	):
		self.api_key = api_key
		self.base_url = base_url.rstrip('/')
		self.timeout = float(timeout)
		self.poll = float(poll)
		self._client = client

	async def _run(self, worker: Callable[[httpx.AsyncClient], Awaitable[str | None]]) -> str | None:
		"""Run one solve, never letting a provider error escape; close only a client created here."""
		client = self._client
		owned = client is None
		if client is None:
			client = _new_http_client(self.timeout)
		try:
			return await worker(client)
		except Exception as error:  # provider errors, bad JSON, transport failures: all return None
			log.debug('%s: solve failed: %s', self.provider, error)
			return None
		finally:
			if owned:
				await client.aclose()

	async def _poll_until_solved(
		self,
		fetch: Callable[[], Awaitable[tuple[str | None, bool]]],
	) -> str | None:
		"""Poll `fetch` (-> (token, pending)) until it has a token, gives up, or the deadline passes."""
		loop = asyncio.get_running_loop()
		deadline = loop.time() + max(self.timeout, 0.0)
		while True:
			token, pending = await fetch()
			if token is not None:
				return token
			if not pending:
				return None
			if loop.time() >= deadline:
				log.debug('%s: no solution within %.0fs', self.provider, self.timeout)
				return None
			await asyncio.sleep(max(self.poll, 0.0))


class TwoCaptcha(_PollingSolver):
	"""2captcha.com client (in.php -> res.php). Resellers speaking the same API work via base_url."""

	provider = '2captcha'

	def __init__(
		self,
		api_key: str,
		*,
		base_url: str = 'https://2captcha.com',
		timeout: float = 120,
		poll: float = 5,
		client: httpx.AsyncClient | None = None,
	):
		super().__init__(api_key, base_url=base_url, timeout=timeout, poll=poll, client=client)

	async def solve(self, widget: Widget) -> str | None:
		task = _two_captcha_task(widget)
		if task is None:
			log.debug('2captcha: no task for widget kind %r', widget.kind)
			return None
		return await self._run(lambda client: self._solve(client, task))

	async def solve_image(self, png_base64: str) -> str | None:
		body = _strip_data_url(png_base64)
		if not body:
			return None
		task = {'method': 'base64', 'body': body}
		return await self._run(lambda client: self._solve(client, task))

	async def _solve(self, client: httpx.AsyncClient, task: dict[str, Any]) -> str | None:
		response = await client.post(f'{self.base_url}/in.php', data={'key': self.api_key, 'json': 1, **task})
		response.raise_for_status()
		data = _json_object(response, '2captcha')
		task_id = _two_captcha_task_id(data)
		if task_id is None:
			raise SolverError(f'2captcha: {_message(data)}')
		return await self._poll(client, task_id)

	async def _poll(self, client: httpx.AsyncClient, task_id: str) -> str | None:
		params = {'key': self.api_key, 'action': 'get', 'id': task_id, 'json': 1}

		async def fetch() -> tuple[str | None, bool]:
			response = await client.get(f'{self.base_url}/res.php', params=params)
			response.raise_for_status()
			data = _json_object(response, '2captcha')
			request_value = data.get('request')
			if _status_ok(data.get('status')):
				return _two_captcha_task_id(data), False
			if isinstance(request_value, str) and request_value.strip() in _TWO_CAPTCHA_NOT_READY:
				return None, True
			raise SolverError(f'2captcha: {_message(data)}')

		return await self._poll_until_solved(fetch)


def _capsolver_error(data: dict[str, Any]) -> bool:
	return data.get('errorId') not in (None, 0, '0')


def _capsolver_task(widget: Widget) -> dict[str, Any] | None:
	"""The `task` object for createTask, or None when the widget cannot be solved here."""
	page = widget.page_url
	sitekey = widget.sitekey
	kind = widget.kind
	if kind == 'recaptcha_v2':
		if not sitekey:
			return None
		task: dict[str, Any] = {'type': 'ReCaptchaV2TaskProxyLess', 'websiteURL': page, 'websiteKey': sitekey}
		if widget.data.get('invisible'):
			task['isInvisible'] = True
		return task
	if kind == 'recaptcha_v3':
		if not sitekey:
			return None
		task = {
			'type': 'ReCaptchaV3TaskProxyLess',
			'websiteURL': page,
			'websiteKey': sitekey,
			'pageAction': str(widget.data.get('action') or 'verify'),
		}
		min_score = widget.data.get('min_score')
		if min_score not in (None, ''):
			try:
				task['minScore'] = float(min_score)
			except (TypeError, ValueError):
				pass
		if widget.data.get('enterprise'):
			task['isEnterprise'] = True
		return task
	if kind == 'hcaptcha':
		if not sitekey:
			return None
		return {'type': 'HCaptchaTaskProxyLess', 'websiteURL': page, 'websiteKey': sitekey}
	if kind == 'turnstile':
		if not sitekey:
			return None
		task = {'type': 'AntiTurnstileTaskProxyLess', 'websiteURL': page, 'websiteKey': sitekey}
		metadata = {name: str(widget.data[name]) for name in ('action', 'cdata') if widget.data.get(name)}
		if metadata:
			task['metadata'] = metadata
		return task
	if kind == 'funcaptcha':
		public_key = sitekey or widget.data.get('public_key')
		if not public_key:
			return None
		task = {'type': 'FunCaptchaTaskProxyLess', 'websiteURL': page, 'websitePublicKey': public_key}
		blob = widget.data.get('blob')
		if blob:
			task['data'] = str(blob)
		return task
	if kind == 'geetest':
		gt = widget.data.get('gt') or sitekey
		challenge = widget.data.get('challenge')
		if not gt:
			return None
		task = {'type': 'GeeTestTaskProxyLess', 'websiteURL': page, 'gt': str(gt)}
		if challenge:
			task['challenge'] = str(challenge)
		return task
	return None


class CapSolver(_PollingSolver):
	"""api.capsolver.com client (createTask -> getTaskResult)."""

	provider = 'capsolver'

	def __init__(
		self,
		api_key: str,
		*,
		base_url: str = 'https://api.capsolver.com',
		timeout: float = 120,
		poll: float = 5,
		client: httpx.AsyncClient | None = None,
	):
		super().__init__(api_key, base_url=base_url, timeout=timeout, poll=poll, client=client)

	async def solve(self, widget: Widget) -> str | None:
		task = _capsolver_task(widget)
		if task is None:
			log.debug('capsolver: no task for widget kind %r', widget.kind)
			return None
		return await self._run(lambda client: self._solve(client, task))

	async def solve_image(self, png_base64: str) -> str | None:
		body = _strip_data_url(png_base64)
		if not body:
			return None
		task = {'type': 'ImageToTextTask', 'body': body}
		return await self._run(lambda client: self._solve(client, task))

	async def _solve(self, client: httpx.AsyncClient, task: dict[str, Any]) -> str | None:
		response = await client.post(f'{self.base_url}/createTask', json={'clientKey': self.api_key, 'task': task})
		response.raise_for_status()
		data = _json_object(response, 'capsolver')
		if _capsolver_error(data):
			raise SolverError(f'capsolver: {_message(data)}')
		task_id = data.get('taskId')
		if isinstance(task_id, (int, float)) and not isinstance(task_id, bool):
			task_id = str(task_id)
		if not isinstance(task_id, str) or not task_id.strip():
			raise SolverError(f'capsolver: no taskId in {str(data)[:200]}')
		return await self._poll(client, task_id.strip())

	async def _poll(self, client: httpx.AsyncClient, task_id: str) -> str | None:
		payload = {'clientKey': self.api_key, 'taskId': task_id}

		async def fetch() -> tuple[str | None, bool]:
			response = await client.post(f'{self.base_url}/getTaskResult', json=payload)
			response.raise_for_status()
			data = _json_object(response, 'capsolver')
			if _capsolver_error(data):
				raise SolverError(f'capsolver: {_message(data)}')
			status = str(data.get('status') or '').lower()
			if status == 'ready':
				token = _solution_token(data.get('solution')) or _solution_token(data)
				if token:
					return token, False
				raise SolverError(f'capsolver: ready without a token ({str(data)[:200]})')
			if status in ('processing', 'idle', ''):
				return None, True
			raise SolverError(f'capsolver: unexpected status {status!r}')

		return await self._poll_until_solved(fetch)


def make_solver(provider: str | None, api_key: str | None, *, timeout: float = 120) -> Solver | None:
	"""Build the configured solver, or None when the provider is unknown or no key is set."""
	name = (provider or '').strip().lower()
	key = (api_key or '').strip()
	if not name or not key:
		return None
	if name in ('2captcha', 'twocaptcha'):
		return TwoCaptcha(key, timeout=timeout)
	if name == 'capsolver':
		return CapSolver(key, timeout=timeout)
	return None
