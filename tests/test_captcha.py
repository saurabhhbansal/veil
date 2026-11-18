"""captcha.py: widget detection, token-injection JavaScript, and the 2captcha / CapSolver clients.

Everything here is offline. Detection and the JS builders are pure. The solver clients run against
``httpx.MockTransport`` with poll=0 (or a tiny poll) and short deadlines, so no real network and no
real sleeps are involved; assertions are on the exact requests the clients sent.
"""
import json
import time
from urllib.parse import parse_qsl

import httpx
import pytest

from veil import captcha
from veil.captcha import (
	CapSolver,
	Solver,
	SolverError,
	TwoCaptcha,
	Widget,
	detect,
	image_entry_js,
	injection_js,
	make_solver,
)

URL = 'https://shop.test/login'
V2_KEY = '6Lc-v2-test_key'
V3_KEY = '6Lc-v3-test_key'
HCAPTCHA_KEY = '10000000-ffff-ffff-ffff-000000000001'
TURNSTILE_KEY = '0x4AAAAAAA-test-key'
FUNCAPTCHA_KEY = '476068BF-9607-4799-B53D-966BE98E2B81'
GT = '0123456789abcdef0123456789abcdef'
CHALLENGE = 'fedcba9876543210fedcba9876543210'
PNG = 'iVBORw0KGgoAAAANSUhEUg=='


def mock_client(handler) -> httpx.AsyncClient:
	return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --- detection: reCAPTCHA -------------------------------------------------------------------------

def test_detect_recaptcha_v2_data_sitekey():
	html = f'<form><div class="g-recaptcha" data-sitekey="{V2_KEY}"></div><script src="https://www.google.com/recaptcha/api.js"></script></form>'
	assert detect(html, URL) == Widget('recaptcha_v2', V2_KEY, URL)


def test_detect_recaptcha_v2_sitekey_from_explicit_render_js():
	html = (
		'<div class="g-recaptcha" id="rc"></div><script src="https://www.google.com/recaptcha/api.js?render=explicit"></script>'
		f'<script>grecaptcha.render("rc", {{sitekey: "{V2_KEY}"}});</script>'
	)
	widget = detect(html, URL)
	assert (widget.kind, widget.sitekey) == ('recaptcha_v2', V2_KEY)


def test_detect_recaptcha_v2_from_iframe_sitekey():
	html = f'<iframe src="https://www.google.com/recaptcha/api2/anchor?ar=1&k={V2_KEY}&co=aHR0cHM6"></iframe>'
	widget = detect(html, URL)
	assert (widget.kind, widget.sitekey) == ('recaptcha_v2', V2_KEY)


def test_detect_recaptcha_enterprise_anchor_is_v2():
	html = f'<iframe src="https://www.google.com/recaptcha/enterprise/anchor?ar=1&k={V2_KEY}"></iframe>'
	widget = detect(html)
	assert (widget.kind, widget.sitekey, widget.page_url) == ('recaptcha_v2', V2_KEY, '')


def test_detect_recaptcha_v3_from_execute():
	html = (
		f'<script src="https://www.google.com/recaptcha/api.js?render={V3_KEY}"></script>'
		f'<script>grecaptcha.ready(function () {{ grecaptcha.execute("{V3_KEY}", {{action: "login"}}) }});</script>'
	)
	widget = detect(html, URL)
	assert (widget.kind, widget.sitekey) == ('recaptcha_v3', V3_KEY)


def test_detect_recaptcha_v3_enterprise():
	html = (
		f'<script src="https://www.google.com/recaptcha/enterprise.js?render={V3_KEY}"></script>'
		f'<script>grecaptcha.enterprise.ready(function () {{ grecaptcha.enterprise.execute("{V3_KEY}", {{action: "signup"}}) }});</script>'
	)
	widget = detect(html, URL)
	assert (widget.kind, widget.sitekey) == ('recaptcha_v3', V3_KEY)


def test_detect_interactive_recaptcha_wins_over_v3_usage():
	html = (
		f'<div class="g-recaptcha" data-sitekey="{V2_KEY}"></div>'
		f'<script>grecaptcha.ready(function () {{ grecaptcha.execute("{V3_KEY}", {{action: "x"}}) }});</script>'
	)
	assert detect(html, URL).kind == 'recaptcha_v2'


def test_detect_recaptcha_needs_a_sitekey():
	assert detect('<div class="g-recaptcha"></div>') is None
	# the solved-response field alone is not a widget
	assert detect('<textarea name="g-recaptcha-response" id="g-recaptcha-response"></textarea>') is None
	# api.js alone (no widget, no render key) is not a confident widget
	assert detect('<script src="https://www.google.com/recaptcha/api.js"></script>') is None
	assert detect('<p>the g-recaptcha docs explain render=explicit</p>') is None


# --- detection: hCaptcha, Turnstile, FunCaptcha, GeeTest ------------------------------------------

def test_detect_hcaptcha_data_sitekey():
	html = f'<div class="h-captcha" data-sitekey="{HCAPTCHA_KEY}"></div><script src="https://js.hcaptcha.com/1/api.js" async defer></script>'
	widget = detect(html, URL)
	assert (widget.kind, widget.sitekey) == ('hcaptcha', HCAPTCHA_KEY)


def test_detect_hcaptcha_from_iframe_sitekey():
	html = f'<iframe src="https://newassets.hcaptcha.com/captcha/v1/abc/static/hcaptcha.html#frame=checkbox&sitekey={HCAPTCHA_KEY}"></iframe>'
	assert detect(html).sitekey == HCAPTCHA_KEY


def test_detect_hcaptcha_response_field_alone_is_not_a_widget():
	assert detect('<textarea id="h-captcha-response" name="h-captcha-response"></textarea>') is None


def test_detect_turnstile_and_callback():
	html = (
		f'<div class="cf-turnstile" data-sitekey="{TURNSTILE_KEY}" data-callback="onTurnstileSolved"></div>'
		f'<script src="https://challenges.cloudflare.com/turnstile/v0/api.js"></script>'
	)
	widget = detect(html, URL)
	assert (widget.kind, widget.sitekey) == ('turnstile', TURNSTILE_KEY)
	assert widget.data['callback'] == 'onTurnstileSolved'


def test_detect_turnstile_from_iframe_sitekey():
	html = f'<iframe src="https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/turnstile/if/ov2/av0/rcv/xyz?sitekey={TURNSTILE_KEY}"></iframe>'
	widget = detect(html)
	assert (widget.kind, widget.sitekey) == ('turnstile', TURNSTILE_KEY)
	assert 'callback' not in widget.data


def test_detect_turnstile_needs_a_sitekey():
	assert detect('<div class="cf-turnstile"></div>') is None
	assert detect('<input type="hidden" name="cf-turnstile-response" value="">') is None


def test_detect_funcaptcha_data_pkey():
	html = f'<div id="funcaptcha" data-pkey="{FUNCAPTCHA_KEY}"></div><script src="https://client-api.arkoselabs.com/v2/abc/api.js"></script>'
	widget = detect(html, URL)
	assert (widget.kind, widget.sitekey) == ('funcaptcha', FUNCAPTCHA_KEY)
	assert widget.data['public_key'] == FUNCAPTCHA_KEY


def test_detect_funcaptcha_public_key_from_enforcement_js():
	html = (
		'<script src="https://client-api.arkoselabs.com/v2/abc/api.js"></script>'
		f'<script>new ArkoseEnforcement({{public_key: "{FUNCAPTCHA_KEY}", data: {{blob: "blob-1"}}}});</script>'
	)
	widget = detect(html, URL)
	assert (widget.kind, widget.sitekey) == ('funcaptcha', FUNCAPTCHA_KEY)
	assert widget.data['blob'] == 'blob-1'


def test_detect_funcaptcha_data_pkey_separate_from_the_widget_element():
	# the public key can sit on a different node than the one carrying the vendor marker
	html = f'<div data-pkey="{FUNCAPTCHA_KEY}"></div><div id="arkose"></div>'
	widget = detect(html, URL)
	assert (widget.kind, widget.sitekey) == ('funcaptcha', FUNCAPTCHA_KEY)


def test_detect_funcaptcha_needs_a_public_key():
	assert detect('<p>Powered by Arkose Labs</p>') is None
	assert detect('<script src="https://funcaptcha.com/fc/api/nojs/?pkey="></script>') is None


def test_detect_geetest_gt_and_challenge():
	html = f'<div class="geetest_holder"><script>initGeetest({{gt: "{GT}", challenge: "{CHALLENGE}"}}, function (captcha) {{}});</script></div>'
	widget = detect(html, URL)
	assert widget == Widget('geetest', None, URL, data={'gt': GT, 'challenge': CHALLENGE})


def test_detect_geetest_from_query_string():
	html = f'<script src="https://static.geetest.com/static/js/gt.0.4.9.js?gt={GT}&challenge={CHALLENGE}"></script>'
	widget = detect(html)
	assert widget.kind == 'geetest'
	assert widget.data == {'gt': GT, 'challenge': CHALLENGE}


def test_detect_geetest_from_data_attributes():
	html = f'<div class="geetest_captcha" data-gt="{GT}" data-challenge="{CHALLENGE}"></div>'
	widget = detect(html)
	assert widget.kind == 'geetest'
	assert widget.data == {'gt': GT, 'challenge': CHALLENGE}


def test_detect_geetest_marker_without_values_is_not_confident():
	assert detect('<div class="geetest_holder"></div>') is None
	assert detect('<p>we use geetest.com widgets</p>') is None


# --- detection: image CAPTCHAs --------------------------------------------------------------------

def test_detect_image_from_id():
	html = '<form><img id="captchaImg" src="/captcha.png"><input type="text" name="captcha"></form>'
	widget = detect(html, URL)
	assert (widget.kind, widget.selector, widget.sitekey) == ('image', '#captchaImg', None)
	assert widget.page_url == URL


def test_detect_image_from_unambiguous_class():
	widget = detect('<img class="captcha-image" src="/captcha.png">', URL)
	assert (widget.kind, widget.selector) == ('image', 'img.captcha-image')


def test_detect_image_needs_an_unambiguous_selector():
	# "captcha" in the src but no id/class to select it by
	assert detect('<img src="/captcha.php">') is None
	assert detect('<img name="captcha" src="/x.png">') is None
	# the same class on two images is ambiguous
	assert detect('<img class="captcha" src="/a.png"><img class="captcha" src="/b.png">') is None


def test_token_widget_wins_over_an_image():
	html = f'<img src="/recaptcha-logo.png"><div class="g-recaptcha" data-sitekey="{V2_KEY}"></div>'
	assert detect(html, URL).kind == 'recaptcha_v2'


# --- detection: conservative defaults -------------------------------------------------------------

@pytest.mark.parametrize(
	'html',
	[
		'',
		'<html><body><p>Nothing here.</p></body></html>',
		'<div data-sitekey="orphan-key"></div>',
		'<a href="https://hcaptcha.com">hCaptcha</a>',
		'<textarea name="h-captcha-response"></textarea>',
		'<textarea name="g-recaptcha-response"></textarea>',
	],
)
def test_detect_is_conservative(html):
	assert detect(html, URL) is None


def test_detect_only_reads_the_first_200k_chars():
	head = '<p>' + 'x' * 200_000 + '</p>'
	assert detect(head + f'<div class="g-recaptcha" data-sitekey="{V2_KEY}"></div>') is None
	widget = detect(f'<div class="g-recaptcha" data-sitekey="{V2_KEY}"></div>' + 'x' * 300_000)
	assert widget and widget.sitekey == V2_KEY


# --- injection JavaScript -------------------------------------------------------------------------

@pytest.mark.parametrize(
	'widget',
	[
		Widget('recaptcha_v2', V2_KEY, URL),
		Widget('recaptcha_v3', V3_KEY, URL),
		Widget('hcaptcha', HCAPTCHA_KEY, URL),
		Widget('turnstile', TURNSTILE_KEY, URL),
		Widget('funcaptcha', FUNCAPTCHA_KEY, URL),
		Widget('image', None, URL, selector='#captchaImg'),
	],
)
def test_injection_js_fills_the_form_and_dispatches_events(widget):
	js = injection_js(widget, 'SOLVED-TOKEN')
	assert json.dumps('SOLVED-TOKEN') in js
	assert 'dispatchEvent' in js
	assert 'new Event("input"' in js
	assert 'new Event("change"' in js
	assert 'querySelectorAll' in js


def test_injection_recaptcha_uses_the_response_fields():
	js = injection_js(Widget('recaptcha_v2', V2_KEY, URL), 'TOKEN')
	assert 'textarea[name="g-recaptcha-response"]' in js
	assert '#g-recaptcha-response' in js
	assert 'textarea[id^="g-recaptcha-response"]' in js


def test_injection_recaptcha_v3_uses_the_same_fields():
	js = injection_js(Widget('recaptcha_v3', V3_KEY, URL), 'TOKEN')
	assert '#g-recaptcha-response' in js
	assert json.dumps('TOKEN') in js


def test_injection_hcaptcha_fields():
	js = injection_js(Widget('hcaptcha', HCAPTCHA_KEY, URL), 'TOKEN')
	assert 'textarea[name="h-captcha-response"]' in js
	assert 'textarea[name="g-recaptcha-response"]' in js
	assert 'textarea[id^="h-captcha-response"]' in js


def test_injection_funcaptcha_fields():
	js = injection_js(Widget('funcaptcha', FUNCAPTCHA_KEY, URL), 'TOKEN')
	assert '#FunCaptcha-Token' in js
	assert 'input[name="fc-token"]' in js


def test_injection_turnstile_fields_and_callback():
	widget = Widget('turnstile', TURNSTILE_KEY, URL, data={'callback': 'onTurnstileSolved'})
	js = injection_js(widget, 'TOKEN')
	assert 'input[name="cf-turnstile-response"]' in js
	assert 'textarea[name="cf-turnstile-response"]' in js
	assert json.dumps('onTurnstileSolved') in js
	assert 'window[' in js
	assert 'typeof' in js
	assert 'notify(token)' in js
	assert 'try' in js and 'catch' in js


def test_injection_turnstile_without_callback_does_not_call_anything():
	js = injection_js(Widget('turnstile', TURNSTILE_KEY, URL), 'TOKEN')
	assert 'window[' not in js


def test_injection_unsupported_kinds_are_empty():
	assert injection_js(Widget('geetest', GT, URL, data={'gt': GT}), 'TOKEN') == ''
	assert injection_js(Widget('unknown', 'key', URL), 'TOKEN') == ''


def test_injection_delegates_image_widgets():
	widget = Widget('image', None, URL, selector='#captchaImg')
	assert injection_js(widget, 'ANSWER') == image_entry_js(widget, 'ANSWER')
	js = injection_js(widget, 'ANSWER')
	assert json.dumps('ANSWER') in js
	assert json.dumps('#captchaImg') in js


def test_injection_quotes_the_token_through_json():
	token = 'to"ken\\with\'quotes</script>'
	js = injection_js(Widget('hcaptcha', HCAPTCHA_KEY, URL), token)
	assert json.dumps(token) in js
	assert token not in js
	assert 'to"ken' not in js


def test_image_entry_js_targets_the_image_and_its_input():
	js = image_entry_js(Widget('image', None, URL, selector='img.captcha-image'), 'A"1\'2')
	assert json.dumps('A"1\'2') in js
	assert json.dumps('img.captcha-image') in js
	assert 'querySelector' in js
	assert 'dispatchEvent' in js and 'new Event("input"' in js and 'new Event("change"' in js
	assert 'closest' in js and 'form' in js
	assert 'return false' in js and 'return true' in js


def test_image_entry_js_has_a_defensive_fallback_without_a_selector():
	js = image_entry_js(Widget('image', None, URL), 'abc')
	assert 'const selector = ""' in js
	assert 'img[src*="captcha" i]' in js
	assert 'return false' in js


def test_solver_error_is_a_normal_exception():
	assert issubclass(SolverError, Exception)
	# the Solver protocol is runtime-checkable, so callers can isinstance a built solver
	assert isinstance(TwoCaptcha('key'), Solver)
	assert isinstance(CapSolver('key'), Solver)
	assert not isinstance(object(), Solver)


# --- 2captcha -------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_two_captcha_solve_sequence():
	requests = []
	state = {'polls': 0}

	def handler(request: httpx.Request) -> httpx.Response:
		requests.append(request)
		if request.url.path == '/in.php':
			return httpx.Response(200, json={'status': 1, 'request': 'task-42'})
		if request.url.path == '/res.php':
			state['polls'] += 1
			if state['polls'] == 1:
				return httpx.Response(200, json={'status': 0, 'request': 'CAPCHA_NOT_READY'})
			return httpx.Response(200, json={'status': 1, 'request': 'SOLVED-TOKEN'})
		return httpx.Response(404)

	async with mock_client(handler) as client:
		solver = TwoCaptcha('api-key', base_url='http://2captcha.test', poll=0.001, timeout=2, client=client)
		assert await solver.solve(Widget('recaptcha_v2', V2_KEY, URL)) == 'SOLVED-TOKEN'
		assert not client.is_closed  # an injected client belongs to the caller

	assert [request.url.path for request in requests] == ['/in.php', '/res.php', '/res.php']
	in_form = dict(parse_qsl(requests[0].content.decode()))
	assert in_form == {'key': 'api-key', 'json': '1', 'method': 'userrecaptcha', 'googlekey': V2_KEY, 'pageurl': URL}
	assert dict(requests[1].url.params) == {'key': 'api-key', 'action': 'get', 'id': 'task-42', 'json': '1'}


@pytest.mark.asyncio
@pytest.mark.parametrize(
	'widget, expected',
	[
		(Widget('recaptcha_v2', 'key-1', URL), {'method': 'userrecaptcha', 'googlekey': 'key-1', 'pageurl': URL}),
		(
			Widget('recaptcha_v2', 'key-2', URL, data={'invisible': True}),
			{'method': 'userrecaptcha', 'googlekey': 'key-2', 'pageurl': URL, 'invisible': '1'},
		),
		(
			Widget('recaptcha_v3', 'key-3', URL, data={'action': 'signup', 'min_score': 0.7}),
			{'method': 'userrecaptcha', 'googlekey': 'key-3', 'pageurl': URL, 'version': 'v3', 'action': 'signup', 'min_score': '0.7'},
		),
		(
			Widget('recaptcha_v3', 'key-4', URL),
			{'method': 'userrecaptcha', 'googlekey': 'key-4', 'pageurl': URL, 'version': 'v3', 'action': 'verify'},
		),
		(Widget('hcaptcha', 'key-5', URL), {'method': 'hcaptcha', 'sitekey': 'key-5', 'pageurl': URL}),
		(Widget('turnstile', 'key-6', URL), {'method': 'turnstile', 'sitekey': 'key-6', 'pageurl': URL}),
		(
			Widget('funcaptcha', 'key-7', URL, data={'blob': 'blob-1'}),
			{'method': 'funcaptcha', 'publickey': 'key-7', 'pageurl': URL, 'data': 'blob-1'},
		),
		(
			Widget('funcaptcha', None, URL, data={'public_key': 'key-8'}),
			{'method': 'funcaptcha', 'publickey': 'key-8', 'pageurl': URL},
		),
		(
			Widget('geetest', None, URL, data={'gt': GT, 'challenge': CHALLENGE}),
			{'method': 'geetest', 'gt': GT, 'challenge': CHALLENGE, 'pageurl': URL},
		),
	],
)
async def test_two_captcha_maps_widgets_to_methods(widget, expected):
	forms = []

	def handler(request: httpx.Request) -> httpx.Response:
		if request.url.path == '/in.php':
			forms.append(dict(parse_qsl(request.content.decode())))
			return httpx.Response(200, json={'status': 1, 'request': 'task-1'})
		return httpx.Response(200, json={'status': 1, 'request': 'TOKEN'})

	async with mock_client(handler) as client:
		solver = TwoCaptcha('key', base_url='http://2captcha.test', poll=0, timeout=2, client=client)
		assert await solver.solve(widget) == 'TOKEN'

	form = forms[0]
	assert form['key'] == 'key' and form['json'] == '1'
	assert set(form) == {'key', 'json'} | set(expected)
	for name, value in expected.items():
		assert form[name] == value


@pytest.mark.asyncio
async def test_two_captcha_image_solve_uses_base64_method():
	forms = []
	responses = {'polls': 0}

	def handler(request: httpx.Request) -> httpx.Response:
		if request.url.path == '/in.php':
			forms.append(dict(parse_qsl(request.content.decode())))
			return httpx.Response(200, json={'status': 1, 'request': f'image-task-{len(forms)}'})
		responses['polls'] += 1
		return httpx.Response(200, json={'status': 1, 'request': 'AB12CD'})

	async with mock_client(handler) as client:
		solver = TwoCaptcha('key', base_url='http://2captcha.test', poll=0, timeout=2, client=client)
		assert await solver.solve_image(PNG) == 'AB12CD'
		assert await solver.solve_image('data:image/png;base64,' + PNG) == 'AB12CD'

	assert forms[0]['method'] == 'base64' and forms[0]['body'] == PNG
	assert forms[1]['body'] == PNG  # data-URL prefix stripped


@pytest.mark.asyncio
async def test_two_captcha_refuses_unsolvable_widgets_without_network():
	requests = []

	def handler(request: httpx.Request) -> httpx.Response:
		requests.append(request)
		return httpx.Response(500)

	async with mock_client(handler) as client:
		solver = TwoCaptcha('key', base_url='http://2captcha.test', client=client)
		assert await solver.solve(Widget('image', None, URL, selector='#captcha')) is None
		assert await solver.solve(Widget('hcaptcha', None, URL)) is None
		assert await solver.solve(Widget('geetest', None, URL, data={'challenge': CHALLENGE})) is None
		assert await solver.solve_image('') is None
	assert requests == []


@pytest.mark.asyncio
async def test_two_captcha_returns_none_on_provider_errors():
	def wrong_key(request: httpx.Request) -> httpx.Response:
		return httpx.Response(200, json={'status': 0, 'request': 'ERROR_WRONG_USER_KEY'})

	async with mock_client(wrong_key) as client:
		solver = TwoCaptcha('bad', base_url='http://2captcha.test', poll=0, timeout=0.05, client=client)
		assert await solver.solve(Widget('recaptcha_v2', V2_KEY, URL)) is None

	def poll_error(request: httpx.Request) -> httpx.Response:
		if request.url.path == '/in.php':
			return httpx.Response(200, json={'status': 1, 'request': 'task-1'})
		return httpx.Response(200, json={'status': 0, 'request': 'ERROR_CAPTCHA_UNSOLVABLE'})

	async with mock_client(poll_error) as client:
		solver = TwoCaptcha('key', base_url='http://2captcha.test', poll=0, timeout=0.05, client=client)
		assert await solver.solve(Widget('recaptcha_v2', V2_KEY, URL)) is None

	def not_json(request: httpx.Request) -> httpx.Response:
		return httpx.Response(200, text='<html>bad gateway</html>')

	async with mock_client(not_json) as client:
		solver = TwoCaptcha('key', base_url='http://2captcha.test', poll=0, timeout=0.05, client=client)
		assert await solver.solve(Widget('recaptcha_v2', V2_KEY, URL)) is None

	def down(request: httpx.Request) -> httpx.Response:
		raise httpx.ConnectError('connection refused')

	async with mock_client(down) as client:
		solver = TwoCaptcha('key', base_url='http://2captcha.test', poll=0, timeout=0.05, client=client)
		assert await solver.solve(Widget('recaptcha_v2', V2_KEY, URL)) is None


@pytest.mark.asyncio
async def test_two_captcha_returns_none_on_timeout():
	polls = []

	def handler(request: httpx.Request) -> httpx.Response:
		if request.url.path == '/in.php':
			return httpx.Response(200, json={'status': 1, 'request': 'task-1'})
		polls.append(request)
		return httpx.Response(200, json={'status': 0, 'request': 'CAPCHA_NOT_READY'})

	async with mock_client(handler) as client:
		solver = TwoCaptcha('key', base_url='http://2captcha.test', poll=0.002, timeout=0.05, client=client)
		started = time.monotonic()
		assert await solver.solve(Widget('recaptcha_v2', V2_KEY, URL)) is None
		assert time.monotonic() - started < 2
	assert polls  # it did try more than once before giving up


# --- CapSolver ------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_capsolver_solve_sequence():
	requests = []
	state = {'polls': 0}

	def handler(request: httpx.Request) -> httpx.Response:
		requests.append(request)
		if request.url.path == '/createTask':
			return httpx.Response(200, json={'errorId': 0, 'taskId': 'cap-42'})
		if request.url.path == '/getTaskResult':
			state['polls'] += 1
			if state['polls'] == 1:
				return httpx.Response(200, json={'errorId': 0, 'status': 'processing'})
			return httpx.Response(200, json={'errorId': 0, 'status': 'ready', 'solution': {'gRecaptchaResponse': 'CAP-TOKEN'}})
		return httpx.Response(404)

	async with mock_client(handler) as client:
		solver = CapSolver('client-key', base_url='http://capsolver.test', poll=0.001, timeout=2, client=client)
		assert await solver.solve(Widget('recaptcha_v2', V2_KEY, URL)) == 'CAP-TOKEN'
		assert not client.is_closed  # an injected client belongs to the caller

	assert [request.url.path for request in requests] == ['/createTask', '/getTaskResult', '/getTaskResult']
	assert json.loads(requests[0].content) == {
		'clientKey': 'client-key',
		'task': {'type': 'ReCaptchaV2TaskProxyLess', 'websiteURL': URL, 'websiteKey': V2_KEY},
	}
	assert json.loads(requests[1].content) == {'clientKey': 'client-key', 'taskId': 'cap-42'}


@pytest.mark.asyncio
@pytest.mark.parametrize(
	'widget, expected',
	[
		(
			Widget('recaptcha_v2', 'key-1', URL),
			{'type': 'ReCaptchaV2TaskProxyLess', 'websiteURL': URL, 'websiteKey': 'key-1'},
		),
		(
			Widget('recaptcha_v2', 'key-2', URL, data={'invisible': True}),
			{'type': 'ReCaptchaV2TaskProxyLess', 'websiteURL': URL, 'websiteKey': 'key-2', 'isInvisible': True},
		),
		(
			Widget('recaptcha_v3', 'key-3', URL, data={'action': 'signup', 'min_score': 0.7}),
			{'type': 'ReCaptchaV3TaskProxyLess', 'websiteURL': URL, 'websiteKey': 'key-3', 'pageAction': 'signup', 'minScore': 0.7},
		),
		(
			Widget('recaptcha_v3', 'key-4', URL),
			{'type': 'ReCaptchaV3TaskProxyLess', 'websiteURL': URL, 'websiteKey': 'key-4', 'pageAction': 'verify'},
		),
		(
			Widget('hcaptcha', 'key-5', URL),
			{'type': 'HCaptchaTaskProxyLess', 'websiteURL': URL, 'websiteKey': 'key-5'},
		),
		(
			Widget('turnstile', 'key-6', URL),
			{'type': 'AntiTurnstileTaskProxyLess', 'websiteURL': URL, 'websiteKey': 'key-6'},
		),
		(
			Widget('funcaptcha', 'key-7', URL, data={'blob': 'blob-1'}),
			{'type': 'FunCaptchaTaskProxyLess', 'websiteURL': URL, 'websitePublicKey': 'key-7', 'data': 'blob-1'},
		),
		(
			Widget('funcaptcha', None, URL, data={'public_key': 'key-8'}),
			{'type': 'FunCaptchaTaskProxyLess', 'websiteURL': URL, 'websitePublicKey': 'key-8'},
		),
		(
			Widget('geetest', None, URL, data={'gt': GT, 'challenge': CHALLENGE}),
			{'type': 'GeeTestTaskProxyLess', 'websiteURL': URL, 'gt': GT, 'challenge': CHALLENGE},
		),
	],
)
async def test_capsolver_maps_widgets_to_tasks(widget, expected):
	tasks = []

	def handler(request: httpx.Request) -> httpx.Response:
		if request.url.path == '/createTask':
			tasks.append(json.loads(request.content))
			return httpx.Response(200, json={'errorId': 0, 'taskId': 'task-1'})
		return httpx.Response(200, json={'errorId': 0, 'status': 'ready', 'solution': {'token': 'TOKEN'}})

	async with mock_client(handler) as client:
		solver = CapSolver('key', base_url='http://capsolver.test', poll=0, timeout=2, client=client)
		assert await solver.solve(widget) == 'TOKEN'

	assert tasks[0] == {'clientKey': 'key', 'task': expected}


@pytest.mark.asyncio
@pytest.mark.parametrize(
	'solution, token',
	[
		({'gRecaptchaResponse': 'g-token'}, 'g-token'),
		({'gRecaptchaResponse': 'h-token'}, 'h-token'),
		({'token': 'turnstile-token'}, 'turnstile-token'),
		({'text': 'image-text'}, 'image-text'),
		({'validate': 'geetest-validate', 'seccode': 'geetest-seccode'}, 'geetest-validate'),
		({'nested': {'gRecaptchaResponse': 'deep-token'}}, 'deep-token'),
	],
)
async def test_capsolver_extracts_tokens_from_common_solution_keys(solution, token):
	def handler(request: httpx.Request) -> httpx.Response:
		if request.url.path == '/createTask':
			return httpx.Response(200, json={'errorId': 0, 'taskId': 'task-1'})
		return httpx.Response(200, json={'errorId': 0, 'status': 'ready', 'solution': solution})

	async with mock_client(handler) as client:
		solver = CapSolver('key', base_url='http://capsolver.test', poll=0, timeout=2, client=client)
		assert await solver.solve(Widget('recaptcha_v2', V2_KEY, URL)) == token


@pytest.mark.asyncio
async def test_capsolver_image_solve():
	tasks = []

	def handler(request: httpx.Request) -> httpx.Response:
		if request.url.path == '/createTask':
			tasks.append(json.loads(request.content))
			return httpx.Response(200, json={'errorId': 0, 'taskId': 'image-task'})
		return httpx.Response(200, json={'errorId': 0, 'status': 'ready', 'solution': {'text': 'WXYZ'}})

	async with mock_client(handler) as client:
		solver = CapSolver('key', base_url='http://capsolver.test', poll=0, timeout=2, client=client)
		assert await solver.solve_image(PNG) == 'WXYZ'
		assert await solver.solve_image('data:image/png;base64,' + PNG) == 'WXYZ'

	assert tasks[0] == {'clientKey': 'key', 'task': {'type': 'ImageToTextTask', 'body': PNG}}
	assert tasks[1]['task']['body'] == PNG  # data-URL prefix stripped


@pytest.mark.asyncio
async def test_capsolver_refuses_unsolvable_widgets_without_network():
	requests = []

	def handler(request: httpx.Request) -> httpx.Response:
		requests.append(request)
		return httpx.Response(500)

	async with mock_client(handler) as client:
		solver = CapSolver('key', base_url='http://capsolver.test', client=client)
		assert await solver.solve(Widget('image', None, URL, selector='#captcha')) is None
		assert await solver.solve(Widget('geetest', None, URL, data={'challenge': CHALLENGE})) is None
		assert await solver.solve_image('') is None
	assert requests == []


@pytest.mark.asyncio
async def test_capsolver_returns_none_on_provider_errors():
	create_error = httpx.Response(200, json={'errorId': 1, 'errorCode': 'ERROR_KEY_DENIED', 'errorDescription': 'invalid key'})
	async with mock_client(lambda request: create_error) as client:
		solver = CapSolver('bad', base_url='http://capsolver.test', poll=0, timeout=0.05, client=client)
		assert await solver.solve(Widget('recaptcha_v2', V2_KEY, URL)) is None

	def no_task_id(request: httpx.Request) -> httpx.Response:
		return httpx.Response(200, json={'errorId': 0})

	async with mock_client(no_task_id) as client:
		solver = CapSolver('key', base_url='http://capsolver.test', poll=0, timeout=0.05, client=client)
		assert await solver.solve(Widget('recaptcha_v2', V2_KEY, URL)) is None

	def poll_error(request: httpx.Request) -> httpx.Response:
		if request.url.path == '/createTask':
			return httpx.Response(200, json={'errorId': 0, 'taskId': 'task-1'})
		return httpx.Response(200, json={'errorId': 1, 'errorCode': 'ERROR_INVALID_TASK_ID'})

	async with mock_client(poll_error) as client:
		solver = CapSolver('key', base_url='http://capsolver.test', poll=0, timeout=0.05, client=client)
		assert await solver.solve(Widget('recaptcha_v2', V2_KEY, URL)) is None

	def ready_without_token(request: httpx.Request) -> httpx.Response:
		if request.url.path == '/createTask':
			return httpx.Response(200, json={'errorId': 0, 'taskId': 'task-1'})
		return httpx.Response(200, json={'errorId': 0, 'status': 'ready', 'solution': {}})

	async with mock_client(ready_without_token) as client:
		solver = CapSolver('key', base_url='http://capsolver.test', poll=0, timeout=0.05, client=client)
		assert await solver.solve(Widget('recaptcha_v2', V2_KEY, URL)) is None


@pytest.mark.asyncio
async def test_capsolver_returns_none_on_timeout():
	polls = []

	def handler(request: httpx.Request) -> httpx.Response:
		if request.url.path == '/createTask':
			return httpx.Response(200, json={'errorId': 0, 'taskId': 'task-1'})
		polls.append(request)
		return httpx.Response(200, json={'errorId': 0, 'status': 'processing'})

	async with mock_client(handler) as client:
		solver = CapSolver('key', base_url='http://capsolver.test', poll=0.002, timeout=0.05, client=client)
		started = time.monotonic()
		assert await solver.solve(Widget('recaptcha_v2', V2_KEY, URL)) is None
		assert time.monotonic() - started < 2
	assert polls


# --- client lifecycle and make_solver -------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize(
	'build',
	[
		lambda: TwoCaptcha('key', base_url='http://2captcha.test', poll=0, timeout=2),
		lambda: CapSolver('key', base_url='http://capsolver.test', poll=0, timeout=2),
	],
	ids=['2captcha', 'capsolver'],
)
async def test_solver_closes_only_the_client_it_created(build, monkeypatch):
	created = []

	def handler(request: httpx.Request) -> httpx.Response:
		if request.url.path == '/in.php':
			return httpx.Response(200, json={'status': 1, 'request': 'task-1'})
		if request.url.path == '/res.php':
			return httpx.Response(200, json={'status': 1, 'request': 'TOKEN'})
		if request.url.path == '/createTask':
			return httpx.Response(200, json={'errorId': 0, 'taskId': 'task-1'})
		return httpx.Response(200, json={'errorId': 0, 'status': 'ready', 'solution': {'token': 'TOKEN'}})

	def factory(timeout):
		client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
		created.append(client)
		return client

	monkeypatch.setattr(captcha, '_new_http_client', factory)
	assert await build().solve(Widget('recaptcha_v2', V2_KEY, URL)) == 'TOKEN'
	assert len(created) == 1
	assert created[0].is_closed


def test_make_solver_maps_provider_names():
	assert make_solver(None, 'key') is None
	assert make_solver('', 'key') is None
	assert make_solver('   ', 'key') is None
	assert make_solver('2captcha', None) is None
	assert make_solver('2captcha', '  ') is None
	assert isinstance(make_solver('2captcha', 'key'), TwoCaptcha)
	assert isinstance(make_solver('2CAPTCHA', 'key'), TwoCaptcha)
	assert isinstance(make_solver('TwoCaptcha', 'key'), TwoCaptcha)
	assert isinstance(make_solver('capsolver', 'key'), CapSolver)
	assert isinstance(make_solver('CapSolver', 'key'), CapSolver)
	assert make_solver('rucaptcha', 'key') is None
	assert make_solver('unknown', 'key') is None


def test_make_solver_carries_the_timeout():
	solver = make_solver('capsolver', 'key', timeout=7)
	assert solver.timeout == 7
	assert solver.api_key == 'key'
	# both solvers expose the Solver protocol surface
	assert callable(solver.solve) and callable(solver.solve_image)
