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
	kind: str  # 'challenge' may clear on its own after a few seconds; 'block' will not
	evidence: str


# (vendor, kind, where, pattern). `where` is 'title', 'html' or 'url'.
_RULES: list[tuple[str, str, str, re.Pattern[str]]] = [
	('cloudflare', 'challenge', 'title', re.compile(r'^\s*just a moment\.\.\.\s*$', re.I)),
	('cloudflare', 'challenge', 'html', re.compile(r'/cdn-cgi/challenge-platform/.*?orchestrate|cf-chl-widget|id="challenge-form"', re.I)),
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


# Read-only probe run in an isolated world (see session.py) so the page can't observe it.
PAGE_PROBE_JS = "JSON.stringify({url: location.href, title: document.title, html: document.documentElement ? document.documentElement.outerHTML.slice(0, 200000) : ''})"
