"""Agent runs driven from the web UI: state, an event log, pause/stop, steering and human takeover.

An AgentRunner is one session (one run at a time, in one browser). SessionHub holds them: the main chat's
session, plus one per job when a request is split to run side by side (each in its own fresh browser, with a
cap on how many browsers run at once; the rest wait as 'queued').

Kept free of any UI framework so it can be tested: the chat app reads `snapshot()` (structured `events`).
Nothing here pushes to a browser.

Human takeover: the person can take the browser over at any time (`take_over`), and the agent asks for it
itself on a CAPTCHA or an unsolved challenge (RunHooks.request_human). Either way the agent is paused with
status 'needs_human' until `hand_back`, which tells the agent what happened and resumes it.

Steering: `steer(text)` queues an instruction that the agent reads at the start of its next step.

History: every run keeps `run.json` (its transcript) next to its screenshots and recording.gif in
`<runs_dir>/<run id>/`, rewritten as it goes, so the app can list and reopen past runs.

Review (opt-in per run): after the agent finishes, an LLM judges the run (veil.judge); the review is saved as
judgement.json / judgement.md and handed to the next run in the same conversation.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import secrets
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from browser_use import ActionResult

from veil import judge
from veil.agent import Attempt, RunHooks, TaskResult, run_task
from veil.config import Settings
from veil.llm import build_llm
from veil.proxy import NoProxy, ProxySource
from veil.session import make_proxy_source

log = logging.getLogger('veil.webui')

RUN_ID = re.compile(r'^[A-Za-z0-9_.-]{1,80}$')


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
	review: bool = False  # judge the run afterwards and hand the review to the next run
	gate: asyncio.Semaphore | None = None  # wait for a browser slot first (runs split from one request)
	proxy_source: ProxySource | None = None  # shared by runs at the same time, so each takes its own proxy
	group: str | None = None  # the split request this run belongs to
	country: str | None = None  # ISO code of where the person is: their exit IP is picked there


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


def steer_message(text: str) -> str:
	return (
		f'The person watching this run sent you an instruction: "{text}". Follow it from now on; where it '
		'conflicts with your current plan, it wins.'
	)


# --- run history (the folders under runs_dir) ---------------------------------------------------


def file_url(run_dir: Path, name: str) -> str:
	return f'/api/runs/{run_dir.name}/{name}'


def _write_json(path: Path, data: Any) -> None:
	tmp = path.with_suffix('.tmp')
	tmp.write_text(json.dumps(data, indent=1, default=str), encoding='utf-8')
	tmp.replace(path)


def _read_json(path: Path) -> Any:
	try:
		return json.loads(path.read_text(encoding='utf-8'))
	except (OSError, ValueError):
		return None


def _started_from_name(name: str) -> float | None:
	try:
		return time.mktime(time.strptime(name[:15], '%Y%m%d-%H%M%S'))
	except ValueError:
		return None


def load_run(runs_dir: Path, run_id: str) -> dict[str, Any] | None:
	"""A past run: run.json, or what an older run left behind (result.json, recording.gif)."""
	if not RUN_ID.match(run_id):
		return None
	d = runs_dir / run_id
	if not d.is_dir():
		return None
	run = _read_json(d / 'run.json')
	if not isinstance(run, dict):
		old = _read_json(d / 'result.json') or {}
		run = {
			'id': run_id, 'title': old.get('task') or '(older run: no transcript)', 'task': old.get('task', ''), 'kind': 'task',
			'started': _started_from_name(run_id), 'ended': None, 'state': 'done' if old else 'unknown', 'ok': old.get('ok'),
			'events': [], 'identity': None,
		}
		if old:
			run['events'] = [
				{'id': 0, 'type': 'task', 'text': run['title']},
				{'id': 1, 'type': 'result', 'ok': old.get('ok'), 'stopped': False, 'output': old.get('output'), 'attempts': old.get('attempts') or []},
			]
	run['id'] = run_id
	run['gif'] = file_url(d, 'recording.gif') if (d / 'recording.gif').exists() else None
	run['result_file'] = file_url(d, 'result.json') if (d / 'result.json').exists() else None
	run['judgement'] = run.get('judgement') or _read_json(d / 'judgement.json')
	run['judgement_file'] = file_url(d, 'judgement.md') if (d / 'judgement.md').exists() else None
	return run


def delete_run(runs_dir: Path, run_id: str) -> bool:
	"""Remove a past run's folder (transcript, screenshots, recording)."""
	if not RUN_ID.match(run_id) or not (runs_dir / run_id).is_dir():
		return False
	shutil.rmtree(runs_dir / run_id, ignore_errors=True)
	return not (runs_dir / run_id).exists()


def rename_run(runs_dir: Path, run_id: str, title: str) -> bool:
	"""Give a past run a new title in the history (its transcript keeps what was asked)."""
	if not RUN_ID.match(run_id):
		return False
	path = runs_dir / run_id / 'run.json'
	run = _read_json(path)
	if not isinstance(run, dict):
		run = load_run(runs_dir, run_id)
		if not run:
			return False
		run = {k: v for k, v in run.items() if k not in ('gif', 'result_file', 'judgement_file')}
	run['title'] = title
	_write_json(path, run)
	return True


def list_runs(runs_dir: Path, limit: int = 300) -> list[dict[str, Any]]:
	"""Newest first, without the transcripts."""
	if not runs_dir.is_dir():
		return []
	out = []
	for d in sorted((p for p in runs_dir.iterdir() if p.is_dir() and RUN_ID.match(p.name)), key=lambda p: p.name, reverse=True)[:limit]:
		run = load_run(runs_dir, d.name)
		if run:
			j = run.get('judgement') or {}
			out.append({
				'id': run['id'], 'title': run.get('title', ''), 'kind': run.get('kind', 'task'), 'started': run.get('started'),
				'state': run.get('state'), 'ok': run.get('ok'), 'has_gif': bool(run.get('gif')), 'verdict': j.get('verdict'),
				'gif': run.get('gif'), 'group': run.get('group'),
			})
	return out


def save_judgement(run_dir: Path, j: judge.Judgement, title: str) -> dict[str, Any]:
	data = j.model_dump()
	_write_json(run_dir / 'judgement.json', data)
	(run_dir / 'judgement.md').write_text(judge.to_markdown(j, title), encoding='utf-8')
	run = _read_json(run_dir / 'run.json')
	if isinstance(run, dict):
		run['judgement'] = data
		_write_json(run_dir / 'run.json', run)
	return data


async def review_saved_run(runs_dir: Path, run_id: str, settings: Settings) -> dict[str, Any]:
	"""Judge a past run on request ("Review this run")."""
	run = load_run(runs_dir, run_id)
	if not run or not run.get('events'):
		raise ValueError('that run has no transcript to review')
	j = await judge.judge_run(build_llm(settings), run['events'])
	return save_judgement(runs_dir / run_id, j, run.get('title', ''))


class AgentRunner:
	"""Holds the current run. Methods are called from the server's handlers on the same event loop."""

	def __init__(self, runs_dir: Path, id: str = 'main', group: str | None = None, label: str = ''):
		self.runs_dir = runs_dir
		self.id, self.group, self.label = id, group, label  # label: the session's tab in the app
		self.events: list[dict[str, Any]] = []
		self.identity: dict[str, Any] | None = None
		self.human: dict[str, Any] | None = None  # what the person is asked to do while status is 'needs_human'
		self.status = 'idle'  # idle | queued | starting | running | paused | needs_human | stopping | reviewing
		self.version = 0  # bumps whenever events/identity change, so the UI only re-sends on change
		self.run_dir: Path | None = None
		self.result: TaskResult | None = None
		self.last_review: dict[str, Any] | None = None  # {task, judgement} of the previous reviewed run in this conversation
		self._task: asyncio.Task | None = None
		self._agent = None
		self._session = None
		self._stop_requested = False
		self._req: RunRequest | None = None
		self._run_start = 0  # index in self.events where the current run begins
		self._run_state = 'running'
		self._steers: list[dict[str, Any]] = []  # steer events not yet handed to the agent

	# --- state -------------------------------------------------------------------------------

	@property
	def busy(self) -> bool:
		return self._task is not None and not self._task.done()

	def _event(self, event_type: str, /, **data: Any) -> dict[str, Any]:
		ev = {**data, 'id': len(self.events), 'type': event_type, 'time': time.time()}
		self.events.append(ev)
		self.version += 1
		self._save_run()
		return ev

	def _file_url(self, path: Path) -> str | None:
		return file_url(path.parent, path.name) if self.run_dir and path.parent == self.run_dir else None

	def _save_run(self) -> None:
		"""Write the current run's transcript, so history survives a crash or a restart."""
		if not self.run_dir or not self._req:
			return
		ok = self.result.ok if self.result else None
		data = {
			'id': self.run_dir.name, 'title': self._req.title or self._req.task, 'task': self._req.task, 'kind': self._req.kind, 'group': self._req.group,
			'started': self.events[self._run_start]['time'] if len(self.events) > self._run_start else time.time(),
			'ended': None if self._run_state == 'running' else time.time(), 'state': self._run_state, 'ok': ok,
			'events': self.events[self._run_start:], 'identity': self.identity,
		}
		try:
			_write_json(self.run_dir / 'run.json', data)
		except OSError as e:
			log.debug('could not save run.json: %s', e)

	def snapshot(self) -> dict[str, Any]:
		"""Everything the chat app shows, as JSON-ready data."""
		gif, res = self.gif_path, self.result_path
		return {
			'session': self.id,
			'group': self.group,
			'label': self.label,
			'version': self.version,
			'status': self.status,
			'busy': self.busy,
			'events': self.events,
			'identity': self.identity,
			'human': self.human,
			'run_id': self.run_dir.name if self.run_dir else None,
			'browser_open': self._session is not None and getattr(self._session, 'browser', None) is not None,
			'gif': self._file_url(Path(gif)) if gif and self.status == 'idle' else None,
			'result_file': self._file_url(Path(res)) if res else None,
			'has_review': self.last_review is not None,
		}

	# --- control -----------------------------------------------------------------------------

	def start(self, req: RunRequest) -> None:
		if self.busy:
			raise RuntimeError('a task is already running')
		self.run_dir = self.runs_dir / f'{time.strftime("%Y%m%d-%H%M%S")}-{secrets.token_hex(3)}'
		self.run_dir.mkdir(parents=True, exist_ok=True)
		self.result, self._agent, self._session, self._stop_requested = None, None, None, False
		self.human, self.identity, self._steers = None, None, []
		self._req, self._run_start, self._run_state = req, len(self.events), 'running'
		self.status = 'queued' if req.gate else 'starting'  # until it has a browser slot
		self._event('task', text=req.title or req.task, kind=req.kind, run=self.run_dir.name)
		if req.review and self.last_review:
			# The previous run in this conversation was reviewed: the agent reads that review in its system prompt.
			note = judge.next_run_message(self.last_review['judgement'], self.last_review['task'])
			prev = req.agent_kwargs.get('extend_system_message')
			req.agent_kwargs['extend_system_message'] = f'{prev}\n\n{note}' if prev else note
			self._event('notice', level='info', text='Using the review of the previous run.')
		self._task = asyncio.create_task(self._run(req))

	def steer(self, text: str) -> bool:
		"""An instruction for the agent mid-run; it reads it at the start of its next step."""
		text = (text or '').strip()[:2000]
		if not text or not self.busy or self.status in ('stopping', 'reviewing'):
			return False
		self._steers.append(self._event('steer', text=text, delivered=False))
		return True

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
		msg += (
			' Continue on the tab that is open now: they may have entered a code or moved the flow forward, so do not switch back to'
			' earlier tabs. If the page looks blank or is still loading, wait 2-3 seconds and look again before deciding anything.'
		)
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
		self._save_run()

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
			self._task.cancel()  # launching the identity or reviewing: nothing to finish

	async def clear(self) -> None:
		if self.busy:
			self.stop()
			try:
				await asyncio.wait_for(asyncio.shield(self._task), timeout=15)
			except (asyncio.TimeoutError, asyncio.CancelledError):
				self._task.cancel()
				# Let the run finish unwinding first, or its last message lands in the cleared chat.
				await asyncio.wait({self._task}, timeout=15)
		self.events, self.identity, self.human, self.result, self.last_review = [], None, None, None, None
		self.run_dir, self._req, self._run_start = None, None, 0
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

	async def _on_step_start(self, agent) -> None:
		"""Hand queued steers to the agent: they land in the prompt this step is about to build."""
		if not self._steers:
			return
		steers, self._steers = self._steers, []
		state = agent.state
		state.last_result = [*(state.last_result or []), *(ActionResult(long_term_memory=steer_message(s['text'])) for s in steers)]
		for s in steers:
			s['delivered'] = True
		self.version += 1
		self._save_run()

	def _take_followups(self) -> list[str]:
		"""Steers that arrived as the agent was finishing: the agent carries on with them as a follow-up."""
		if not self._steers or self._stop_requested:
			return []
		steers, self._steers = self._steers, []
		for s in steers:
			s.update(delivered=True, followup=True)
		self.version += 1
		self._save_run()
		return [s['text'] for s in steers]

	async def _on_attempt(self, n: int, total: int, session, agent) -> None:
		self._session, self._agent = session, agent
		self.identity = identity_info(n, total, session)
		self._event('attempt', identity=self.identity)
		want = self._req.country if self._req else None
		got = session.exit.country if getattr(session, 'exit', None) else None
		if want and got and got.upper() != want.upper():
			self._event(
				'notice', level='info',
				text=f'This person is in {want.upper()} but the exit IP is in {got.upper()}: no proxy for {want.upper()} is set up '
				'(test your proxies so veil knows their countries, or use a gateway with {country}).',
			)
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

	async def _review(self, req: RunRequest) -> None:
		"""Judge the run that just ended; show it, save it, and keep it for the next run."""
		self.status = 'reviewing'
		self.version += 1
		run_events = self.events[self._run_start:]
		try:
			j = await judge.judge_run(build_llm(req.settings), run_events)
		except asyncio.CancelledError:
			self._event('notice', level='info', text='Review skipped.')
			return
		except Exception as e:
			log.warning('run review failed: %s', e)
			self._event('notice', level='error', text=f'Could not review the run: {type(e).__name__}: {e}')
			return
		data = save_judgement(self.run_dir, j, req.title or req.task) if self.run_dir else j.model_dump()
		self.last_review = {'task': req.title or req.task, 'judgement': data}
		self._event('judgement', judgement=data, file=self._file_url(self.run_dir / 'judgement.md') if self.run_dir else None)

	async def _run(self, req: RunRequest) -> None:
		hooks = RunHooks(
			on_attempt=self._on_attempt, on_step=self._on_step, on_attempt_end=self._on_attempt_end,
			stop_requested=lambda: self._stop_requested, request_human=self._on_request_human, on_step_start=self._on_step_start,
			take_followups=self._take_followups,
		)
		kwargs = dict(req.agent_kwargs)
		if self.run_dir:
			kwargs.setdefault('generate_gif', str(self.run_dir / 'recording.gif'))
		extra: dict[str, Any] = {'proxy_source': req.proxy_source} if req.proxy_source is not None else {}
		if req.country:
			extra['country'] = req.country
		try:
			if req.gate:
				async with req.gate:  # a browser slot: Stop while queued cancels here
					self.status = 'starting' if self.status == 'queued' else self.status
					self.version += 1
					self.result = await run_task(req.task, req.settings, identity_name=req.identity, max_steps=req.max_steps, hooks=hooks, agent_kwargs=kwargs, **extra)
			else:
				self.result = await run_task(req.task, req.settings, identity_name=req.identity, max_steps=req.max_steps, hooks=hooks, agent_kwargs=kwargs, **extra)
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
			self._run_state = 'stopped' if self._stop_requested else 'done'
			if req.review and not self._stop_requested:
				await self._review(req)
		except asyncio.CancelledError:
			self._run_state = 'stopped'
			self._event('result', ok=False, stopped=True, output=None, attempts=[])
		except Exception as e:
			log.exception('run failed')
			self._run_state = 'failed'
			self._event('notice', level='error', text=f'Run failed: {type(e).__name__}: {e}')
		finally:
			for s in self._steers:  # never reached the agent
				s['undelivered'] = True
			self._agent, self._session, self.human, self._steers = None, None, None, []
			if self._run_state == 'running':
				self._run_state = 'done'
			self._save_run()
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


class SessionHub:
	"""Every session the app has: 'main' (the chat) plus the ones a split request started.

	Sessions of a split request share one browser gate (at most `max_parallel` browsers at once) and one
	proxy source, so each takes a different proxy from a list (a gateway gives each its own sticky exit IP).
	"""

	def __init__(self, runs_dir: Path, main: AgentRunner | None = None, max_parallel: int = 3):
		self.runs_dir = runs_dir
		self.main = main or AgentRunner(runs_dir)
		self.main.id = 'main'
		self.sessions: dict[str, AgentRunner] = {'main': self.main}
		self.max_parallel = max(1, max_parallel)
		self.version = 0
		self._gate: asyncio.Semaphore | None = None
		self._sources: dict[tuple, ProxySource] = {}

	def get(self, sid: str | None) -> AgentRunner | None:
		return self.sessions.get(sid or 'main')

	@property
	def gate(self) -> asyncio.Semaphore:
		if self._gate is None:
			self._gate = asyncio.Semaphore(self.max_parallel)
		return self._gate

	def add(self, group: str, label: str) -> AgentRunner:
		n = sum(1 for r in self.sessions.values() if r.group == group) + 1
		runner = AgentRunner(self.runs_dir, id=f'{group}-{n}', group=group, label=label)
		self.sessions[runner.id] = runner
		self.version += 1
		return runner

	def close(self, sid: str) -> bool:
		"""Drop a finished split-off session from the app (its run stays in the history)."""
		r = self.sessions.get(sid)
		if not r or r is self.main or r.busy:
			return False
		del self.sessions[sid]
		self.version += 1
		return True

	def close_idle(self) -> None:
		for sid in [sid for sid, r in self.sessions.items() if r is not self.main and not r.busy]:
			self.close(sid)

	def find_run(self, run_id: str) -> AgentRunner | None:
		return next((r for r in self.sessions.values() if r.run_dir and r.run_dir.name == run_id), None)

	def key(self) -> tuple:
		"""Changes whenever any session's state does (for the event stream)."""
		return self.version, tuple((r.id, r.version, r.status) for r in self.sessions.values())

	def summaries(self) -> list[dict[str, Any]]:
		out = []
		for r in self.sessions.values():
			task = next((e for e in reversed(r.events) if e['type'] == 'task'), None)
			out.append({
				'id': r.id, 'group': r.group, 'label': r.label or (task['text'].split('\n')[0][:80] if task else 'New chat'),
				'status': r.status, 'busy': r.busy, 'run_id': r.run_dir.name if r.run_dir else None,
				'ok': r.result.ok if r.result else None, 'needs_human': r.status == 'needs_human',
			})
		return out

	def proxy_source(self, settings: Settings) -> ProxySource | None:
		"""One source per proxy configuration, shared by every run; None when there's no proxy (direct)."""
		if settings.proxy_gateway:
			key: tuple = ('gateway', settings.proxy_gateway)
		elif settings.proxy_file:
			try:
				mtime = settings.proxy_file.stat().st_mtime
			except OSError:
				return None  # the run reports the missing file itself
			try:
				tested = (settings.data_dir / 'proxy-status.json').stat().st_mtime  # a test teaches proxies' countries
			except OSError:
				tested = 0
			key = ('list', str(settings.proxy_file), mtime, settings.proxy_strategy, tested)
		else:
			return None
		if key not in self._sources:
			try:
				source = make_proxy_source(settings)
			except (OSError, ValueError):
				return None  # e.g. an empty list: the run reports it
			if isinstance(source, NoProxy):
				return None
			self._sources = {key: source}  # an edited list replaces the old pool
		return self._sources[key]
