"""One agent run at a time, driven from the web UI: state, a chat transcript, and pause/stop.

Kept free of Gradio so it can be tested (and reused by another front end). The UI polls
`snapshot()`; nothing here pushes to the browser.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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


def identity_markdown(n: int, total: int, session) -> str:
	"""The identity panel: what the site sees this attempt."""
	p, ex, s = session.persona, session.exit, session.settings
	lines = [f'**Attempt {n}/{total}** · engine `{s.engine}`' + (' · headless' if s.headless else ' · window shown')]
	lines.append(f'- **Proxy:** {session.proxy.redacted if session.proxy else "none (direct)"}')
	if ex:
		lines.append(f'- **Exit IP:** {ex.ip} · {ex.country} {ex.city or ""} · {ex.timezone}')
	if p:
		lines += [
			f'- **Device:** {p.windows} {p.machine_class} · {p.webgpu_description} · {p.hw_concurrency} cores / {p.device_memory} GB · {p.screen_width}x{p.screen_height}',
			f'- **Locale:** {p.timezone} · {p.languages}',
			f'- **Seeds:** canvas {p.canvas_seed} · audio {p.audio_seed}',
		]
	if session.identity:
		lines.append(f'- **Sticky identity:** `{session.identity.name}` (same device, IP and cookies next time)')
	return '\n'.join(lines)


def step_markdown(summary, output, n: int) -> str:
	parts = [f'**Step {n}** · {getattr(summary, "title", "") or ""}  \n`{getattr(summary, "url", "")}`']
	for label, attr in (('Eval', 'evaluation_previous_goal'), ('Memory', 'memory'), ('Next', 'next_goal')):
		v = getattr(output, attr, None)
		if v:
			parts.append(f'**{label}:** {v}')
	actions = []
	for a in getattr(output, 'action', None) or []:
		d = a.model_dump(exclude_none=True)
		actions += [f'`{k}` {json.dumps(v, ensure_ascii=False)[:300]}' for k, v in d.items()]
	if actions:
		parts.append('**Actions:**\n' + '\n'.join(f'- {x}' for x in actions))
	return '\n\n'.join(parts)


def attempts_markdown(result: TaskResult) -> str:
	lines = ['**Attempts:**']
	for i, a in enumerate(result.attempts, 1):
		line = f'{i}. **{a.outcome}** · {a.proxy or "direct"} → {a.exit_ip or "?"} · {a.persona or "no device"}'
		if a.detail:
			line += f'  \n   `{a.detail[:300]}`'
		lines.append(line)
	return '\n'.join(lines)


class AgentRunner:
	"""Holds the current run. Methods are called from Gradio handlers on the same event loop."""

	def __init__(self, runs_dir: Path):
		self.runs_dir = runs_dir
		self.messages: list[dict[str, Any]] = []
		self.identity_md = '_No run yet._'
		self.status = 'idle'  # idle | starting | running | paused | stopping
		self.version = 0  # bumps whenever messages/identity change, so the UI only re-sends on change
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

	def _post(self, role: str, content: Any) -> None:
		self.messages.append({'role': role, 'content': content})
		self.version += 1

	# --- control -----------------------------------------------------------------------------

	def start(self, req: RunRequest) -> None:
		if self.busy:
			raise RuntimeError('a task is already running')
		self.run_dir = self.runs_dir / f'{time.strftime("%Y%m%d-%H%M%S")}-{secrets.token_hex(3)}'
		self.run_dir.mkdir(parents=True, exist_ok=True)
		self.result, self._agent, self._session, self._stop_requested = None, None, None, False
		self.status = 'starting'
		self._post('user', req.task)
		self._task = asyncio.create_task(self._run(req))

	def pause_resume(self) -> None:
		if not self._agent or self.status not in ('running', 'paused'):
			return
		if self.status == 'paused':
			self._agent.resume()
			self.status = 'running'
		else:
			self._agent.pause()
			self.status = 'paused'

	def stop(self) -> None:
		if not self.busy:
			return
		self._stop_requested = True
		self.status = 'stopping'
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
		self.messages, self.identity_md, self.result = [], '_No run yet._', None
		self.status = 'idle'
		self.version += 1

	async def screenshot(self) -> bytes | None:
		"""Current viewport as JPEG, for the live view. None when no browser is up."""
		s = self._session
		if not s or not s.browser or self.status not in ('running', 'paused', 'stopping'):
			return None
		try:
			return await asyncio.wait_for(s.browser.take_screenshot(format='jpeg', quality=60), timeout=5)
		except Exception as e:
			log.debug('live screenshot failed: %s', e)
			return None

	# --- the run -----------------------------------------------------------------------------

	async def _on_attempt(self, n: int, total: int, session, agent) -> None:
		self._session, self._agent = session, agent
		self.identity_md = identity_markdown(n, total, session)
		if self._stop_requested:  # Stop was pressed while the identity was launching
			agent.stop()
		elif self.status != 'stopping':
			self.status = 'running'
		self._post('assistant', f'🕵️ New identity (attempt {n}/{total})\n\n' + self.identity_md)

	async def _on_step(self, summary, output, n: int) -> None:
		content: list[Any] = [step_markdown(summary, output, n)]
		shot = getattr(summary, 'screenshot', None)
		if shot and self.run_dir:
			path = self.run_dir / f'step-{n:03d}.png'
			try:
				path.write_bytes(base64.b64decode(shot))
				content.append({'path': str(path)})
			except Exception as e:
				log.debug('could not save step screenshot: %s', e)
		self._post('assistant', content if len(content) > 1 else content[0])

	async def _on_attempt_end(self, n: int, attempt: Attempt) -> None:
		self._agent, self._session = None, None
		if attempt.outcome == 'blocked':
			self._post('assistant', f'🚧 Blocked on attempt {n}: {attempt.detail}. Rotating to a fresh device and exit IP.')
		elif attempt.outcome == 'error':
			self._post('assistant', f'💥 Attempt {n} crashed: `{attempt.detail}`')

	async def _run(self, req: RunRequest) -> None:
		hooks = RunHooks(
			on_attempt=self._on_attempt, on_step=self._on_step, on_attempt_end=self._on_attempt_end, stop_requested=lambda: self._stop_requested
		)
		kwargs = dict(req.agent_kwargs)
		if self.run_dir:
			kwargs.setdefault('generate_gif', str(self.run_dir / 'recording.gif'))
		try:
			self.result = await run_task(req.task, req.settings, identity_name=req.identity, max_steps=req.max_steps, hooks=hooks, agent_kwargs=kwargs)
			r = self.result
			if self._stop_requested:
				head = '⏹️ **Stopped** before the agent finished.'
			else:
				head = f'{"✅ **Done.**" if r.ok else "❌ **Did not finish.**"}\n\n**Result:** {r.output or "(no output)"}'
			self._post('assistant', f'{head}\n\n{attempts_markdown(r)}')
			if self.run_dir:
				(self.run_dir / 'result.json').write_text(
					json.dumps({'task': req.task, 'ok': r.ok, 'output': r.output, 'attempts': [a.__dict__ for a in r.attempts]}, indent=2)
				)
		except asyncio.CancelledError:
			self._post('assistant', '⏹️ **Stopped** before the agent started.')
		except Exception as e:
			log.exception('run failed')
			self._post('assistant', f'💥 **Run failed:** `{type(e).__name__}: {e}`')
		finally:
			self._agent, self._session = None, None
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
