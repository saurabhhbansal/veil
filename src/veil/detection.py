"""Recognise anti-bot challenge and block pages, so a session can rotate identity instead of flailing.

``classify`` is a pure function over (url, title, html) so it's cheap to unit-test. The
signatures are deliberately conservative: a false positive burns a healthy proxy.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class BlockSignal:
	vendor: str  # cloudflare | akamai | perimeterx | datadome | imperva | google | generic
	kind: str  # 'challenge' may clear on its own after a few seconds; 'block' will not; 'captcha' needs a person
	evidence: str


# (vendor, kind, where, pattern). `where` is 'title', 'html' or 'url'.
_RULES: list[tuple[str, str, str, re.Pattern[str]]] = [
	('cloudflare', 'challenge', 'title', re.compile(r'^\s*just a moment\.\.\.\s*$', re.I)),
	# Not `cf-chl-widget`: that is the iframe id of any embedded Turnstile (login and signup forms), not a challenge page.
	('cloudflare', 'challenge', 'html', re.compile(r'/cdn-cgi/challenge-platform/[^"\'\s]*orchestrate|id="challenge-form"', re.I)),
	('cloudflare', 'block', 'title', re.compile(r'attention required! \| cloudflare', re.I)),
	('cloudflare', 'block', 'html', re.compile(r'cf-error-details|error code:?\s*1020', re.I)),
	('akamai', 'block', 'html', re.compile(r'errors\.edgesuite\.net|Reference\s*#\d+\.[0-9a-f]+\.\d+', re.I)),
	('perimeterx', 'challenge', 'html', re.compile(r'px-captcha|press\s*&(amp;)?\s*hold', re.I)),
	('datadome', 'challenge', 'html', re.compile(r'captcha-delivery\.com|geo\.captcha-delivery', re.I)),
	('imperva', 'block', 'html', re.compile(r'incapsula incident id|_Incapsula_Resource', re.I)),
	('google', 'block', 'url', re.compile(r'^https?://(www\.)?google\.[a-z.]+/sorry/', re.I)),
	('google', 'block', 'html', re.compile(r'our systems have detected unusual traffic', re.I)),
	('generic', 'block', 'title', re.compile(r'^\s*(access denied|403 forbidden|429 too many requests|request blocked)\s*$', re.I)),
]


def classify(url: str, title: str, html: str) -> BlockSignal | None:
	fields = {'url': url or '', 'title': title or '', 'html': (html or '')[:200_000]}
	for vendor, kind, where, pattern in _RULES:
		m = pattern.search(fields[where])
		if m:
			return BlockSignal(vendor=vendor, kind=kind, evidence=f'{where}: {m.group(0)[:80]!r}')
	return None


# Interactive CAPTCHA widgets a person has to solve. Invisible variants (reCAPTCHA v3 / invisible, hCaptcha
# invisible, a Turnstile that passes on its own) are deliberately left out: they sit on ordinary login and
# signup pages and need nobody. If one of those does turn interactive, the agent asks for help itself.
_CAPTCHA_RULES: list[tuple[str, re.Pattern[str]]] = [
	('recaptcha', re.compile(r'recaptcha/(?:api2|enterprise)/anchor\?[^"\'\s>]*size=(?:normal|compact)', re.I)),
	('hcaptcha', re.compile(r'hcaptcha\.com/captcha/v1/[^"\'\s>]*frame=checkbox(?!-invisible)', re.I)),
	('arkose', re.compile(r'client-api\.arkoselabs\.com|funcaptcha\.com/fc/|id="FunCaptcha"', re.I)),
	('geetest', re.compile(r'class="geetest_(?:holder|panel|radar)', re.I)),
	('aws-waf', re.compile(r'captcha\.awswaf\.com|AwsWafCaptcha', re.I)),
	('amazon', re.compile(r'/errors/validateCaptcha', re.I)),
	('text-captcha', re.compile(r'<img[^>]+(?:id|name|class)="[^"]*captcha[^"]*"[^>]*>', re.I)),
]


def find_captcha(html: str) -> BlockSignal | None:
	"""An interactive CAPTCHA on the page, if any (kind 'captcha'). Pure, like `classify`."""
	html = (html or '')[:200_000]
	for vendor, pattern in _CAPTCHA_RULES:
		m = pattern.search(html)
		if m:
			return BlockSignal(vendor=vendor, kind='captcha', evidence=f'html: {m.group(0)[:80]!r}')
	return None


# Read-only probe run in an isolated world (see session.py) so the page can't observe it.
PAGE_PROBE_JS = "JSON.stringify({url: location.href, title: document.title, html: document.documentElement ? document.documentElement.outerHTML.slice(0, 200000) : ''})"
