"""The company identity server client (identities.py) against a fake server, and account runs that use it."""
import asyncio
import json

import httpx
import pytest

from veil import accounts as A
from veil import identities
from veil.agent import TaskResult
from veil.config import Settings
from veil.identities import IdentityAPI, IdentityAPIError
from veil.webui import runner as runner_mod
from veil.webui.server import create_app

ASHA = {'id': 'emp-104', 'email': 'asha@corp.test', 'first_name': 'Asha', 'last_name': 'Rao', 'phone': '+4915112345678', 'country': 'DE', 'employee_no': 77}


class FakeServer:
	"""Records every request; answers like the contract in docs/IDENTITY_API.md."""

	def __init__(self, people=(ASHA,), codes=None):
		self.people = {p['id']: p for p in people}
		self.codes = codes or {}
		self.requests: list[httpx.Request] = []
		self.events: list[dict] = []

	def __call__(self, request: httpx.Request) -> httpx.Response:
		self.requests.append(request)
		path = request.url.path
		if request.method == 'POST' and path == '/v1/identities/assign':
			body = json.loads(request.content)
			free = [p for p in self.people.values() if p['id'] not in body['exclude']]
			return httpx.Response(200, json=free[0]) if free else httpx.Response(409, json={'error': 'nobody free for ' + body['site']})
		if request.method == 'GET' and path == '/v1/identities':
			return httpx.Response(200, json={'identities': [{'id': p['id'], 'email': p['email']} for p in self.people.values()]})
		parts = path.split('/')  # ['', 'v1', 'identities', id, ...]
		person = self.people.get(parts[3]) if len(parts) > 3 else None
		if not person:
			return httpx.Response(404, json={'error': 'no such identity'})
		if request.method == 'GET' and len(parts) == 4:
			return httpx.Response(200, json=person)
		if request.method == 'POST' and parts[4:] == ['events']:
			self.events.append(json.loads(request.content))
			return httpx.Response(204)
		if request.method == 'GET' and parts[4:] == ['codes']:
			code = self.codes.get((person['id'], request.url.params['channel']))
			return httpx.Response(200, json={'code': code}) if code else httpx.Response(404, json={'error': 'no code yet'})
		return httpx.Response(405)


def _api(server, **kw):
	return IdentityAPI('http://ids.corp.test/', 'tok-1', transport=httpx.MockTransport(server), **kw)


def test_assign_sends_the_request_and_returns_a_profile(tmp_path):
	server = FakeServer()
	api = _api(server, country='de', exit_countries=['DE', 'US'])
	profile, row, exists = A.plan_signup(api, A.SignupLog(tmp_path / 's.csv'), 'shop.test')
	assert not exists and profile['id'] == 'emp-104' and profile['phone'] == '+4915112345678'
	assert 'employee_no' not in profile  # only the profile columns are kept
	assert row.identity == 'acct-shop.test-emp-104' and len(row.password) == 16  # the server sent none: veil makes one
	req = server.requests[0]
	assert req.headers['authorization'] == 'Bearer tok-1' and str(req.url) == 'http://ids.corp.test/v1/identities/assign'
	assert json.loads(req.content) == {
		'site': 'shop.test', 'url': 'https://shop.test', 'purpose': 'signup', 'country': 'DE', 'exit_countries': ['DE', 'US'], 'exclude': [],
	}
	assert A.placeholders(profile, row.password)['x_phone'] == '+4915112345678'


def test_retries_and_logins_fetch_the_same_person(tmp_path):
	server = FakeServer()
	api, log = _api(server), A.SignupLog(tmp_path / 's.csv')
	_, row, _ = A.plan_signup(api, log, 'shop.test')
	row.status = 'failed'
	log.record(row)
	profile, again, exists = A.plan_signup(api, A.SignupLog(log.path), 'shop.test')
	assert profile['email'] == 'asha@corp.test' and again.password == row.password and not exists
	assert server.requests[-1].url.path == '/v1/identities/emp-104'  # by id, not a new assignment
	row.status = 'signed_up'
	log.record(row)
	profile, login_row, logged = A.plan_login(api, A.SignupLog(log.path), 'shop.test')
	assert logged and login_row.password == row.password and profile['first_name'] == 'Asha'


def test_server_errors_say_what_happened(tmp_path):
	server = FakeServer(people=())
	with pytest.raises(IdentityAPIError, match='409 nobody free for shop.test'):
		A.plan_signup(_api(server), A.SignupLog(tmp_path / 's.csv'), 'shop.test')
	with pytest.raises(IdentityAPIError, match='no such identity'):
		_api(server).get('emp-1')
	bad = IdentityAPI('http://ids.corp.test', transport=httpx.MockTransport(lambda r: httpx.Response(200, json={'id': 'x', 'email': 'nope'})))
	with pytest.raises(IdentityAPIError, match='not an email address'):
		bad.get('x')

	def down(request):
		raise httpx.ConnectError('connection refused')

	with pytest.raises(IdentityAPIError, match='unreachable at http://ids.corp.test'):
		IdentityAPI('http://ids.corp.test', transport=httpx.MockTransport(down)).assign('shop.test', 'signup')
	assert IdentityAPI('http://ids.corp.test', transport=httpx.MockTransport(down)).listing() == []  # optional endpoint


def test_report_and_codes():
	server = FakeServer(codes={('emp-104', 'sms'): '482913'})
	row = A.Signup('shop.test', 'emp-104', 'asha@corp.test', 'Pw1!', 'signed_up', 'acct-shop.test-emp-104', note='welcome')
	_api(server).report(row, 'signup', background=False)
	assert server.events[0]['status'] == 'signed_up' and 'password' not in server.events[0]
	_api(server, send_passwords=True).report(row, 'signup', background=False)
	assert server.events[1]['password'] == 'Pw1!'
	api = _api(server)
	assert api.code('emp-104', 'sms', 'shop.test', 0) == '482913'
	assert api.code('emp-104', 'email', 'shop.test', 0) is None
	assert asyncio.run(api.code_fetcher('emp-104', 'shop.test')('sms')) == '482913'
	assert identities.LocalProfiles([]).code_fetcher('p1', 'x') is None  # profiles.csv: codes go to the person


def test_settings_pick_the_source(tmp_path):
	assert isinstance(identities.profile_source(Settings(identity_api='http://ids.corp.test', proxy_country='de')), IdentityAPI)
	A.save_profiles(tmp_path / 'p.csv', [{'id': 'p1', 'email': 'a@a.com'}])
	assert isinstance(identities.profile_source(Settings(profiles_file=tmp_path / 'p.csv')), identities.LocalProfiles)


@pytest.mark.asyncio
async def test_the_app_signs_up_as_whoever_the_server_assigns(tmp_path, monkeypatch):
	server, seen = FakeServer(codes={('emp-104', 'sms'): '111222'}), {}

	async def fake_run_task(task, settings, identity_name=None, max_steps=None, *, hooks, agent_kwargs, country=None, fetch_code=None):
		seen.update(task=task, identity=identity_name, data=agent_kwargs['sensitive_data'], country=country, code=await fetch_code('sms'))
		return TaskResult(ok=True, output='SIGNUP_STATUS: signed_up - welcome', attempts=[])

	real = identities.profile_source
	monkeypatch.setattr(runner_mod, 'run_task', fake_run_task)
	monkeypatch.setattr(identities, 'profile_source', lambda s, p=None: _api(server) if s.identity_api else real(s, p))
	env = Settings(data_dir=tmp_path, identity_api='http://ids.corp.test')
	app = create_app(env, port=7788)
	async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1:7788') as c:
		state = (await c.get('/api/accounts')).json()
		assert state['identity_api'] == 'http://ids.corp.test' and [p['id'] for p in state['profiles']] == ['emp-104']
		assert (await c.post('/api/accounts/profiles', json={'profiles': []})).status_code == 409  # edited on the server, not here
		r = await c.post('/api/accounts/signup', json={'url': 'shop.test'})
		assert r.status_code == 200, r.text
		await asyncio.wait_for(app.state.runner._task, timeout=2)
	await asyncio.sleep(0.05)  # the outcome is reported on a background thread
	assert seen['identity'] == 'acct-shop.test-emp-104' and seen['country'] == 'DE' and seen['code'] == '111222'
	assert seen['data']['https://*.shop.test']['x_email'] == 'asha@corp.test' and 'get_verification_code' in seen['task']
	assert A.SignupLog(env.accounts_dir / 'signups.csv').get('shop.test', 'emp-104').status == 'signed_up'
	assert server.events and server.events[-1]['status'] == 'signed_up'
