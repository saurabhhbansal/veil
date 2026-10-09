import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from veil.agent import Attempt, TaskResult
from veil.config import Settings
from veil.webui import runner as runner_mod
from veil.webui.manager import WebuiManager, settings_from_ui
from veil.webui.runner import AgentRunner, RunRequest, step_markdown


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


def test_saved_configs_never_contain_secrets_and_stay_in_their_dir(tmp_path):
	m = WebuiManager(config_dir=tmp_path / 'configs')
	m.add_components('agent_settings', {'llm_api_key': object(), 'llm_model': object()})
	m.add_components('stealth_settings', {'proxy_gateway': object()})
	path = m.save_config({'agent_settings.llm_api_key': 'sk-secret', 'agent_settings.llm_model': 'x', 'stealth_settings.proxy_gateway': 'http://u:p@h:1'})
	assert json.loads(path.read_text()) == {'agent_settings.llm_model': 'x'}
	assert m.load_config('../configs/' + path.name) == {'agent_settings.llm_model': 'x'}  # only the bare name is used


def test_step_markdown_shows_goal_and_actions():
	action = SimpleNamespace(model_dump=lambda exclude_none: {'navigate': {'url': 'https://example.com'}})
	out = SimpleNamespace(evaluation_previous_goal='ok', memory=None, next_goal='open the page', action=[action])
	md = step_markdown(SimpleNamespace(url='https://example.com', title='Example'), out, 2)
	assert '**Step 2**' in md and 'open the page' in md and '`navigate`' in md and 'Memory' not in md


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
	assert r.status == 'running' and 'Attempt 1/3' in r.identity_md

	r.stop()
	assert r.status == 'stopping'
	await asyncio.wait_for(r._task, timeout=2)
	assert r.status == 'idle' and not r.busy
	last = r.messages[-1]['content']
	assert 'Stopped' in last and 'Navigated to' not in last  # a stop shows no fake "result"
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
	assert r.status == 'idle' and 'before the agent started' in r.messages[-1]['content']
