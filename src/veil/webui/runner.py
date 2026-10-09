"""One agent run at a time, driven from the web UI: state, an event log, pause/stop and human takeover.

Kept free of any UI framework so it can be tested: the chat app reads `snapshot()` (structured `events`).
Nothing here pushes to a browser.

Human takeover: the person can take the browser over at any time (`take_over`), and the agent asks for it
itself on a CAPTCHA or an unsolved challenge (RunHooks.request_human). Either way the agent is paused with
status 'needs_human' until `hand_back`, which tells the agent what happened and resumes it.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from browser_use import ActionResult

from veil.agent import Attempt, RunHooks, TaskResult, run_task
from veil.config import Settings

log = logging.getLogger('veil.webui')


@dataclass
class RunRequest:
	task: str
	settings: Settings
	identity: str | None = None
	max_steps: int | None = None
	agent_kwargs: dict[str, Any] = field(default_factory=dict)
	title: str | None = None  # what the chat shows instead of `task` (e.g. a generated sign-up prompt)
	kind: str = 'task'  # task | signup
	on_done: Callable[[TaskResult], None] | None = None  # called with the result once the agent has finished or stopped


def identity_info(n: int, total: int, session) -> dict[str, Any]:
	"""The identity panel: what the site sees this attempt."""
	p, ex, s = session.persona, session.exit, session.settings
	info: dict[str, Any] = {
		'attempt': n, 'total': total, 'engine': s.engine, 'headless': s.headless,
		'proxy': session.proxy.redacted if session.proxy else None,
		'sticky': session.identity.name if session.identity else None,
	}
	if ex:
		info['exit'] = {'ip': ex.ip, 'country': ex.country, 'city': ex.city, 'timezone': ex.timezone}
	if p:
		info['device'] = {
			'windows': p.windows, 'machine_class': p.machine_class, 'gpu': p.webgpu_description, 'cores': p.hw_concurrency,
			'memory': p.device_memory, 'screen': f'{p.screen_width}x{p.screen_height}', 'timezone': p.timezone,
			'languages': p.languages, 'canvas_seed': p.canvas_seed, 'audio_seed': p.audio_seed,
		}
	return info


def step_actions(output) -> list[dict[str, Any]]:
	actions = []
	for a in getattr(output, 'action', None) or []:
		for name, args in a.model_dump(exclude_none=True).items():
			actions.append({'name': name, 'args': args if isinstance(args, dict) else {'value': args}})
	return actions


class AgentRunner:
	"""Holds the current run. Methods are called from Gradio handlers on the same event loop."""

	def __init__(self, runs_dir: Path):
		self.runs_dir = runs_dir
		self.events: list[dict[str, Any]] = []
		self.identity: dict[str, Any] | None = None
		self.human: dict[str, Any] | None = None  # what the person is asked to do while status is 'needs_human'
		self.status = 'idle'  # idle | starting | running | paused | needs_human | stopping
		self.version = 0  # bumps whenever events/identity change, so the UI only re-sends on change
		self.run_dir: Path | None = None
		self.result: TaskResult | None = None
		self._task: asyncio.Task | None = None
		self._agent = None
		self._session = None
		self._stop_requested = False

	# --- state -------------------------------------------------------------------------------

	@property
	def busy(self) -> bool:
		return self._task is not None and not self._task.done()

	def _event(self, event_type: str, /, **data: Any) -> None:
		self.events.append({**data, 'id': len(self.events), 'type': event_type, 'time': time.time()})
		self.version += 1

	def _file_url(self, path: Path) -> str | None:
		return f'/api/runs/{path.parent.name}/{path.name}' if self.run_dir and path.parent == self.run_dir else None

	def snapshot(self) -> dict[str, Any]:
		"""Everything the chat app shows, as JSON-ready data."""
		gif, res = self.gif_path, self.result_path
		return {
			'version': self.version,
			'status': self.status,
			'busy': self.busy,
			'events': self.events,
			'identity': self.identity,
			'human': self.human,
			'browser_open': self._session is not None and getattr(self._session, 'browser', None) is not None,
			'gif': self._file_url(Path(gif)) if gif and self.status == 'idle' else None,
			'result_file': self._file_url(Path(res)) if res else None,
		}

	# --- control -----------------------------------------------------------------------------

	def start(self, req: RunRequest) -> None:
		if self.busy:
			raise RuntimeError('a task is already running')
		self.run_dir = self.runs_dir / f'{time.strftime("%Y%m%d-%H%M%S")}-{secrets.token_hex(3)}'
		self.run_dir.mkdir(parents=True, exist_ok=True)
		self.result, self._agent, self._session, self._stop_requested = None, None, None, False
		self.human, self.identity = None, None
		self.status = 'starting'
		self._event('task', text=req.title or req.task, kind=req.kind, run=self.run_dir.name)
		self._task = asyncio.create_task(self._run(req))

	def pause_resume(self) -> None:
		if self.status == 'needs_human':
			self.hand_back()
			return
		if not self._agent or self.status not in ('running', 'paused'):
			return
		if self.status == 'paused':
			self._agent.resume()
			self.status = 'running'
		else:
			self._agent.pause()
			self.status = 'paused'

	def take_over(self) -> bool:
		"""The person takes the browser: pause the agent (before its next step) until hand_back."""
		if not self._agent or self.status not in ('running', 'paused'):
			return False
		self._ask_human('You took over the browser', {'source': 'user'})
		return True

	def hand_back(self, note: str = '') -> bool:
		"""The person is done: tell the agent what happened and let it carry on."""
		if self.status != 'needs_human' or not self._agent:
			return False
		h, note = self.human or {}, (note or '').strip()
		what = {'user': 'took over the browser', 'detector': f'was asked to solve: {h.get("reason")}'}.get(
			h.get('source', ''), f'was asked for help: {h.get("reason")}'
		)
		msg = f'The human operator {what}, and has handed control back.' + (f' Their note: {note}' if note else '')
		msg += ' The page may have changed: check the current browser state before your next action.'
		state = self._agent.state
		state.last_result = [*(state.last_result or []), ActionResult(long_term_memory=msg)]
		self._resolve_human(note)
		self.status = 'running'
		self._agent.resume()
		return True

	def _ask_human(self, reason: str, detail: dict[str, Any]) -> None:
		self._agent.pause()  # takes effect before the next step
		self.status = 'needs_human'
		self.human = {'reason': reason, 'since': time.time(), **detail}
		self._event('human', reason=reason, resolved=False, **detail)

	def _resolve_human(self, note: str = '', outcome: str = 'handed back') -> None:
		for e in reversed(self.events):
			if e['type'] == 'human' and not e.get('resolved'):
				e.update(resolved=True, note=note, outcome=outcome)
				break
		self.human = None
		self.version += 1

	def stop(self) -> None:
		if not self.busy:
			return
		self._stop_requested = True
		self.status = 'stopping'
		if self.human:
			self._resolve_human(outcome='stopped')
		if self._agent:
			self._agent.stop()  # finishes the current step, then run() returns
		else:
			self._task.cancel()  # still launching the identity: nothing to finish

	async def clear(self) -> None:
		if self.busy:
			self.stop()
			try:
				await asyncio.wait_for(asyncio.shield(self._task), timeout=15)
			except (asyncio.TimeoutError, asyncio.CancelledError):
				self._task.cancel()
				# Let the run finish unwinding first, or its last message lands in the cleared chat.
				await asyncio.wait({self._task}, timeout=15)
		self.events, self.identity, self.human, self.result = [], None, None, None
		self.status = 'idle'
		self.version += 1

	async def screenshot(self) -> bytes | None:
		"""Current viewport as JPEG, for the live view. None when no browser is up."""
		s = self._session
		if not s or not s.browser or self.status not in ('running', 'paused', 'needs_human', 'stopping'):
			return None
		try:
			return await asyncio.wait_for(s.browser.take_screenshot(format='jpeg', quality=60), timeout=5)
		except Exception as e:
			log.debug('live screenshot failed: %s', e)
			return None

	# --- the run -----------------------------------------------------------------------------

	@property
	def session(self):
		"""The StealthSession of the attempt in progress (None between attempts)."""
		return self._session

	async def _on_request_human(self, reason: str, detail: dict[str, Any]) -> None:
		if not self._agent or self._stop_requested or self.status == 'needs_human':
			return  # nothing to pause, or the person already has the browser
		self._ask_human(reason, detail)

	async def _on_attempt(self, n: int, total: int, session, agent) -> None:
		self._session, self._agent = session, agent
		self.identity = identity_info(n, total, session)
		self._event('attempt', identity=self.identity)
		if self._stop_requested:  # Stop was pressed while the identity was launching
			agent.stop()
		elif self.status != 'stopping':
			self.status = 'running'

	async def _on_step(self, summary, output, n: int) -> None:
		shot, shot_url = getattr(summary, 'screenshot', None), None
		if shot and self.run_dir:
			path = self.run_dir / f'step-{n:03d}.png'
			try:
				path.write_bytes(base64.b64decode(shot))
				shot_url = self._file_url(path)
			except Exception as e:
				log.debug('could not save step screenshot: %s', e)
		self._event(
			'step', n=n, url=getattr(summary, 'url', '') or '', title=getattr(summary, 'title', '') or '',
			evaluation=getattr(output, 'evaluation_previous_goal', None), memory=getattr(output, 'memory', None),
			next_goal=getattr(output, 'next_goal', None), actions=step_actions(output), screenshot=shot_url,
		)

	async def _on_attempt_end(self, n: int, attempt: Attempt) -> None:
		self._agent, self._session = None, None
		if self.human:
			self._resolve_human(outcome='attempt ended')
		if self.status == 'needs_human':
			self.status = 'starting'
		if attempt.outcome == 'blocked':
			self._event('notice', level='blocked', text=f'Blocked on attempt {n}: {attempt.detail}. Rotating to a fresh device and exit IP.')
		elif attempt.outcome == 'error':
			self._event('notice', level='error', text=f'Attempt {n} crashed: {attempt.detail}')

	async def _run(self, req: RunRequest) -> None:
		hooks = RunHooks(
			on_attempt=self._on_attempt, on_step=self._on_step, on_attempt_end=self._on_attempt_end,
			stop_requested=lambda: self._stop_requested, request_human=self._on_request_human,
		)
		kwargs = dict(req.agent_kwargs)
		if self.run_dir:
			kwargs.setdefault('generate_gif', str(self.run_dir / 'recording.gif'))
		try:
			self.result = await run_task(req.task, req.settings, identity_name=req.identity, max_steps=req.max_steps, hooks=hooks, agent_kwargs=kwargs)
			r = self.result
			self._event(
				'result', ok=r.ok, stopped=self._stop_requested, output=None if self._stop_requested else r.output,
				attempts=[a.__dict__ for a in r.attempts],
			)
			if req.on_done:
				try:
					req.on_done(r)
				except Exception:
					log.exception('on_done failed')
			if self.run_dir:
				(self.run_dir / 'result.json').write_text(
					json.dumps({'task': req.task, 'ok': r.ok, 'output': r.output, 'attempts': [a.__dict__ for a in r.attempts]}, indent=2)
				)
		except asyncio.CancelledError:
			self._event('result', ok=False, stopped=True, output=None, attempts=[])
		except Exception as e:
			log.exception('run failed')
			self._event('notice', level='error', text=f'Run failed: {type(e).__name__}: {e}')
		finally:
			self._agent, self._session, self.human = None, None, None
			self.status = 'idle'
			self.version += 1

	@property
	def gif_path(self) -> str | None:
		p = self.run_dir / 'recording.gif' if self.run_dir else None
		return str(p) if p and p.exists() else None

	@property
	def result_path(self) -> str | None:
		p = self.run_dir / 'result.json' if self.run_dir else None
		return str(p) if p and p.exists() else None
