"""Run a browser-use agent task inside a stealth session, rotating identity when the site blocks it."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from browser_use import Agent

from veil.config import Settings
from veil.detection import BlockSignal
from veil.llm import build_llm
from veil.session import Identity, StealthSession, make_proxy_source

log = logging.getLogger('veil.agent')

# A challenge page (e.g. Cloudflare's "Just a moment...") often clears by itself for a clean
# fingerprint; give it this long before treating it as a block.
CHALLENGE_GRACE_SECONDS = 12


@dataclass
class Attempt:
	proxy: str | None
	exit_ip: str | None
	persona: str | None
	outcome: str  # done | blocked | failed | error
	detail: str = ''


@dataclass
class TaskResult:
	ok: bool
	output: str | None
	attempts: list[Attempt] = field(default_factory=list)


class _BlockGuard:
	"""on_step_end hook: stop the agent as soon as it lands on a block/challenge that won't clear."""

	def __init__(self, session: StealthSession):
		self.session = session
		self.signal: BlockSignal | None = None

	async def __call__(self, agent: Agent) -> None:
		signal = await self.session.check_block()
		if signal and signal.kind == 'challenge':
			log.info('challenge detected (%s, %s); waiting up to %ss for it to clear', signal.vendor, signal.evidence, CHALLENGE_GRACE_SECONDS)
			for _ in range(CHALLENGE_GRACE_SECONDS // 2):
				await asyncio.sleep(2)
				signal = await self.session.check_block()
				if not signal:
					return
		if signal:
			log.warning('blocked by %s (%s): rotating identity', signal.vendor, signal.evidence)
			self.signal = signal
			agent.stop()


async def run_task(task: str, settings: Settings, identity_name: str | None = None, max_steps: int | None = None) -> TaskResult:
	llm = build_llm(settings)
	proxy_source = make_proxy_source(settings)  # shared across attempts so cooldowns carry over
	identity = Identity.load_or_create(settings, identity_name) if identity_name else None
	# A named identity is supposed to stay the same person; rotating it on a block would defeat that.
	attempts_allowed = 1 if identity else settings.max_attempts
	result = TaskResult(ok=False, output=None)

	for n in range(1, attempts_allowed + 1):
		log.info('attempt %d/%d', n, attempts_allowed)
		session = StealthSession(settings, identity=identity, proxy_source=proxy_source)
		attempt = Attempt(proxy=None, exit_ip=None, persona=None, outcome='error')
		result.attempts.append(attempt)
		try:
			async with session:
				attempt.proxy = session.proxy.redacted if session.proxy else None
				attempt.exit_ip = session.exit.ip if session.exit else None
				attempt.persona = session.persona.summary() if session.persona else None
				guard = _BlockGuard(session)
				agent = Agent(task=task, llm=llm, browser_session=session.browser)
				history = await agent.run(max_steps=max_steps or settings.max_steps, on_step_end=guard)

				if guard.signal:
					session.report(ok=False, blocked=True)
					attempt.outcome, attempt.detail = 'blocked', f'{guard.signal.vendor}: {guard.signal.evidence}'
					continue
				done = history.is_done() and history.is_successful() is not False
				session.report(ok=done)
				attempt.outcome = 'done' if done else 'failed'
				attempt.detail = '; '.join(e for e in history.errors() if e)[-500:]
				result.ok, result.output = done, history.final_result()
				return result  # the agent finished (or gave up) on its own terms; retrying won't help
		except Exception as e:
			log.exception('attempt %d crashed', n)
			attempt.outcome, attempt.detail = 'error', f'{type(e).__name__}: {e}'
	return result
