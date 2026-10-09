from types import SimpleNamespace

import pytest

from veil import agent as agent_mod
from veil.config import Settings
from veil.detection import BlockSignal


class _History:
	def __init__(self, done: bool, success: bool | None, final: str):
		self.done, self.success, self.final = done, success, final

	def is_done(self):
		return self.done

	def is_successful(self):
		return self.success if self.done else None

	def errors(self):
		return [None]

	def final_result(self):
		return self.final


def _fakes(monkeypatch, history_for_attempt, blocked_attempts=()):
	launched = []

	class FakeSession:
		def __init__(self, settings, identity=None, proxy_source=None):
			self.proxy = self.exit = self.persona = self.browser = None
			launched.append(self)

		async def __aenter__(self):
			return self

		async def __aexit__(self, *exc):
			pass

		def report(self, **kw):
			pass

	class FakeGuard:
		def __init__(self, session):
			n = len(launched)
			self.signal = BlockSignal('cloudflare', 'block', 'title') if n in blocked_attempts else None

		async def __call__(self, agent):
			pass

	class FakeAgent:
		def __init__(self, **kw):
			pass

		async def run(self, max_steps, on_step_end):
			return history_for_attempt(len(launched))

	monkeypatch.setattr(agent_mod, 'StealthSession', FakeSession)
	monkeypatch.setattr(agent_mod, '_BlockGuard', FakeGuard)
	monkeypatch.setattr(agent_mod, 'Agent', FakeAgent)
	monkeypatch.setattr(agent_mod, 'build_llm', lambda s: None)
	monkeypatch.setattr(agent_mod, 'make_proxy_source', lambda s: None)
	return launched


@pytest.mark.asyncio
async def test_unfinished_run_reports_no_output(monkeypatch):
	# without a done action, final_result() is the last action's log line, not an answer
	_fakes(monkeypatch, lambda n: _History(done=False, success=None, final='🔗 Navigated to https://example.com'))
	r = await agent_mod.run_task('t', Settings(max_attempts=3))
	assert (r.ok, r.output, [a.outcome for a in r.attempts]) == (False, None, ['failed'])


@pytest.mark.asyncio
async def test_done_but_unsuccessful_keeps_the_agents_explanation(monkeypatch):
	_fakes(monkeypatch, lambda n: _History(done=True, success=False, final='Google requires phone verification'))
	r = await agent_mod.run_task('t', Settings())
	assert (r.ok, r.output) == (False, 'Google requires phone verification')


@pytest.mark.asyncio
async def test_stop_after_a_block_does_not_launch_another_identity(monkeypatch):
	stop = {'now': False}
	launched = _fakes(monkeypatch, lambda n: stop.update(now=True) or _History(True, True, 'x'), blocked_attempts={1})
	r = await agent_mod.run_task('t', Settings(max_attempts=3), hooks=agent_mod.RunHooks(stop_requested=lambda: stop['now']))
	assert len(launched) == 1 and [a.outcome for a in r.attempts] == ['blocked']
