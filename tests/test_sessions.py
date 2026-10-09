"""Side-by-side sessions, logins from chat, run history edits, and the settings the app saves to .env."""
import asyncio
import json
import os

import pytest
from dotenv import dotenv_values
from httpx import ASGITransport, AsyncClient

from veil import accounts, planner
from veil.agent import TaskResult
from veil.config import Settings
from veil.llm import build_llm
from veil.planner import Job
from veil.webui import runner as runner_mod
from veil.webui import server as server_mod
from veil.webui.runner import SessionHub
from veil.webui.server import create_app


def _client(app):
	return AsyncClient(transport=ASGITransport(app=app), base_url='http://127.0.0.1:7788')


@pytest.mark.asyncio
async def test_a_split_request_runs_each_job_in_its_own_session(tmp_path, monkeypatch):
	seen, release = [], asyncio.Event()

	async def fake_run_task(task, settings, identity_name=None, max_steps=None, *, hooks, agent_kwargs):
		seen.append((identity_name, agent_kwargs['sensitive_data']))
		await release.wait()
		return TaskResult(ok=True, output='SIGNUP_STATUS: signed_up - welcome', attempts=[])

	async def fake_plan(task, profiles, known, llm):
		return [Job('signup', 'https://shop.test/join', 'p1'), Job('signup', 'https://shop.test/join', 'p2'), Job('signup', 'https://shop.test/join', 'p1')]

	monkeypatch.setattr(runner_mod, 'run_task', fake_run_task)
	monkeypatch.setattr(server_mod, 'build_llm', lambda s: object())
	monkeypatch.setattr(planner, 'plan', fake_plan)
	env = Settings(data_dir=tmp_path, max_parallel=1)
	accounts.save_profiles(accounts.profiles_path(env), [{'id': 'p1', 'email': 'a@a.com'}, {'id': 'p2', 'email': 'b@b.com'}])
	app = create_app(env, port=7788)
	hub = app.state.hub
	async with _client(app) as c:
		r = (await c.post('/api/run', json={'task': 'create 2 accounts on shop.test, one per profile'})).json()
		assert r['group'] and r['skipped'] == ['shop.test as p1: already in this request']
		side = [s for s in r['sessions'] if s['group'] == r['group']]
		assert [s['label'] for s in side] == ['Sign up · shop.test · p1', 'Sign up · shop.test · p2']
		await asyncio.sleep(0.05)
		assert [hub.get(s['id']).status for s in side] == ['starting', 'queued']  # one browser at a time here
		assert not app.state.runner.busy  # the chat itself stays free
		state = (await c.get('/api/state', params={'session': side[1]['id']})).json()
		assert state['session'] == side[1]['id'] and state['status'] == 'queued'
		release.set()
		for s in side:
			await asyncio.wait_for(hub.get(s['id'])._task, timeout=2)
		assert sorted(i for i, _ in seen) == ['acct-shop.test-p1', 'acct-shop.test-p2']  # each its own identity
		assert {d['https://*.shop.test']['x_email'] for _, d in seen} == {'a@a.com', 'b@b.com'}
		log = accounts.SignupLog(env.accounts_dir / 'signups.csv')
		assert {(row.profile_id, row.status) for row in log.for_site('shop.test')} == {('p1', 'signed_up'), ('p2', 'signed_up')}
		assert (await c.post('/api/sessions/close', json={'session': side[0]['id']})).status_code == 200
		assert (await c.post('/api/sessions/close', json={'session': 'main'})).status_code == 409
		await c.post('/api/clear', json={})  # New task: finished side sessions close too
		assert list(hub.sessions) == ['main']


@pytest.mark.asyncio
async def test_login_or_signup_logs_in_where_the_account_exists(tmp_path, monkeypatch):
	seen = {}

	async def fake_run_task(task, settings, identity_name=None, max_steps=None, *, hooks, agent_kwargs):
		seen.update(task=task, identity=identity_name)
		return TaskResult(ok=True, output='LOGIN_STATUS: logged_in - home feed', attempts=[])

	def no_llm(settings):
		raise RuntimeError('no key')

	monkeypatch.setattr(runner_mod, 'run_task', fake_run_task)
	monkeypatch.setattr(server_mod, 'build_llm', no_llm)
	env = Settings(data_dir=tmp_path)
	accounts.save_profiles(accounts.profiles_path(env), [{'id': 'me', 'email': 'me@a.com', 'password': 'Mine1!'}])
	accounts.SignupLog(env.accounts_dir / 'signups.csv').record(accounts.Signup('x.com', 'me', 'me@a.com', 'Mine1!', 'signed_up', 'acct-x.com-me'))
	app = create_app(env, port=7788)
	async with _client(app) as c:
		r = await c.post('/api/run', json={'task': 'go to x.com and login/signup using my details'})
		assert r.status_code == 200 and '→ Logging in at x.com as me' in r.json()['events'][0]['text']
		await asyncio.wait_for(app.state.runner._task, timeout=2)
		assert 'LOGIN_STATUS' in seen['task'] and seen['identity'] == 'acct-x.com-me' and 'Mine1!' not in seen['task']
		assert accounts.SignupLog(env.accounts_dir / 'signups.csv').get('x.com', 'me').status == 'logged_in'


@pytest.mark.asyncio
async def test_runs_can_be_renamed_and_deleted_but_not_while_running(tmp_path, monkeypatch):
	release = asyncio.Event()

	async def fake_run_task(task, settings, identity_name=None, max_steps=None, *, hooks, agent_kwargs):
		await release.wait()
		return TaskResult(ok=True, output='done', attempts=[])

	monkeypatch.setattr(runner_mod, 'run_task', fake_run_task)
	app = create_app(Settings(data_dir=tmp_path), port=7788)
	async with _client(app) as c:
		run_id = (await c.post('/api/run', json={'task': 'find it'})).json()['run_id']
		assert (await c.post(f'/api/runs/{run_id}/delete')).status_code == 409
		release.set()
		await asyncio.wait_for(app.state.runner._task, timeout=2)
		assert (await c.post(f'/api/runs/{run_id}/rename', json={'title': '  my   search '})).status_code == 200
		assert (await c.get('/api/runs')).json()['runs'][0]['title'] == 'my search'
		assert (await c.post(f'/api/runs/{run_id}/delete')).status_code == 200
		assert (await c.get('/api/runs')).json()['runs'] == [] and (await c.get('/api/state')).json()['events'] == []
		assert (await c.post('/api/runs/nope-123/delete')).status_code == 404


@pytest.mark.asyncio
async def test_keys_and_proxies_are_saved_to_env_without_coming_back_out(tmp_path, monkeypatch):
	from veil.geo import ExitInfo
	from veil.proxy import read_list
	from veil.webui import admin

	for var in ('OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'VEIL_PROXY_FILE', 'VEIL_PROXY_GATEWAY', 'VEIL_PROXY_MODE', 'VEIL_PROXY_STRATEGY', 'VEIL_LLM_EFFORT'):
		monkeypatch.delenv(var, raising=False)
	for var in ('VEIL_LLM_PROVIDER', 'VEIL_LLM_MODEL'):
		monkeypatch.setenv(var, 'x')  # restored afterwards; the endpoints overwrite them
	env_file = tmp_path / '.env'
	env_file.write_text('# mine\nVEIL_LLM_PROVIDER=openai\nVEIL_LLM_API_KEY=sk-old-key-1234\n')
	monkeypatch.setenv('VEIL_LLM_API_KEY', 'sk-old-key-1234')
	env = Settings(data_dir=tmp_path, env_file=env_file, llm_api_key='sk-old-key-1234')

	async def fake_lookup(proxy, timeout=15):
		if proxy.host == 'dead.test':
			raise OSError('timed out')
		return ExitInfo('9.9.9.9', 'US', 'America/New_York', 'Boston')

	monkeypatch.setattr(admin, 'lookup_exit', fake_lookup)
	app = create_app(env, port=7788)
	async with _client(app) as c:
		k = (await c.post('/api/keys', json={'provider': 'openai', 'key': 'sk-new-key-5678'})).json()['keys']['openai']
		assert k['set'] and k['masked'] == '••••5678' and 'sk-new' not in json.dumps(k)
		saved = dotenv_values(env_file)
		assert saved['OPENAI_API_KEY'] == saved['VEIL_LLM_API_KEY'] == 'sk-new-key-5678' and '# mine' in env_file.read_text()
		assert env.llm_api_key == 'sk-new-key-5678'
		assert (await c.post('/api/keys', json={'provider': 'openai', 'key': 'has space'})).status_code == 400
		# a new default provider: the old VEIL_LLM_API_KEY is not sent to it
		await c.post('/api/llm/default', json={'provider': 'anthropic', 'model': 'claude-opus-5-5', 'effort': 'high'})
		assert (env.llm_provider, env.llm_model, env.llm_effort, env.llm_api_key) == ('anthropic', 'claude-opus-5-5', 'high', None)
		assert 'VEIL_LLM_API_KEY' not in dotenv_values(env_file) and dotenv_values(env_file)['VEIL_LLM_EFFORT'] == 'high'
		assert (await c.post('/api/llm/default', json={'provider': 'anthropic', 'model': 'x', 'effort': 'minimal'})).status_code == 400
		cfg = (await c.get('/api/config')).json()
		assert cfg['keys']['openai'] and not cfg['keys']['anthropic'] and cfg['efforts']['anthropic'][-1] == 'max'

		r = (await c.post('/api/proxies/add', json={'text': 'gw.test:823:user:pa:ss\nsocks5://u:p@h.test:1080\n\nhttp://dead.test:80'})).json()
		assert r['mode'] == 'list' and len(r['proxies']) == 2 and len(r['errors']) == 1  # SOCKS with a password can't work in Chromium
		assert all('pa:ss' not in p['server'] for p in r['proxies'])
		assert read_list(env.data_dir / 'proxies.txt')[0].password == 'pa:ss' and env.proxy_file == env.data_dir / 'proxies.txt'
		r = (await c.post('/api/proxies/test', json={})).json()
		assert [p['status']['ok'] for p in r['proxies']] == [True, False] and r['proxies'][0]['status']['country'] == 'US'
		assert (await c.post('/api/proxies/mode', json={'mode': 'gateway'})).status_code == 400  # no gateway yet
		r = (await c.post('/api/proxies/mode', json={'mode': 'gateway', 'gateway': 'http://me__sessid.{session}:pw@gw.test:823'})).json()
		assert r['mode'] == 'gateway' and r['gateway']['sticky'] and ':pw@' not in r['gateway']['server'] and env.proxy_gateway
		r = (await c.post('/api/proxies/mode', json={'mode': 'direct'})).json()
		assert r['mode'] == 'direct' and env.proxy_file is None and env.proxy_gateway is None
		assert dotenv_values(env_file)['VEIL_PROXY_MODE'] == 'direct' and 'VEIL_PROXY_GATEWAY' in dotenv_values(env_file)  # kept for later
		r = (await c.post('/api/proxies/remove', json={'ids': [p['id'] for p in r['proxies']]})).json()
		assert r['proxies'] == []
	assert os.environ['VEIL_PROXY_MODE'] == 'direct'
	for var in ('VEIL_PROXY_MODE', 'VEIL_PROXY_GATEWAY', 'VEIL_PROXY_FILE', 'OPENAI_API_KEY', 'VEIL_LLM_EFFORT'):
		os.environ.pop(var, None)  # written by the endpoints, not by monkeypatch


def test_one_proxy_source_is_shared_by_runs_at_the_same_time(tmp_path):
	path = tmp_path / 'proxies.txt'
	path.write_text('a.test:1\nb.test:2\n')
	hub = SessionHub(tmp_path)
	s = Settings(proxy_file=path)
	src = hub.proxy_source(s)
	assert src is hub.proxy_source(s) and [src.acquire().host, src.acquire().host] == ['a.test', 'b.test']
	assert hub.proxy_source(Settings()) is None


def test_effort_goes_to_each_provider_its_own_way():
	llm = build_llm(Settings(llm_provider='openai', llm_model='gpt-6-luna', llm_api_key='sk-test', llm_effort='high'))
	assert llm.reasoning_effort == 'high' and 'gpt-6-luna' in llm.reasoning_models
	llm = build_llm(Settings(llm_provider='anthropic', llm_model='claude-opus-5-5', llm_api_key='sk-ant', llm_effort='xhigh'))
	assert llm.output_config == {'effort': 'xhigh'} and llm.thinking == {'type': 'adaptive'}
	llm = build_llm(Settings(llm_provider='anthropic', llm_model='claude-opus-5-5', llm_api_key='sk-ant', llm_effort='minimal'))
	assert llm.output_config is None  # not an Anthropic level: the provider's default
	llm = build_llm(Settings(llm_provider='anthropic', llm_model='claude-haiku-4-5', llm_api_key='sk-ant', llm_effort='high'))
	assert llm.thinking is None and llm.output_config is None  # predates adaptive thinking and effort
