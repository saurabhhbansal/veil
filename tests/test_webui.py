import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from veil.agent import Attempt, TaskResult
from veil.config import Settings
from veil.webui import runner as runner_mod
from veil.webui.manager import settings_from_ui
from veil.webui.runner import AgentRunner, RunRequest, step_actions


def _base():
	return Settings(llm_api_key='sk-env', proxy_gateway='http://u:{session}@gw:1', headless=True, max_attempts=3)


def test_blank_ui_fields_keep_env_settings():
	s, identity, max_steps, kwargs = settings_from_ui({'agent_settings.llm_api_key': '', 'stealth_settings.identity': '  '}, _base())
	assert s.llm_api_key == 'sk-env' and s.proxy_gateway == 'http://u:{session}@gw:1'
	assert identity is None and max_steps is None and kwargs == {}


def test_ui_fields_override_settings_and_agent_kwargs():
	values = {
		'agent_settings.llm_model': 'gpt-6-luna-mini', 'agent_settings.llm_temperature': None, 'agent_settings.max_steps': 12,
		'agent_settings.use_vision': False, 'agent_settings.max_actions': 3, 'agent_settings.extend_system_prompt': 'be brief',
		'stealth_settings.headful': True, 'stealth_settings.geo_match': False, 'stealth_settings.identity': 'shop1',
		'stealth_settings.max_attempts': 5,
	}
	base = _base()
	s, identity, max_steps, kwargs = settings_from_ui(values, base)
	assert (s.llm_model, s.llm_temperature, s.headless, s.geo_match, s.max_attempts) == ('gpt-6-luna-mini', None, False, False, 5)
	assert (identity, max_steps) == ('shop1', 12)
	assert kwargs == {'use_vision': False, 'max_actions_per_step': 3, 'extend_system_message': 'be brief'}
	assert base.headless is True  # the env settings object is never mutated


def test_prefilled_fields_can_be_cleared():
	base = _base()
	base.llm_base_url, base.cdp_url, base.proxy_file = 'http://llm', 'http://cdp', Path('proxies.txt')
	s, *_ = settings_from_ui({'agent_settings.llm_base_url': '', 'stealth_settings.cdp_url': ' ', 'stealth_settings.proxy_file': ''}, base)
	assert (s.llm_base_url, s.cdp_url, s.proxy_file) == (None, None, None)
	s, *_ = settings_from_ui({}, base)  # fields not in the form keep .env
	assert (s.llm_base_url, s.cdp_url, s.proxy_file) == ('http://llm', 'http://cdp', Path('proxies.txt'))


def test_switching_provider_never_reuses_the_env_key():
	s, *_ = settings_from_ui({'agent_settings.llm_provider': 'groq', 'agent_settings.llm_api_key': ''}, _base())
	assert s.llm_api_key is None  # build_llm falls back to GROQ_API_KEY instead of sending the OpenAI key to Groq
	s, *_ = settings_from_ui({'agent_settings.llm_provider': 'groq', 'agent_settings.llm_api_key': 'gsk-ui'}, _base())
	assert s.llm_api_key == 'gsk-ui'
	s, *_ = settings_from_ui({'agent_settings.llm_provider': 'openai'}, _base())
	assert s.llm_api_key == 'sk-env'


def test_step_actions_are_plain_data():
	action = SimpleNamespace(model_dump=lambda exclude_none: {'navigate': {'url': 'https://example.com'}, 'wait': 3})
	out = SimpleNamespace(action=[action])
	assert step_actions(out) == [{'name': 'navigate', 'args': {'url': 'https://example.com'}}, {'name': 'wait', 'args': {'value': 3}}]
	assert step_actions(SimpleNamespace()) == []


class _FakeAgent:
	def __init__(self):
		self.stopped = asyncio.Event()

	def pause(self): ...
	def resume(self): ...

	def stop(self):
		self.stopped.set()


@pytest.mark.asyncio
async def test_runner_lifecycle_with_stop(tmp_path, monkeypatch):
	agent = _FakeAgent()

	async def fake_run_task(task, settings, identity_name=None, max_steps=None, *, hooks, agent_kwargs):
		session = SimpleNamespace(persona=None, exit=None, proxy=None, identity=None, settings=settings, browser=None)
		await hooks.on_attempt(1, 3, session, agent)
		await agent.stopped.wait()  # "runs" until the UI presses Stop
		assert hooks.stop_requested()
		attempt = Attempt(proxy=None, exit_ip='1.2.3.4', persona='dev', outcome='stopped')
		await hooks.on_attempt_end(1, attempt)
		return TaskResult(ok=False, output='Navigated to https://example.com', attempts=[attempt])

	monkeypatch.setattr(runner_mod, 'run_task', fake_run_task)
	r = AgentRunner(runs_dir=tmp_path)
	r.start(RunRequest(task='do it', settings=Settings()))
	with pytest.raises(RuntimeError):
		r.start(RunRequest(task='again', settings=Settings()))  # one run at a time
	await asyncio.sleep(0.05)
	assert r.status == 'running' and r.identity['attempt'] == 1 and r.identity['total'] == 3

	r.stop()
	assert r.status == 'stopping'
	await asyncio.wait_for(r._task, timeout=2)
	assert r.status == 'idle' and not r.busy
	last = r.events[-1]
	assert last['type'] == 'result' and last['stopped'] and last['output'] is None  # a stop shows no fake "result"
	assert json.loads((r.run_dir / 'result.json').read_text())['attempts'][0]['outcome'] == 'stopped'


@pytest.mark.asyncio
async def test_stop_while_identity_is_launching_cancels(tmp_path, monkeypatch):
	async def slow_run_task(*a, **k):
		await asyncio.sleep(30)

	monkeypatch.setattr(runner_mod, 'run_task', slow_run_task)
	r = AgentRunner(runs_dir=tmp_path)
	r.start(RunRequest(task='do it', settings=Settings()))
	await asyncio.sleep(0.05)
	r.stop()
	await asyncio.wait_for(r._task, timeout=2)  # the runner absorbs its own cancellation
	assert r.status == 'idle' and r.events[-1]['type'] == 'result' and r.events[-1]['stopped']


class _PausableAgent(_FakeAgent):
	def __init__(self):
		super().__init__()
		self.paused = False
		self.state = SimpleNamespace(last_result=None)

	def pause(self):
		self.paused = True

	def resume(self):
		self.paused = False


@pytest.mark.asyncio
async def test_captcha_pauses_for_the_person_and_hand_back_tells_the_agent(tmp_path, monkeypatch):
	agent = _PausableAgent()

	async def fake_run_task(task, settings, identity_name=None, max_steps=None, *, hooks, agent_kwargs):
		session = SimpleNamespace(persona=None, exit=None, proxy=None, identity=None, settings=settings, browser=object())
		await hooks.on_attempt(1, 1, session, agent)
		await hooks.request_human('recaptcha CAPTCHA on this page', {'source': 'detector', 'vendor': 'recaptcha', 'kind': 'captcha', 'url': 'https://a.test/'})
		await agent.stopped.wait()
		return TaskResult(ok=False, output=None, attempts=[])

	monkeypatch.setattr(runner_mod, 'run_task', fake_run_task)
	r = AgentRunner(runs_dir=tmp_path)
	r.start(RunRequest(task='sign up', settings=Settings()))
	await asyncio.sleep(0.05)
	assert r.status == 'needs_human' and agent.paused and r.human['vendor'] == 'recaptcha'
	snap = r.snapshot()
	assert snap['browser_open'] and [e['type'] for e in snap['events']] == ['task', 'attempt', 'human']

	assert r.hand_back('ticked the box')
	assert r.status == 'running' and not agent.paused and r.human is None
	memo = agent.state.last_result[-1].long_term_memory
	assert 'recaptcha CAPTCHA' in memo and 'ticked the box' in memo
	assert r.events[-1]['resolved'] and r.events[-1]['note'] == 'ticked the box'

	assert r.take_over() and r.status == 'needs_human' and r.human['source'] == 'user'
	r.stop()  # stopping while the person has the browser
	await asyncio.wait_for(r._task, timeout=2)
	assert r.status == 'idle' and r.human is None and r.events[-2]['outcome'] == 'stopped'


def test_take_over_and_hand_back_do_nothing_without_a_run(tmp_path):
	r = AgentRunner(runs_dir=tmp_path)
	assert not r.take_over() and not r.hand_back() and r.status == 'idle'


def test_live_key_events_map_to_cdp():
	from veil.webui.live import key_event_params

	(m, p), = key_event_params({'type': 'keyDown', 'key': 'a', 'code': 'KeyA', 'keyCode': 65})
	assert m == 'Input.dispatchKeyEvent' and p['type'] == 'keyDown' and p['text'] == 'a'
	(_, p), = key_event_params({'type': 'keyDown', 'key': 'Enter', 'code': 'Enter', 'keyCode': 13})
	assert p['text'] == '\r'
	(_, p), = key_event_params({'type': 'keyDown', 'key': 'Backspace', 'code': 'Backspace', 'keyCode': 8})
	assert p['type'] == 'rawKeyDown' and 'text' not in p
	(_, p), = key_event_params({'type': 'keyDown', 'key': 'a', 'code': 'KeyA', 'keyCode': 65, 'modifiers': 2})
	assert p['type'] == 'rawKeyDown' and p['commands'] == ['selectAll'] and 'text' not in p
	assert key_event_params({'type': 'keyUp', 'key': 'a'})[0][1]['type'] == 'keyUp'
	assert key_event_params({'type': 'char', 'key': 'a'}) == []


def test_server_refuses_other_origins_and_odd_paths(tmp_path):
	from fastapi.testclient import TestClient

	from veil.webui.server import create_app, takes_control

	app = create_app(Settings(data_dir=tmp_path), port=7788)
	c = TestClient(app, base_url='http://127.0.0.1:7788')
	assert c.get('/api/state').json()['status'] == 'idle'
	assert c.post('/api/run', json={'task': 'x'}, headers={'origin': 'https://evil.example'}).status_code == 403
	assert c.get('/api/state', headers={'host': 'evil.example:7788'}).status_code == 403  # DNS rebinding
	assert c.post('/api/run', json={'task': ' '}).status_code == 400
	assert c.get('/api/runs/..%2F..%2Fsecrets/x.txt').status_code == 404
	assert 'llm_api_key' not in c.get('/api/config').json()
	assert takes_control({'t': 'mouse', 'type': 'mousePressed'}) and not takes_control({'t': 'mouse', 'type': 'mouseMoved'})
	assert not takes_control({'t': 'wheel'})


@pytest.mark.asyncio
async def test_accounts_api_keeps_passwords_server_side_and_records_signups(tmp_path, monkeypatch):
	from httpx import ASGITransport, AsyncClient

	from veil import accounts
	from veil.webui.server import create_app

	seen = {}

	async def fake_run_task(task, settings, identity_name=None, max_steps=None, *, hooks, agent_kwargs):
		seen.update(task=task, identity=identity_name, sensitive=agent_kwargs['sensitive_data'])
		return TaskResult(ok=True, output='SIGNUP_STATUS: verify_email - check your inbox', attempts=[])

	monkeypatch.setattr(runner_mod, 'run_task', fake_run_task)
	env = Settings(data_dir=tmp_path)
	app = create_app(env, port=7788)
	async with AsyncClient(transport=ASGITransport(app=app), base_url='http://127.0.0.1:7788') as c:
		assert (await c.get('/api/accounts')).json()['profiles'] == []
		r = await c.post('/api/accounts/profiles', json={'profiles': [{'id': 'p1', 'email': 'me@a.com', 'first_name': 'Asha', 'password': 'S3cret!'}]})
		assert r.status_code == 200 and r.json()['profiles'][0]['has_password'] and 'password' not in r.json()['profiles'][0]
		# renamed without resending the password: it stays
		await c.post('/api/accounts/profiles', json={'profiles': [{'id': 'me', 'original_id': 'p1', 'email': 'me@a.com'}]})
		assert accounts.load_profiles(accounts.profiles_path(env))[0]['password'] == 'S3cret!'
		assert (await c.post('/api/accounts/profiles', json={'profiles': [{'id': 'x', 'email': 'bad'}]})).status_code == 400

		r = await c.post('/api/accounts/signup', json={'url': 'https://www.shop.test/join', 'values': {}})
		assert r.status_code == 200 and r.json()['events'][0]['text'] == 'Sign up at shop.test as me (me@a.com)'
		await asyncio.wait_for(app.state.runner._task, timeout=2)
		assert seen['identity'] == 'acct-shop.test-me' and seen['sensitive']['https://*.shop.test']['x_password'] == 'S3cret!'
		assert 'S3cret!' not in seen['task'] and 'me@a.com' not in seen['task']  # the LLM sees placeholders only
		(row,) = (await c.get('/api/accounts')).json()['signups']
		assert row['status'] == 'verify_email' and 'password' not in row
		assert (await c.post('/api/accounts/reveal', json={'site': 'shop.test'})).json()['password'] == 'S3cret!'
		assert (await c.post('/api/accounts/signup', json={'url': 'shop.test'})).status_code == 409  # one account per site
