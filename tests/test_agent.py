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
		def __init__(self, session, request_human=None):
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


class _ProbeSession:
	"""A StealthSession stand-in whose page is whatever the test says."""

	def __init__(self, pages):
		self.pages = list(pages)

	async def probe_page(self):
		return self.pages.pop(0) if len(self.pages) > 1 else self.pages[0]


class _StopAgent:
	stopped = False

	def stop(self):
		self.stopped = True


RECAPTCHA = '<iframe src="https://www.google.com/recaptcha/api2/anchor?ar=1&k=abc&size=normal"></iframe>'


@pytest.mark.asyncio
async def test_guard_hands_a_captcha_to_the_person_once(monkeypatch):
	asks = []
	page = {'url': 'https://site.test/signup?x=1', 'title': 'Sign up', 'html': RECAPTCHA}
	guard = agent_mod._BlockGuard(_ProbeSession([page]), request_human=lambda reason, detail: asks.append(detail))
	agent = _StopAgent()
	await guard(agent)
	await guard(agent)  # the solved widget stays in the DOM: don't ask again for the same page
	assert len(asks) == 1 and asks[0]['vendor'] == 'recaptcha' and asks[0]['source'] == 'detector'
	assert not agent.stopped and guard.signal is None


@pytest.mark.asyncio
async def test_guard_asks_before_rotating_on_a_challenge_then_rotates_if_it_stays(monkeypatch):
	monkeypatch.setattr(agent_mod, 'CHALLENGE_GRACE_SECONDS', 0)
	asks = []
	page = {'url': 'https://site.test/', 'title': 'Just a moment...', 'html': ''}
	guard = agent_mod._BlockGuard(_ProbeSession([page]), request_human=lambda reason, detail: asks.append(reason))
	agent = _StopAgent()
	await guard(agent)
	assert asks and not agent.stopped
	await guard(agent)  # the person had a go and it's still a challenge: give up on this identity
	assert agent.stopped and guard.signal.vendor == 'cloudflare'


@pytest.mark.asyncio
async def test_guard_rotates_a_hard_block_without_asking_and_never_asks_without_a_hook():
	blocked = {'url': 'https://site.test/', 'title': 'Attention Required! | Cloudflare', 'html': ''}
	asks = []
	guard = agent_mod._BlockGuard(_ProbeSession([blocked]), request_human=lambda r, d: asks.append(r))
	agent = _StopAgent()
	await guard(agent)
	assert agent.stopped and not asks
	cli_guard = agent_mod._BlockGuard(_ProbeSession([{'url': 'https://a.test/', 'title': '', 'html': RECAPTCHA}]))
	agent = _StopAgent()
	await cli_guard(agent)  # the CLI has nobody to ask: a CAPTCHA widget alone isn't a block
	assert not agent.stopped and cli_guard.signal is None
