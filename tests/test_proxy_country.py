"""Country-matched exits: a person's runs exit in their profile's country when a proxy there exists."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from veil import accounts
from veil.agent import TaskResult
from veil.config import Settings
from veil.geo import country_code
from veil.proxy import GatewayProxy, Proxy, ProxyPool, load_countries, proxy_id
from veil.session import make_proxy_source
from veil.webui import runner as runner_mod
from veil.webui.runner import AgentRunner, RunRequest


def test_country_code_reads_names_and_codes():
	assert country_code('India') == country_code('in') == 'IN'
	assert country_code(' United Kingdom ') == 'GB'
	assert country_code('') is None and country_code('Atlantis') is None


def test_pool_prefers_a_proxy_in_the_asked_country_and_falls_back():
	de, us1, us2 = Proxy.parse('de.test:1'), Proxy.parse('us1.test:1'), Proxy.parse('us2.test:1')
	pool = ProxyPool([de, us1, us2], countries={de: 'DE', us1: 'US', us2: 'US'})
	assert pool.acquire(country='de') == de
	assert {pool.acquire(country='US'), pool.acquire(country='US')} == {us1, us2}
	assert pool.acquire(country='JP') in {de, us1, us2}  # none there: any proxy (the run notes the mismatch)


def test_gateway_fills_in_the_country():
	gw = GatewayProxy('http://login__cr.{country};sessid.{session}:pw@gw.test:823', default_country='us')
	assert gw.by_country and gw.acquire('abc', country='DE').username == 'login__cr.de;sessid.abc'
	assert gw.acquire('abc').username == 'login__cr.us;sessid.abc'
	assert not GatewayProxy('http://u-{session}:pw@gw.test:1').by_country


def test_list_learns_countries_from_its_last_test(tmp_path):
	(tmp_path / 'proxies.txt').write_text('de.test:1\nus.test:1\nnew.test:1\n')
	de, us = Proxy.parse('de.test:1'), Proxy.parse('us.test:1')
	(tmp_path / 'proxy-status.json').write_text(json.dumps({
		proxy_id(de): {'ok': True, 'country': 'de'}, proxy_id(us): {'ok': False, 'error': 'timed out', 'country': 'US'},
	}))
	assert load_countries(tmp_path / 'proxy-status.json', [de, us]) == {de: 'DE'}  # a failed test teaches nothing
	pool = make_proxy_source(Settings(data_dir=tmp_path, proxy_file=tmp_path / 'proxies.txt'))
	assert pool.countries == {de: 'DE'} and pool.acquire(country='DE') == de


@pytest.mark.asyncio
async def test_account_runs_ask_for_the_persons_country(tmp_path, monkeypatch):
	from httpx import ASGITransport, AsyncClient

	from veil.webui.server import create_app

	seen = {}

	async def fake_run_task(task, settings, identity_name=None, max_steps=None, *, hooks, agent_kwargs, country=None):
		seen['country'] = country
		return TaskResult(ok=True, output='SIGNUP_STATUS: signed_up - welcome', attempts=[])

	monkeypatch.setattr(runner_mod, 'run_task', fake_run_task)
	env = Settings(data_dir=tmp_path)
	accounts.save_profiles(accounts.profiles_path(env), [{'id': 'p1', 'email': 'a@a.com', 'country': 'Germany'}])
	app = create_app(env, port=7788)
	async with AsyncClient(transport=ASGITransport(app=app), base_url='http://127.0.0.1:7788') as c:
		assert (await c.post('/api/accounts/signup', json={'url': 'shop.test', 'profile_id': 'p1'})).status_code == 200
		await asyncio.wait_for(app.state.runner._task, timeout=2)
	assert seen['country'] == 'DE'


@pytest.mark.asyncio
async def test_a_run_says_when_no_exit_matches_the_persons_country(tmp_path, monkeypatch):
	async def fake_run_task(task, settings, identity_name=None, max_steps=None, *, hooks, agent_kwargs, country=None):
		session = SimpleNamespace(persona=None, exit=SimpleNamespace(ip='1.1.1.1', country='US', city=None, timezone='UTC'),
			proxy=None, identity=None, settings=settings, browser=None)
		await hooks.on_attempt(1, 1, session, SimpleNamespace(stop=lambda: None))
		return TaskResult(ok=True, output='done', attempts=[])

	monkeypatch.setattr(runner_mod, 'run_task', fake_run_task)
	r = AgentRunner(runs_dir=tmp_path)
	r.start(RunRequest(task='sign up', settings=Settings(), country='IN'))
	await asyncio.wait_for(r._task, timeout=2)
	assert any(e['type'] == 'notice' and 'in IN but the exit IP is in US' in e['text'] for e in r.events)
