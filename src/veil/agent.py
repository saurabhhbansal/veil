"""Run a browser-use agent task inside a stealth session, rotating identity when the site blocks it."""
from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from browser_use import ActionResult, Agent, Tools

from veil.autopilot import AutoPilot
from veil.config import Settings
from veil.detection import BlockSignal, classify, find_captcha
from veil.llm import build_llm
from veil.proxy import ProxySource
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
	request_human(reason, detail) asks the person watching to take over the browser (a CAPTCHA, a 2FA code). It
	should pause the agent and return; the UI resumes it once the person is done. With it set, a CAPTCHA or a
	challenge page that doesn't clear is handed to the person first instead of burning the identity, and the
	agent gets a `request_human_help` action. Without it (the CLI), a challenge still rotates the identity.
	"""

	on_attempt: Callable[[int, int, StealthSession, Agent], Awaitable[None] | None] | None = None
	on_step: Callable[..., Awaitable[None] | None] | None = None
	on_attempt_end: Callable[[int, Attempt], Awaitable[None] | None] | None = None
	stop_requested: Callable[[], bool] | None = None
	request_human: Callable[[str, dict[str, Any]], Awaitable[None] | None] | None = None
	# on_step_start(agent) runs before each step builds its prompt: the place to hand the agent a steer.
	on_step_start: Callable[[Agent], Awaitable[None] | None] | None = None
	# take_followups() -> instructions that arrived too late for a step; the same agent continues with them.
	take_followups: Callable[[], list[str]] | None = None
	# on_notice(level, text) reports what veil did behind the agent's back (solved a CAPTCHA, read an emailed code):
	# the person watching sees it in the app; the agent never does.
	on_notice: Callable[[str, str], Awaitable[None] | None] | None = None


async def _call(fn, *args) -> None:
	if fn is None:
		return
	res = fn(*args)
	if inspect.isawaitable(res):
		await res


def _page_key(vendor: str, url: str) -> tuple[str, str]:
	return vendor, (url or '').split('#')[0].split('?')[0]


class _BlockGuard:
	"""on_step_end hook: stop the agent as soon as it lands on a block/challenge that won't clear.

	With `request_human`, a CAPTCHA or an unsolved challenge goes to the person first (once per page); only if
	the page is still a challenge after they had a go does the guard rotate the identity.
	"""

	def __init__(self, session: StealthSession, request_human=None, autopilot: AutoPilot | None = None):
		self.session = session
		self.request_human = request_human
		self.autopilot = autopilot
		self.signal: BlockSignal | None = None
		self.handed_over: set[tuple[str, str]] = set()  # (vendor, page) the person already had a go at

	async def _page(self) -> tuple[dict | None, BlockSignal | None]:
		page = await self.session.probe_page()
		return page, classify(page['url'], page['title'], page['html']) if page else None

	async def __call__(self, agent: Agent) -> None:
		page, signal = await self._page()
		if signal and signal.kind == 'challenge':
			log.info('challenge detected (%s, %s); waiting up to %ss for it to clear', signal.vendor, signal.evidence, CHALLENGE_GRACE_SECONDS)
			for _ in range(CHALLENGE_GRACE_SECONDS // 2):
				await asyncio.sleep(2)
				page, signal = await self._page()
				if not signal:
					break
		if not page:
			return
		captcha = find_captcha(page['html']) if (self.request_human or self.autopilot) else None
		# A hard block (Cloudflare 1020, Akamai) has nothing to solve, unless it carries a CAPTCHA (Google /sorry/).
		human = captcha if captcha else (signal if signal and signal.kind == 'challenge' else None)
		finished = getattr(agent, 'history', None) is not None and agent.history.is_done()
		if human and not finished:  # a finished agent has nothing left to do on the page
			key = _page_key(human.vendor, page['url'])
			# Invisible first: a solver token put into the page, or a code read from the account's mailbox.
			if self.autopilot and await self.autopilot.try_page(page, signal):
				return  # the agent carries on; it is never told what happened
			if self.request_human and key not in self.handed_over:
				self.handed_over.add(key)
				log.info('%s %s on %s: asking the person to solve it', human.vendor, human.kind, page['url'])
				await _call(
					self.request_human,
					f'{human.vendor} {"CAPTCHA" if human.kind == "captcha" else "challenge"} on this page',
					{'source': 'detector', 'vendor': human.vendor, 'kind': human.kind, 'url': page['url'], 'evidence': human.evidence},
				)
				return
		if signal:
			log.warning('blocked by %s (%s): rotating identity', signal.vendor, signal.evidence)
			self.signal = signal
			agent.stop()


def _tools(request_human=None, fetch_code=None, autopilot: AutoPilot | None = None) -> Tools:
	"""browser-use's default actions, plus one to hand the browser to the person watching and/or ones to read
	a one-time code or an email confirmation link (the autopilot reads it from the account's own mailbox, but
	the agent only sees a neutral tool)."""
	tools = Tools()

	if request_human:

		@tools.action(
			'Ask the human operator to take over the browser for something only a person can do: a CAPTCHA or '
			'"verify you are human" check, a 2FA / SMS / email code, or a login you have no credentials for. '
			'The run pauses until they finish. Do not use it for anything you can do yourself.'
		)
		async def request_human_help(reason: str) -> ActionResult:
			# Veil first tries the invisible route (solver token, code from the account's mailbox): if it works the
			# agent just gets the outcome, so its request never has to mention mail or CAPTCHA services.
			if autopilot is not None:
				handled = await autopilot.help(reason, {'source': 'agent'})
				if handled:
					return ActionResult(extracted_content=handled, long_term_memory=handled)
			await _call(request_human, reason, {'source': 'agent'})
			msg = f'Asked the human operator for help: {reason}. They take over the browser before your next step.'
			return ActionResult(extracted_content=msg, long_term_memory=msg)

	if fetch_code:

		@tools.action(
			"Get the one-time verification code the site just sent to the account's email or phone. channel is 'sms' "
			"or 'email'. Waits up to a minute and a half for it to arrive. Call it after the site says it sent a code."
		)
		async def get_verification_code(channel: str) -> ActionResult:
			try:
				code = await fetch_code(channel)
			except Exception as e:
				log.warning('verification code lookup failed: %s', e)
				code = None
			if not code:
				msg = f'No {channel} code arrived. Treat the code as something only the human can provide.'
				return ActionResult(extracted_content=msg, long_term_memory=msg)
			# The code itself stays out of long-term memory: it's single-use and needed only for the next input.
			return ActionResult(extracted_content=f'The {channel} verification code is {code}', long_term_memory=f'Fetched the {channel} code.')

		@tools.action(
			"Get the confirmation link the site just emailed to the account (\"verify your email\", \"confirm your account\"). "
			'Waits up to a minute and a half for it to arrive. Open the link it returns in the current tab with navigate.'
		)
		async def get_verification_link() -> ActionResult:
			try:
				link = await fetch_code('link')
			except Exception as e:
				log.warning('verification link lookup failed: %s', e)
				link = None
			if not link:
				msg = 'No confirmation link arrived. Report that the site asked to confirm the email.'
				return ActionResult(extracted_content=msg, long_term_memory=msg)
			return ActionResult(extracted_content=f'The confirmation link is {link}', long_term_memory='Fetched the email confirmation link.')

	return tools


async def run_task(
	task: str,
	settings: Settings,
	identity_name: str | None = None,
	max_steps: int | None = None,
	*,
	hooks: RunHooks | None = None,
	agent_kwargs: dict[str, Any] | None = None,
	proxy_source: ProxySource | None = None,
	country: str | None = None,
	fetch_code: Callable[[str], Awaitable[str | None]] | None = None,
) -> TaskResult:
	"""proxy_source: share one between runs that go at the same time, so each takes a different proxy.
	country: ISO code of where the person is (an account run): the exit IP is picked there when the source can.
	fetch_code(channel): reads the account's one-time codes ('sms' | 'email') and confirmation links ('link')
	(identities.IdentityAPI.code_fetcher); the agent gets get_verification_code / get_verification_link actions for it."""
	hooks = hooks or RunHooks()
	llm = build_llm(settings)
	proxy_source = proxy_source or make_proxy_source(settings)  # shared across attempts so cooldowns carry over
	identity = Identity.load_or_create(settings, identity_name) if identity_name else None
	# A named identity is supposed to stay the same person; rotating it on a block would defeat that.
	attempts_allowed = 1 if identity else settings.max_attempts
	result = TaskResult(ok=False, output=None)

	for n in range(1, attempts_allowed + 1):
		if hooks.stop_requested and hooks.stop_requested():  # Stop pressed after a block or crash: don't launch another identity
			break
		log.info('attempt %d/%d', n, attempts_allowed)
		session = StealthSession(settings, identity=identity, proxy_source=proxy_source, **({'country': country} if country else {}))
		attempt = Attempt(proxy=None, exit_ip=None, persona=None, outcome='error')
		result.attempts.append(attempt)
		try:
			async with session:
				attempt.proxy = session.proxy.redacted if session.proxy else None
				attempt.exit_ip = session.exit.ip if session.exit else None
				attempt.persona = session.persona.summary() if session.persona else None
				# The invisible in-between: emailed codes and CAPTCHA tokens are handled here, not by the agent.
				autopilot = AutoPilot(session, settings, fetch_code=fetch_code, notify=hooks.on_notice)

				async def notified_code(channel: str) -> str | None:
					code = await fetch_code(channel)  # type: ignore[misc]
					if code:
						await _call(hooks.on_notice, 'info', f'Read the {channel} verification code automatically.')
					return code

				guard = _BlockGuard(session, hooks.request_human, autopilot=autopilot)
				kwargs = dict(agent_kwargs or {})
				if (hooks.request_human or fetch_code) and 'tools' not in kwargs:
					kwargs['tools'] = _tools(hooks.request_human, notified_code if fetch_code else None, autopilot)
				agent = Agent(task=task, llm=llm, browser_session=session.browser, register_new_step_callback=hooks.on_step, **kwargs)
				await _call(hooks.on_attempt, n, attempts_allowed, session, agent)

				async def step_start(a: Agent) -> None:
					await _call(hooks.on_step_start, a)

				budget = max_steps or settings.max_steps
				history = await agent.run(max_steps=budget, on_step_start=step_start, on_step_end=guard)
				# Instructions the person sent while the agent was finishing: carry on in the same browser.
				while hooks.take_followups and not guard.signal and not (hooks.stop_requested and hooks.stop_requested()):
					followups = hooks.take_followups()
					if not followups:
						break
					agent.add_new_task('The person watching added: ' + ' / '.join(f'"{t}"' for t in followups) + '. Do this now, then finish with done.')
					history = await agent.run(max_steps=agent.state.n_steps + budget, on_step_start=step_start, on_step_end=guard)

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
				# Without a done action, final_result() is just the last action's log line (e.g. "Navigated to ..."), not an answer.
				result.ok, result.output = done, history.final_result() if history.is_done() else None
				await _call(hooks.on_attempt_end, n, attempt)
				return result  # the agent finished (or gave up) on its own terms; retrying won't help
		except Exception as e:
			log.exception('attempt %d crashed', n)
			attempt.outcome, attempt.detail = 'error', f'{type(e).__name__}: {e}'
			await _call(hooks.on_attempt_end, n, attempt)
	return result
