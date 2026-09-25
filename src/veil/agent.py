"""Run a browser-use agent task inside a stealth session, rotating identity when the site blocks it."""
from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

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
	outcome: str  # done | blocked | failed | stopped | error
	detail: str = ''


@dataclass
class TaskResult:
	ok: bool
	output: str | None
	attempts: list[Attempt] = field(default_factory=list)


@dataclass
class RunHooks:
	"""Optional callbacks for a UI that wants to watch and steer a run (the CLI passes none).

	on_attempt(n, total, session, agent) fires once the identity is up and the agent exists, before
	it runs: the place to show the persona and exit IP, and to keep `agent` for pause/stop.
	on_step is browser-use's register_new_step_callback(state_summary, model_output, step_number).
	on_attempt_end(n, attempt) fires after each attempt with its outcome.
	stop_requested() says the user stopped the run (browser-use's own `stopped` flag also covers a crashed browser).
	"""

	on_attempt: Callable[[int, int, StealthSession, Agent], Awaitable[None] | None] | None = None
	on_step: Callable[..., Awaitable[None] | None] | None = None
	on_attempt_end: Callable[[int, Attempt], Awaitable[None] | None] | None = None
	stop_requested: Callable[[], bool] | None = None


async def _call(fn, *args) -> None:
	if fn is None:
		return
	res = fn(*args)
	if inspect.isawaitable(res):
		await res


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


async def run_task(
	task: str,
	settings: Settings,
	identity_name: str | None = None,
	max_steps: int | None = None,
	*,
	hooks: RunHooks | None = None,
	agent_kwargs: dict[str, Any] | None = None,
) -> TaskResult:
	hooks = hooks or RunHooks()
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
				agent = Agent(task=task, llm=llm, browser_session=session.browser, register_new_step_callback=hooks.on_step, **(agent_kwargs or {}))
				await _call(hooks.on_attempt, n, attempts_allowed, session, agent)
				history = await agent.run(max_steps=max_steps or settings.max_steps, on_step_end=guard)

				if guard.signal:
					session.report(ok=False, blocked=True)
					attempt.outcome, attempt.detail = 'blocked', f'{guard.signal.vendor}: {guard.signal.evidence}'
					await _call(hooks.on_attempt_end, n, attempt)
					continue
				if hooks.stop_requested and hooks.stop_requested():  # the user pressed Stop: not the proxy's fault, and no answer
					attempt.outcome = 'stopped'
					await _call(hooks.on_attempt_end, n, attempt)
					return result
				done = history.is_done() and history.is_successful() is not False
				session.report(ok=done)
				attempt.outcome = 'done' if done else 'failed'
				attempt.detail = '; '.join(e for e in history.errors() if e)[-500:]
				result.ok, result.output = done, history.final_result()
				await _call(hooks.on_attempt_end, n, attempt)
				return result  # the agent finished (or gave up) on its own terms; retrying won't help
		except Exception as e:
			log.exception('attempt %d crashed', n)
			attempt.outcome, attempt.detail = 'error', f'{type(e).__name__}: {e}'
			await _call(hooks.on_attempt_end, n, attempt)
	return result
