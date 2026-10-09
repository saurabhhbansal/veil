import pytest

from veil.detection import classify


@pytest.mark.parametrize(
	'url, title, html, vendor, kind',
	[
		('https://x.com/', 'Just a moment...', '', 'cloudflare', 'challenge'),
		('https://x.com/', 'x', '<script src="/cdn-cgi/challenge-platform/h/g/orchestrate/chl_page/v1"></script>', 'cloudflare', 'challenge'),
		('https://x.com/', 'Attention Required! | Cloudflare', '', 'cloudflare', 'block'),
		('https://aa.com/', 'Access Denied', 'You don\'t have permission. Reference #18.5c3e1402.1712345678.1a2b3c', 'akamai', 'block'),
		('https://x.com/', 'x', '<div id="px-captcha"></div>', 'perimeterx', 'challenge'),
		('https://x.com/', 'x', '<iframe src="https://geo.captcha-delivery.com/captcha/"></iframe>', 'datadome', 'challenge'),
		('https://x.com/', 'x', 'Incapsula incident ID: 123', 'imperva', 'block'),
		('https://www.google.com/sorry/index?continue=', 'x', '', 'google', 'block'),
		('https://x.com/', '403 Forbidden', '', 'generic', 'block'),
	],
)
def test_detects(url, title, html, vendor, kind):
	sig = classify(url, title, html)
	assert sig and (sig.vendor, sig.kind) == (vendor, kind)


@pytest.mark.parametrize(
	'title, html',
	[
		('Cloudflare Blog', 'We wrote about how to wait for just a moment... before deploying.'),
		('Access control lists explained', 'Access denied errors happen when...'),
		('Example Domain', '<h1>Example Domain</h1>'),
		# a login form with an embedded Turnstile widget is not a challenge page
		('Sign in', '<form><div class="cf-turnstile"><iframe id="cf-chl-widget-a1b2c" src="https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/turnstile/if/ov2/av0/rcv/x"></iframe></div></form>'),
		('Shop', '<script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js"></script><p>orchestrate your wardrobe</p>'),
	],
)
def test_ignores_normal_pages(title, html):
	assert classify('https://example.com/', title, html) is None


def test_timezone_aliases_compare_equal():
	from veil.check import same_tz

	assert same_tz('Asia/Calcutta', 'Asia/Kolkata')
	assert same_tz('Europe/Berlin', 'Europe/Berlin')
	assert not same_tz('Europe/Berlin', 'Europe/Paris')
