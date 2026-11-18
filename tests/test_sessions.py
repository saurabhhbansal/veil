"""Side-by-side sessions, logins from chat, run history edits, and the settings the app saves to .env."""
import asyncio
import json
import os

import pytest
from dotenv import dotenv_values
from httpx import ASGITransport, AsyncClient

from veil import accounts, identities, planner
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

	for var in ('OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'VEIL_PROXY_FILE', 'VEIL_PROXY_GATEWAY', 'VEIL_PROXY_MODE', 'VEIL_PROXY_STRATEGY', 'VEIL_LLM_EFFORT',
	            'VEIL_CAPTCHA_PROVIDER', 'VEIL_CAPTCHA_API_KEY'):
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
		# A non-default provider's key goes under its own variable: the model menu's "Save key & make default".
		k = (await c.post('/api/keys', json={'provider': 'deepseek', 'key': 'sk-ds-key-9012'})).json()['keys']['deepseek']
		assert k['set'] and k['source'] == 'DEEPSEEK_API_KEY' and dotenv_values(env_file)['DEEPSEEK_API_KEY'] == 'sk-ds-key-9012'
		# CAPTCHA solving: provider + key saved to .env, key never returned
		cap = (await c.post('/api/captcha', json={'provider': '2captcha', 'key': 'captcha-key-5678'})).json()
		assert cap['provider'] == '2captcha' and cap['has_key'] and cap['masked'] == '••••5678'
		assert 'captcha-key' not in json.dumps(cap) and dotenv_values(env_file)['VEIL_CAPTCHA_API_KEY'] == 'captcha-key-5678'
		cap = (await c.post('/api/captcha', json={'provider': ''})).json()
		assert cap['provider'] is None and cap['has_key']  # the key is kept for when solving is switched back on
		assert dotenv_values(env_file).get('VEIL_CAPTCHA_PROVIDER') is None
		assert (await c.post('/api/captcha', json={'provider': 'nosuch', 'key': 'x'})).status_code == 400
		# a new default provider: the old VEIL_LLM_API_KEY is not sent to it
		await c.post('/api/llm/default', json={'provider': 'anthropic', 'model': 'claude-opus-5-5', 'effort': 'high'})
		assert (env.llm_provider, env.llm_model, env.llm_effort, env.llm_api_key) == ('anthropic', 'claude-opus-5-5', 'high', None)
		assert 'VEIL_LLM_API_KEY' not in dotenv_values(env_file) and dotenv_values(env_file)['VEIL_LLM_EFFORT'] == 'high'
		assert (await c.post('/api/llm/default', json={'provider': 'anthropic', 'model': 'x', 'effort': 'minimal'})).status_code == 400
		cfg = (await c.get('/api/config')).json()
		assert cfg['keys']['openai'] and cfg['keys']['deepseek'] and not cfg['keys']['anthropic'] and cfg['efforts']['anthropic'][-1] == 'max'
		assert cfg['efforts']['deepseek'] == ['low', 'high', 'max'] and cfg['models']['deepseek'] == ['deepseek-flash', 'deepseek-v4-pro']
		assert 'engine' not in cfg and 'engines' not in cfg  # the app always runs Fortress; no engine picker
		assert cfg['captcha'] == {'provider': None, 'has_key': True, 'timeout': 120.0} and cfg['mailbox_wait'] == 90.0

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
	for var in ('VEIL_PROXY_MODE', 'VEIL_PROXY_GATEWAY', 'VEIL_PROXY_FILE', 'OPENAI_API_KEY', 'DEEPSEEK_API_KEY', 'VEIL_LLM_EFFORT', 'VEIL_CAPTCHA_PROVIDER', 'VEIL_CAPTCHA_API_KEY'):
		os.environ.pop(var, None)  # written by the endpoints, not by monkeypatch


def test_one_proxy_source_is_shared_by_runs_at_the_same_time(tmp_path):
	path = tmp_path / 'proxies.txt'
	path.write_text('a.test:1\nb.test:2\n')
	hub = SessionHub(tmp_path)
	s = Settings(proxy_file=path)
	src = hub.proxy_source(s)
	assert src is hub.proxy_source(s) and [src.acquire().host, src.acquire().host] == ['a.test', 'b.test']
	assert hub.proxy_source(Settings()) is None


@pytest.mark.asyncio
async def test_bulk_accounts_make_one_fresh_account_each_without_telling_the_agent_how_many(tmp_path, monkeypatch):
	seen, release = [], asyncio.Event()

	async def fake_run_task(task, settings, identity_name=None, max_steps=None, *, hooks, agent_kwargs):
		seen.append((identity_name, task, agent_kwargs['sensitive_data']['https://*.shop.test']['x_email']))
		await release.wait()
		return TaskResult(ok=True, output='SIGNUP_STATUS: signed_up - welcome', attempts=[])

	monkeypatch.setattr(runner_mod, 'run_task', fake_run_task)
	env = Settings(data_dir=tmp_path, max_parallel=2)
	app = create_app(env, port=7788)
	async with _client(app) as c:
		cfg = (await c.get('/api/config')).json()
		assert cfg['max_bulk'] == 25 and any(p['name'] == 'Reddit' for p in cfg['platforms'])
		assert 'deepseek' in cfg['providers'] and cfg['models']['deepseek']
		r = await c.post('/api/accounts/bulk', json={'url': 'shop.test', 'count': 2, 'values': {}})
		assert r.status_code == 400 and 'Accounts' in r.json()['detail']  # no profiles yet

		accounts.save_profiles(accounts.profiles_path(env), [{'id': f'p{i}', 'email': f'{i}@a.com'} for i in (1, 2, 3)])
		accounts.SignupLog(env.accounts_dir / 'signups.csv').record(
			accounts.Signup('shop.test', 'p1', '1@a.com', 'pw', 'signed_up', 'acct-shop.test-p1')
		)
		assert (await c.post('/api/accounts/bulk', json={'url': 'shop.test', 'count': 0, 'values': {}})).status_code == 400
		assert (await c.post('/api/accounts/bulk', json={'url': 'shop.test', 'count': 'x', 'values': {}})).status_code == 400
		assert (await c.post('/api/accounts/bulk', json={'url': 'shop.test', 'count': 2.9, 'values': {}})).status_code == 400
		assert (await c.post('/api/accounts/bulk', json={'url': 'shop.test', 'count': True, 'values': {}})).status_code == 400
		r = await c.post(
			'/api/accounts/bulk',
			json={'url': 'shop.test', 'count': 2, 'prompt': 'create 2 accounts with my work email', 'values': {}},
		)
		assert r.status_code == 200, r.text
		body = r.json()
		side = [s for s in body['sessions'] if s['group'] == body['group']]
		assert len(side) == 2 and not body['skipped']
		assert body['events'][0]['text'].startswith('Create 2 accounts on shop.test')
		await asyncio.sleep(0.05)
		release.set()
		for s in side:
			await asyncio.wait_for(app.state.hub.get(s['id'])._task, timeout=2)
		# One single-account instruction, identical for every run: the agent can't tell 2 accounts were asked for.
		tasks = {task for _, task, _ in seen}
		assert len(seen) == 2 and len(tasks) == 1
		task = tasks.pop()
		assert task.startswith('Create an account for me at https://shop.test') and 'create an account with my work email' in task
		assert '2 accounts' not in task and 'two accounts' not in task
		# A fresh person each, and p1 isn't reused: it already has an account there.
		assert sorted(identity for identity, _, _ in seen) == ['acct-shop.test-p2', 'acct-shop.test-p3']
		assert sorted(email for _, _, email in seen) == ['2@a.com', '3@a.com']
		log = accounts.SignupLog(env.accounts_dir / 'signups.csv')
		assert {row.profile_id: row.status for row in log.for_site('shop.test')} == {'p1': 'signed_up', 'p2': 'signed_up', 'p3': 'signed_up'}
		# No free people left for a new account there; the count must be sane; a site must be named.
		r = await c.post('/api/accounts/bulk', json={'url': 'shop.test', 'count': 1, 'values': {}})
		assert r.status_code == 400 and 'profiles' in r.json()['detail']
		assert (await c.post('/api/accounts/bulk', json={'url': 'shop.test', 'count': 0, 'values': {}})).status_code == 400
		assert (await c.post('/api/accounts/bulk', json={'url': 'shop.test', 'count': 99, 'values': {}})).status_code == 400
		assert (await c.post('/api/accounts/bulk', json={'url': '', 'count': 1, 'values': {}})).status_code == 400


@pytest.mark.asyncio
async def test_bulk_accounts_with_the_identity_server_asks_it_for_people(tmp_path, monkeypatch):
	seen = []

	async def fake_run_task(task, settings, identity_name=None, max_steps=None, *, hooks, agent_kwargs):
		seen.append((identity_name, task))
		return TaskResult(ok=True, output='SIGNUP_STATUS: signed_up - welcome', attempts=[])

	class People:
		"""The identity server's contract: no listing, people handed out on demand."""

		remote = True
		people = [{'id': 'p1', 'email': '1@a.com'}, {'id': 'p2', 'email': '2@a.com'}]

		def listing(self):
			return []

		def assign(self, site, purpose, exclude=()):
			for person in self.people:
				if person['id'] not in exclude:
					return person
			raise ValueError('no people left')

		def get(self, profile_id):
			return next(p for p in self.people if p['id'] == profile_id)

		def report(self, row, action, *, background=True):
			pass

		def code_fetcher(self, profile_id, site):
			return None

	monkeypatch.setattr(runner_mod, 'run_task', fake_run_task)
	monkeypatch.setattr(identities, 'profile_source', lambda settings, proxy_source=None: People())
	env = Settings(data_dir=tmp_path, max_parallel=2, identity_api='http://id.test')
	app = create_app(env, port=7788)
	async with _client(app) as c:
		r = await c.post('/api/accounts/bulk', json={'url': 'shop.test', 'count': 2, 'values': {}})
		assert r.status_code == 200, r.text  # an empty listing is not "no people": the server assigns on demand
		body = r.json()
		side = [s for s in body['sessions'] if s['group'] == body['group']]
		assert len(side) == 2
		for s in side:
			await asyncio.wait_for(app.state.hub.get(s['id'])._task, timeout=2)
		assert sorted(identity for identity, _ in seen) == ['acct-shop.test-p1', 'acct-shop.test-p2']
		assert len({task for _, task in seen}) == 1 and '1 account' not in seen[0][1]


def test_effort_goes_to_each_provider_its_own_way():
	llm = build_llm(Settings(llm_provider='openai', llm_model='gpt-6-luna', llm_api_key='sk-test', llm_effort='high'))
	assert llm.reasoning_effort == 'high' and 'gpt-6-luna' in llm.reasoning_models
	llm = build_llm(Settings(llm_provider='anthropic', llm_model='claude-opus-5-5', llm_api_key='sk-ant', llm_effort='xhigh'))
	assert llm.output_config == {'effort': 'xhigh'} and llm.thinking == {'type': 'adaptive'}
	llm = build_llm(Settings(llm_provider='anthropic', llm_model='claude-opus-5-5', llm_api_key='sk-ant', llm_effort='minimal'))
	assert llm.output_config is None  # not an Anthropic level: the provider's default
	llm = build_llm(Settings(llm_provider='anthropic', llm_model='claude-haiku-4-5', llm_api_key='sk-ant', llm_effort='high'))
	assert llm.thinking is None and llm.output_config is None  # predates adaptive thinking and effort
