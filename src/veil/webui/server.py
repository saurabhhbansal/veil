"""`veil ui`: the chat app (web/, adapted from browser-use/chat-ui-example) and the local API it talks to.

Everything runs on this PC: the agent, Fortress, and this server. No hosted browser service is involved.

Sessions: 'main' is the chat; a request split into jobs ("create 2 accounts, one per profile") runs each job
in its own session and browser side by side. Session endpoints take `session` (JSON body, or ?session= for
GET/SSE/WebSocket) and default to 'main'.

    GET  /api/config            defaults from .env, for the settings bar (no secrets)
    GET  /api/state             a session's run (AgentRunner.snapshot) plus every session's summary
    GET  /api/events            the same, pushed as server-sent events whenever anything changes
    POST /api/run               {task, values, session?} start a task (values use the setting ids of manager.settings_from_ui).
                                Account requests and split requests are planned first (veil.planner).
    POST /api/stop | /api/pause | /api/clear
    POST /api/takeover          the person takes the browser (pauses the agent)
    POST /api/handback          {note} the person is done; the agent carries on
    POST /api/steer             {text} an instruction the agent reads at its next step
    POST /api/sessions/close    {session} drop a finished side session from the app (its run stays in history)
    GET  /api/runs              past runs, newest first; GET /api/runs/<id> one run's transcript, recording, review
    POST /api/runs/<id>/review  judge a past run now (the "Review runs" toggle does it after each run)
    POST /api/runs/<id>/rename  {title}
    POST /api/runs/<id>/delete  remove the run's folder (not while it's running)
    WS   /api/live              live view: JPEG frames out, mouse/keyboard in (see live.py)
    GET  /api/runs/<run>/<file> step screenshots, recording.gif, result.json
    GET  /api/accounts          your sign-up profiles and the sign-up log (passwords left out)
    POST /api/accounts/profiles {profiles} replace profiles.csv; a profile sent without "password" keeps its saved one
                                (with VEIL_IDENTITY_API set, people come from the company identity server instead: identities.py)
    POST /api/accounts/signup   {url, profile_id?, values} sign up on a site as one of your profiles (a normal run)
    POST /api/accounts/login    {url, profile_id?, values} log in to an account you have there
    POST /api/accounts/bulk     {url, count, prompt?, values} create `count` NEW accounts there, one run per account:
                                each gets a different person, its own identity, and a one-account instruction (the
                                agent never learns the count). Platforms come from GET /api/config.
    POST /api/accounts/signups/delete {site, profile_id} forget one sign-up row (the site account is not touched)
    POST /api/accounts/reveal   {site, profile_id} the password veil used on that site
    POST /api/identities/delete {name} remove one saved browser identity (device, exit IP, cookies)
    ... and /api/keys, /api/llm/default, /api/proxies* (admin.py)

The UI has no login, so requests are only accepted from this PC's own origin (or the dev server's): another
website open in the same browser can't drive the agent or the live view.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import secrets
import shutil
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from veil import accounts, identities, planner
from veil.config import Settings
from veil.geo import country_code
from veil.llm import EFFORTS, KEY_ENV, MODELS, build_llm
from veil.planner import Job
from veil.webui import admin
from veil.webui.live import LiveView
from veil.webui.manager import PROVIDERS, saved_identities, settings_from_ui
from veil.webui.runner import AgentRunner, RunRequest, SessionHub, delete_run, list_runs, load_run, rename_run, review_saved_run

log = logging.getLogger('veil.webui.server')

WEB_DIST = Path(__file__).resolve().parents[3] / 'web' / 'out'  # `npm run build` in web/
DEV_ORIGINS = {'http://localhost:3000', 'http://127.0.0.1:3000'}  # `npm run dev` in web/
# Which UI values a request may set: the ids settings_from_ui reads. Secrets (API key) are allowed in, never out.
UI_VALUE_IDS = {
	'agent_settings.llm_provider', 'agent_settings.llm_model', 'agent_settings.llm_api_key', 'agent_settings.llm_temperature', 'agent_settings.llm_effort',
	'agent_settings.max_steps', 'agent_settings.use_vision', 'agent_settings.max_actions', 'agent_settings.extend_system_prompt',
	'stealth_settings.headful', 'stealth_settings.identity', 'stealth_settings.max_attempts',
	'stealth_settings.geo_match',
}
LOOPBACK = {'127.0.0.1', 'localhost', '::1'}
_RUN_FILE = re.compile(r'^[A-Za-z0-9_.-]{1,80}$')

NOT_BUILT = """<!doctype html><meta charset="utf-8"><title>veil</title>
<body style="background:#09090b;color:#e4e4e7;font:15px system-ui;display:grid;place-items:center;height:100vh;margin:0">
<div style="max-width:560px"><h2>The veil chat app isn't built yet</h2>
<p>Build it once (needs Node.js 20+), then reload this page:</p>
<pre style="background:#18181b;padding:12px;border-radius:8px">cd web
npm install
npm run build</pre></div>"""


def allowed_origins(host: str, port: int, dev: bool) -> set[str]:
	origins = {f'http://{h}:{port}' for h in {host, '127.0.0.1', 'localhost'}}
	return origins | DEV_ORIGINS if dev else origins


def host_ok(host_header: str | None, allowed: set[str]) -> bool:
	"""The Host header must name this server, so a DNS-rebinding page can't reach it under its own name."""
	return bool(host_header) and f'http://{host_header}' in allowed


def origin_ok(origin: str | None, allowed: set[str]) -> bool:
	"""Browsers always send Origin on cross-site POSTs and WebSockets. None = curl or the same page's GET."""
	return origin is None or origin in allowed


def create_app(env: Settings, *, host: str = '127.0.0.1', port: int = 7788, dev: bool = False, runner: AgentRunner | None = None) -> FastAPI:
	hub = SessionHub(env.data_dir / 'runs', main=runner, max_parallel=env.max_parallel)
	runner = hub.main
	runner.runs_dir.mkdir(parents=True, exist_ok=True)
	profiles_file, signups_file = accounts.profiles_path(env), env.accounts_dir / 'signups.csv'
	allowed = allowed_origins(host, port, dev)
	hosts = allowed_origins(host, port, dev=False)
	app = FastAPI(title='veil', docs_url=None, redoc_url=None, openapi_url=None)
	app.state.runner, app.state.hub = runner, hub

	@app.middleware('http')
	async def guard(request: Request, call_next):
		origin = request.headers.get('origin')
		if request.url.path.startswith('/api/') and not (origin_ok(origin, allowed) and (host not in LOOPBACK or host_ok(request.headers.get('host'), hosts))):
			return JSONResponse({'error': 'cross-origin request refused'}, status_code=403)
		response = await call_next(request)
		if origin and origin in DEV_ORIGINS and dev:
			response.headers['Access-Control-Allow-Origin'] = origin
			response.headers['Access-Control-Allow-Headers'] = 'content-type'
			response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
		return response

	@app.options('/api/{path:path}')
	async def preflight(path: str):
		return JSONResponse({})

	@app.get('/api/config')
	async def config():
		keys = {p: bool(env.llm_api_key if p == env.llm_provider and env.llm_api_key else os.environ.get(var)) for p, var in KEY_ENV.items()}
		try:
			known = sorted({r.site for r in accounts.SignupLog(signups_file).rows.values()})
		except (OSError, ValueError):
			known = []
		return {
			'llm_provider': env.llm_provider, 'llm_model': env.llm_model, 'llm_effort': env.llm_effort,
			'has_api_key': keys.get(env.llm_provider, False), 'keys': keys, 'providers': PROVIDERS, 'efforts': EFFORTS, 'models': MODELS,
			'headless': env.headless,
			'geo_match': env.geo_match, 'max_attempts': env.max_attempts, 'max_steps': env.max_steps, 'max_parallel': hub.max_parallel,
			'identities': saved_identities(env), 'platforms': accounts.platforms(known), 'max_bulk': env.max_bulk,
			'captcha': {'provider': env.captcha_provider, 'has_key': bool(env.captcha_api_key), 'timeout': env.captcha_timeout},
			'mailbox_wait': env.mailbox_wait,
			'proxy': 'gateway' if env.proxy_gateway else 'list' if env.proxy_file else None,
			'proxy_detail': str(env.proxy_file) if env.proxy_file and not env.proxy_gateway else None,
		}

	# --- saved browser identities ---------------------------------------------------------------

	@app.post('/api/identities/delete')
	async def delete_identity(request: Request):
		"""Remove one saved identity: its sticky device, exit IP and cookies. The site accounts it made stay."""
		name = str((await body_of(request)).get('name') or '')
		if not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', name) or name in ('.', '..'):
			raise HTTPException(400, 'bad identity name')
		path = env.data_dir / 'identities' / name
		if not (path / 'identity.json').is_file():
			raise HTTPException(404, 'no such saved identity')
		if any(r.busy and (r.identity or {}).get('sticky') == name for r in hub.sessions.values()):
			raise HTTPException(409, 'a run is using that identity right now: stop it first')
		await asyncio.to_thread(shutil.rmtree, path)
		return {'ok': True, 'identities': saved_identities(env)}

	# --- sessions --------------------------------------------------------------------------------

	def session_of(sid: Any) -> AgentRunner:
		r = hub.get(str(sid) if sid else None)
		if not r:
			raise HTTPException(404, 'that session was closed')
		return r

	def snap(r: AgentRunner, **extra: Any) -> dict[str, Any]:
		return {**r.snapshot(), 'sessions': hub.summaries(), **extra}

	async def body_of(request: Request) -> dict[str, Any]:
		if not request.headers.get('content-type', '').startswith('application/json'):
			return {}
		try:
			body = await request.json()
		except ValueError:
			return {}
		return body if isinstance(body, dict) else {}

	@app.get('/api/state')
	async def state(session: str | None = None):
		return snap(hub.get(session) or runner)

	@app.get('/api/events')
	async def events(request: Request, session: str | None = None):
		async def stream():
			last = None
			idle = 0
			while not await request.is_disconnected():
				r = hub.get(session) or runner  # a closed session falls back to the chat
				key = (hub.key(), r.id)
				if key != last:
					last, idle = key, 0
					yield f'data: {json.dumps(snap(r), default=str)}\n\n'
				else:
					idle += 1
					if idle % 50 == 0:
						yield ': keep-alive\n\n'
				await asyncio.sleep(0.3)

		return StreamingResponse(stream(), media_type='text/event-stream', headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

	def start_on(target: AgentRunner | None, req: RunRequest, label: str) -> AgentRunner:
		"""Run in `target` when it's free; otherwise in a new side session, so it runs alongside."""
		if target is None or target.busy:
			group = f'g{secrets.token_hex(3)}'
			target = hub.add(group, label)
			req.gate, req.group = hub.gate, group
		target.start(req)
		return target

	# --- account jobs: sign up / log in with your own details ----------------------------------

	def read_profiles() -> tuple[list[dict[str, str]], str | None]:
		"""profiles.csv, or with an identity server, whoever it lists (blocking: call it off the event loop)."""
		if env.identity_api:
			return identities.profile_source(env).listing(), None
		try:
			return accounts.load_profiles(profiles_file, allow_empty=True), None
		except ValueError as e:
			return [], str(e)

	async def account_request(
		job: Job, values: dict[str, Any], review: bool, *, request_text: str | None = None, panel: bool = False, exclude: frozenset[str] = frozenset()
	) -> tuple[RunRequest, str, str]:
		"""A sign-up / log-in job -> (RunRequest, site, profile id). The details go in as secrets only."""
		values = {k: v for k, v in values.items() if k != 'stealth_settings.identity'}  # an account always uses its own identity
		try:
			settings, _, max_steps, agent_kwargs = settings_from_ui(values, env)
		except (ValueError, RuntimeError) as e:
			raise HTTPException(400, str(e)) from e
		proxy_source = hub.proxy_source(settings)
		try:
			raw = job.url.strip()
			site, url = accounts.site_of(raw), accounts.normalize_url(raw)
			source = identities.profile_source(settings, proxy_source)
			log_ = accounts.SignupLog(signups_file)
		except ValueError as e:
			raise HTTPException(400, str(e)) from e
		action, if_registered = job.action, 'stop'
		if action == 'auto':  # "log in or sign up": log in where veil knows the account, else sign up (or log in if it exists)
			row = log_.get(site, job.profile_id)
			action, if_registered = ('login' if row and row.status in accounts.DONE_STATUSES else 'signup'), 'login'
		kind = 'signup' if action == 'signup' else 'login'
		try:
			# The identity server is a network call: keep it off the event loop (the live view streams through it).
			if kind == 'signup':
				profile, row, exists = await asyncio.to_thread(accounts.plan_signup, source, log_, site, job.profile_id, exclude)
			else:
				profile, row, logged = await asyncio.to_thread(accounts.plan_login, source, log_, site, job.profile_id)
		except identities.IdentityAPIError as e:
			raise HTTPException(502, str(e)) from e
		except ValueError as e:
			raise HTTPException(400, str(e)) from e
		fetch_code = source.code_fetcher(profile['id'], site)
		typed = accounts.placeholders(profile, row.password)
		if kind == 'signup':
			if exists:
				raise HTTPException(
					409,
					f'{site} already has an account ({row.status.replace("_", " ")}) with {row.email}. Not creating another; '
					f'say "log in to {site}" to use it.',
				)
			task = accounts.build_task(url, typed, ask_human=True, request=accounts.per_account(request_text) if request_text else None, if_registered=if_registered, codes=bool(fetch_code))

			def done(result) -> None:
				log_.record(accounts.finish(row, result.output, result.attempts))
				source.report(row, 'signup')

			verb = 'Signing up at'
		else:
			task = accounts.build_login_task(url, typed, ask_human=True, request=request_text, codes=bool(fetch_code))

			def done(result) -> None:
				rec = accounts.finish_login(row, result.output, result.attempts, logged)
				if rec:
					log_.record(rec)
				source.report(rec or row, 'login')

			verb = 'Logging in at'
		agent_kwargs['sensitive_data'] = accounts.sensitive_data(site, typed)  # the LLM only ever sees the x_* names
		who = f'{profile["id"]} ({profile["email"]})'
		if panel:
			title = f'{"Sign up" if kind == "signup" else "Log in"} at {site} as {who}'
		else:
			title = f'{request_text}\n\n→ {verb} {site} as {who}' if request_text else f'{verb} {site} as {who}'
		req = RunRequest(
			task=task, title=title, kind=kind, settings=settings, identity=row.identity, max_steps=max_steps, agent_kwargs=agent_kwargs,
			on_done=done, review=review, proxy_source=proxy_source,
			country=country_code(profile.get('country')),  # exit where the person is, when a proxy there exists
			fetch_code=fetch_code,
		)
		return req, site, profile['id']

	def task_request(task: str, values: dict[str, Any], review: bool, title: str | None = None) -> RunRequest:
		try:
			settings, identity, max_steps, agent_kwargs = settings_from_ui(values, env)
		except (ValueError, RuntimeError) as e:
			raise HTTPException(400, str(e)) from e
		return RunRequest(
			task=task, title=title, settings=settings, identity=identity, max_steps=max_steps, agent_kwargs=agent_kwargs, review=review,
			proxy_source=hub.proxy_source(settings),
		)

	async def start_group(task: str, jobs: list[Job], values: dict[str, Any], review: bool) -> dict[str, Any]:
		"""Each job in its own session and fresh browser, side by side (up to max_parallel at once)."""
		group = f'g{secrets.token_hex(3)}'
		planned: list[tuple[str, RunRequest]] = []
		skipped: list[str] = []
		taken: dict[str, set[str]] = defaultdict(set)  # site -> profiles this request already uses there
		for job in jobs:
			try:
				if job.is_account:
					site = accounts.site_of(job.url)
					if job.profile_id and job.profile_id in taken[site]:
						skipped.append(f'{site} as {job.profile_id}: already in this request')
						continue
					req, site, pid = await account_request(job, values, review, request_text=job.task or None, exclude=frozenset(taken[site]))
					taken[site].add(pid)
					req.title = f'{task}\n\n→ {req.title}'
					label = f'{"Sign up" if req.kind == "signup" else "Log in"} · {site} · {pid}'
				else:
					req = task_request(job.task, values, review, title=f'{task}\n\n→ {job.task}' if job.task != task else None)
					label = job.task[:60]
			except (HTTPException, ValueError) as e:
				skipped.append(str(e.detail if isinstance(e, HTTPException) else e))
				continue
			req.gate, req.group = hub.gate, group
			planned.append((label, req))
		if not planned:
			raise HTTPException(409 if skipped else 400, '; '.join(skipped) or 'nothing to run')
		started = []
		for label, req in planned:
			r = hub.add(group, label)
			r.start(req)
			started.append(r)
		return snap(started[0], group=group, skipped=skipped)

	@app.post('/api/run')
	async def run(request: Request):
		body = await body_of(request)
		task = str(body.get('task') or '').strip()
		if not task:
			raise HTTPException(400, 'enter a task')
		target = session_of(body.get('session'))
		if target.busy:
			raise HTTPException(409, 'a task is already running in this session: stop it first')
		values = {k: v for k, v in (body.get('values') or {}).items() if k in UI_VALUE_IDS}
		review = bool(body.get('review'))
		jobs = None
		if planner.worth_planning(task):
			try:
				settings = settings_from_ui(values, env)[0]
			except (ValueError, RuntimeError) as e:
				raise HTTPException(400, str(e)) from e
			try:
				llm = build_llm(settings)
			except (ValueError, RuntimeError):
				llm = None  # the run below will say why; here only a written domain is understood
			profiles = (await asyncio.to_thread(read_profiles))[0]
			known = list(accounts.SignupLog(signups_file).rows.values())
			jobs = await planner.plan(task, profiles, known, llm, **({'on_demand': True} if env.identity_api else {}))
			if jobs and any(j.is_account for j in jobs) and not profiles and not env.identity_api:
				raise HTTPException(400, 'To sign up or log in with your details, add a profile under Accounts (top right) first.')
		if jobs and len(jobs) > 1:
			return await start_group(task, jobs, values, review)
		if jobs:
			req, site, pid = await account_request(jobs[0], values, review, request_text=task)
		else:
			req = task_request(task, values, review)
		try:
			target.start(req)
		except (ValueError, RuntimeError) as e:
			raise HTTPException(400, str(e)) from e
		return snap(target)

	@app.post('/api/stop')
	async def stop(request: Request):
		r = session_of((await body_of(request)).get('session'))
		r.stop()
		return snap(r)

	@app.post('/api/pause')
	async def pause(request: Request):
		r = session_of((await body_of(request)).get('session'))
		r.pause_resume()
		return snap(r)

	@app.post('/api/takeover')
	async def takeover(request: Request):
		r = session_of((await body_of(request)).get('session'))
		r.take_over()
		return snap(r)

	@app.post('/api/handback')
	async def handback(request: Request):
		body = await body_of(request)
		r = session_of(body.get('session'))
		r.hand_back(str(body.get('note') or '')[:2000])
		return snap(r)

	@app.post('/api/steer')
	async def steer(request: Request):
		body = await body_of(request)
		r = session_of(body.get('session'))
		if not r.steer(str(body.get('text') or '')):
			raise HTTPException(409, 'nothing to steer: no run in progress')
		return snap(r)

	@app.post('/api/clear')
	async def clear(request: Request):
		"""The main chat starts over (finished side sessions close too); a side session closes."""
		r = session_of((await body_of(request)).get('session'))
		if r is runner:
			await runner.clear()
			hub.close_idle()
		elif r.busy:
			raise HTTPException(409, 'that session is still running: stop it first')
		else:
			hub.close(r.id)
		return snap(runner)

	@app.post('/api/sessions/close')
	async def close_session(request: Request):
		r = session_of((await body_of(request)).get('session'))
		if r is runner or not hub.close(r.id):
			raise HTTPException(409, 'only a finished side session can be closed')
		return snap(runner)

	# --- run history -------------------------------------------------------------------------

	@app.get('/api/runs')
	async def runs():
		return {'runs': await asyncio.to_thread(list_runs, runner.runs_dir)}

	@app.get('/api/runs/{run_id}')
	async def run_detail(run_id: str):
		run = await asyncio.to_thread(load_run, runner.runs_dir, run_id)
		if not run:
			raise HTTPException(404, 'no such run')
		return run

	@app.post('/api/runs/{run_id}/review')
	async def review_run(run_id: str, request: Request):
		body = await body_of(request)
		values = {k: v for k, v in (body.get('values') or {}).items() if k in UI_VALUE_IDS}
		live = hub.find_run(run_id)
		if live and live.busy:
			raise HTTPException(409, 'that run is still going')
		try:
			settings = settings_from_ui(values, env)[0]
			await review_saved_run(runner.runs_dir, run_id, settings)
		except (ValueError, RuntimeError) as e:
			raise HTTPException(400, str(e)) from e
		return await asyncio.to_thread(load_run, runner.runs_dir, run_id)

	@app.post('/api/runs/{run_id}/rename')
	async def rename(run_id: str, request: Request):
		title = ' '.join(str((await body_of(request)).get('title') or '').split())[:200]
		if not title:
			raise HTTPException(400, 'enter a name')
		if not await asyncio.to_thread(rename_run, runner.runs_dir, run_id, title):
			raise HTTPException(404, 'no such run')
		return {'ok': True}

	@app.post('/api/runs/{run_id}/delete')
	async def delete(run_id: str):
		live = hub.find_run(run_id)
		if live and live.busy:
			raise HTTPException(409, 'that run is still going: stop it first')
		if live is runner:
			await runner.clear()  # the chat was showing it
		elif live:
			hub.close(live.id)
		if not await asyncio.to_thread(delete_run, runner.runs_dir, run_id):
			raise HTTPException(404, 'no such run')
		return {'ok': True}

	# --- accounts: your own sign-up details, and where each was used ---------------------------

	@app.get('/api/accounts')
	async def accounts_state():
		profiles, error = await asyncio.to_thread(read_profiles)
		signups = sorted(accounts.SignupLog(signups_file).rows.values(), key=lambda x: x.updated, reverse=True)
		return {
			'profiles_file': str(profiles_file), 'signups_file': str(signups_file), 'error': error, 'identity_api': env.identity_api,
			'columns': accounts.PROFILE_COLUMNS, 'statuses': accounts.STATUSES, 'done_statuses': accounts.DONE_STATUSES,
			'profiles': [
				{
					**{k: v for k, v in p.items() if k not in accounts.SECRET_COLUMNS},
					'has_password': bool(p.get('password')),
					'has_mailbox_password': bool(p.get('mailbox_password')),
				}
				for p in profiles
			],
			'signups': [{**{k: v for k, v in asdict(x).items() if k != 'password'}, 'has_password': bool(x.password)} for x in signups],
		}

	@app.post('/api/accounts/profiles')
	async def save_profiles(request: Request):
		rows = (await body_of(request)).get('profiles')
		if env.identity_api:
			raise HTTPException(409, f'profiles come from the identity server at {env.identity_api}: edit them there (or unset VEIL_IDENTITY_API)')
		if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
			raise HTTPException(400, 'profiles must be a list')
		saved = {p['id']: p for p in read_profiles()[0]}
		merged = []
		for r in rows:
			row = {c: str(r.get(c) or '') for c in accounts.PROFILE_COLUMNS if c not in accounts.SECRET_COLUMNS}
			original = saved.get(str(r.get('original_id') or row['id'])) or saved.get(row['id'], {})  # after a rename, the id may be the old one
			for secret in accounts.SECRET_COLUMNS:  # secrets never come to the page, so not sent back: keep the saved ones
				row[secret] = str(r[secret]) if r.get(secret) is not None else original.get(secret, '')
			merged.append(row)
		try:
			accounts.save_profiles(profiles_file, merged)
		except ValueError as e:
			raise HTTPException(400, str(e)) from e
		return await accounts_state()

	@app.post('/api/accounts/signups/delete')
	async def delete_signup(request: Request):
		"""Forget one sign-up row (the account itself, if it exists on the site, is untouched)."""
		body = await body_of(request)
		try:
			site = accounts.site_of(str(body.get('site') or ''))
		except ValueError as e:
			raise HTTPException(400, str(e)) from e
		if not accounts.SignupLog(signups_file).remove(site, str(body.get('profile_id') or '')):
			raise HTTPException(404, 'no sign-up for that site and profile')
		return await accounts_state()

	@app.post('/api/accounts/reveal')
	async def reveal(request: Request):
		body = await body_of(request)
		row = accounts.SignupLog(signups_file).get(str(body.get('site') or ''), str(body.get('profile_id') or '') or None)
		if not row:
			raise HTTPException(404, 'no sign-up for that site')
		return {'site': row.site, 'email': row.email, 'password': row.password}

	async def account_from_panel(request: Request, action: str):
		body = await body_of(request)
		values = {k: v for k, v in (body.get('values') or {}).items() if k in UI_VALUE_IDS}
		job = Job(action, str(body.get('url') or ''), str(body.get('profile_id') or '') or None)
		req, site, pid = await account_request(job, values, bool(body.get('review')), panel=True)
		label = f'{"Sign up" if action == "signup" else "Log in"} · {site} · {pid}'
		return snap(start_on(runner, req, label))  # the chat's session when it's free, else alongside it

	@app.post('/api/accounts/signup')
	async def signup(request: Request):
		return await account_from_panel(request, 'signup')

	@app.post('/api/accounts/login')
	async def login(request: Request):
		return await account_from_panel(request, 'login')

	@app.post('/api/accounts/bulk')
	async def bulk_signup(request: Request):
		"""Create `count` NEW accounts on one site, side by side: each gets its own person, browser identity and
		a single-account instruction (the agent never learns how many accounts the request makes)."""
		body = await body_of(request)
		values = {k: v for k, v in (body.get('values') or {}).items() if k in UI_VALUE_IDS}
		raw_count = body.get('count')
		if raw_count is None:
			count = 1
		elif isinstance(raw_count, int) and not isinstance(raw_count, bool):
			count = raw_count
		elif isinstance(raw_count, str) and raw_count.strip().isdigit():
			count = int(raw_count.strip())
		else:
			raise HTTPException(400, 'how many accounts must be a whole number')
		if not 1 <= count <= env.max_bulk:
			raise HTTPException(400, f'ask for between 1 and {env.max_bulk} accounts at a time')
		raw = str(body.get('url') or '').strip()
		if not raw:
			raise HTTPException(400, 'pick the site to create accounts on')
		try:
			settings = settings_from_ui(values, env)[0]
			site, url = accounts.site_of(raw), accounts.normalize_url(raw)
		except (ValueError, RuntimeError) as e:
			raise HTTPException(400, str(e)) from e
		if env.identity_api:
			try:
				source = await asyncio.to_thread(identities.profile_source, settings, hub.proxy_source(settings))
			except ValueError as e:
				raise HTTPException(400, str(e)) from e
		else:
			profiles, error = await asyncio.to_thread(read_profiles)
			if not profiles:
				raise HTTPException(400, error or 'To create accounts, add a profile under Accounts (top right) first.')
			source = identities.LocalProfiles(profiles, mailbox_wait=env.mailbox_wait)
		log_ = accounts.SignupLog(signups_file)
		try:
			# Who signs up as whom: people without an account there yet, one per account (off the event loop:
			# the identity server is a network call).
			picks = await asyncio.to_thread(accounts.plan_fresh_signups, source, log_, site, count)
		except identities.IdentityAPIError as e:
			raise HTTPException(502, str(e)) from e
		except ValueError as e:
			raise HTTPException(400, str(e)) from e
		# The prompt box is extra instructions each account gets; counts in it are rewritten, never sent as-is.
		prompt = accounts.per_account(str(body.get('prompt') or ''))[:4000]
		jobs = [Job('signup', url, profile['id'], prompt) for profile, _ in picks]
		title = f'Create {count} account{"s" if count != 1 else ""} on {site}'
		return await start_group(title, jobs, values, bool(body.get('review')))

	admin.register(app, env)

	@app.get('/api/runs/{run}/{name}')
	async def run_file(run: str, name: str):
		if not (_RUN_FILE.match(run) and _RUN_FILE.match(name)) or '..' in run or '..' in name:
			raise HTTPException(404)
		path = runner.runs_dir / run / name
		if not path.is_file():
			raise HTTPException(404)
		return FileResponse(path)

	@app.websocket('/api/live')
	async def live(ws: WebSocket):
		if not origin_ok(ws.headers.get('origin'), allowed) or (host in LOOPBACK and not host_ok(ws.headers.get('host'), hosts)):
			await ws.close(code=1008)
			return
		await ws.accept()
		await LiveSocket(ws, hub.get(ws.query_params.get('session')) or runner).serve()

	if WEB_DIST.is_dir():
		app.mount('/', StaticFiles(directory=WEB_DIST, html=True), name='web')
	else:
		@app.get('/')
		async def not_built():
			return HTMLResponse(NOT_BUILT)

	return app


# Input that means "I'm doing something here": hand the browser to the person before it reaches the page,
# so the agent's next step can't fight them for the mouse.
def takes_control(ev: dict[str, Any]) -> bool:
	return (ev.get('t') == 'mouse' and ev.get('type') == 'mousePressed') or (ev.get('t') == 'key' and ev.get('type') == 'keyDown') or ev.get('t') in ('text', 'nav')


class LiveSocket:
	"""One viewer: follows whichever session the runner has, streams its frames, forwards input."""

	def __init__(self, ws: WebSocket, runner: AgentRunner):
		self.ws, self.runner = ws, runner
		self.view: LiveView | None = None
		self._session = None

	async def _attach(self) -> None:
		session = self.runner.session
		browser = getattr(session, 'browser', None)
		if session is self._session and (self.view or not browser):
			if self.view:
				try:
					if await self.view.follow():
						await self._send_info()
				except Exception as e:
					log.debug('live view follow: %s', e)
			return
		if self.view:
			await self.view.close()
			self.view = None
		self._session = session
		if browser:
			view = LiveView(browser)
			try:
				await view.start()
				self.view = view
			except Exception as e:
				log.debug('live view start: %s', e)
				await view.close()
				self._session = None  # retry next tick
		await self._send_info()

	async def _send_info(self) -> None:
		v = self.view
		await self.ws.send_json({'t': 'info', 'live': v is not None, 'url': v.url if v else '', 'title': v.title if v else ''})

	async def _pump(self) -> None:
		ticks = 0
		while True:
			await self._attach()
			if not self.view:
				await asyncio.sleep(0.5)
				continue
			frame = await self.view.next_frame(timeout=0.5)
			if frame:
				await self.ws.send_json({'t': 'frame', 'data': base64.b64encode(frame.jpeg).decode(), 'w': frame.width, 'h': frame.height})
			ticks += 1
			if ticks % 4 == 0:
				url, title = self.view.url, self.view.title
				await self.view.refresh_info()
				if (url, title) != (self.view.url, self.view.title):
					await self._send_info()

	async def _inputs(self) -> None:
		while True:
			ev = await self.ws.receive_json()
			if not isinstance(ev, dict) or not self.view:
				continue
			if takes_control(ev) and self.runner.status in ('running', 'paused'):
				self.runner.take_over()
			try:
				await self.view.dispatch(ev)
			except Exception as e:
				log.debug('live input %s failed: %s', ev.get('t'), e)

	async def serve(self) -> None:
		pump, inputs = asyncio.create_task(self._pump()), asyncio.create_task(self._inputs())
		try:
			await asyncio.wait({pump, inputs}, return_when=asyncio.FIRST_COMPLETED)
		except WebSocketDisconnect:
			pass
		finally:
			for t in (pump, inputs):
				t.cancel()
			await asyncio.gather(pump, inputs, return_exceptions=True)
			if self.view:
				await self.view.close()


def launch(env: Settings, host: str = '127.0.0.1', port: int = 7788, open_browser: bool = True, dev: bool = False) -> None:
	import threading
	import webbrowser

	import uvicorn

	app = create_app(env, host=host, port=port, dev=dev)
	if not WEB_DIST.is_dir():
		log.warning('chat app not built: run `npm install && npm run build` in %s', WEB_DIST.parent)
	if open_browser:
		threading.Timer(1.5, webbrowser.open, args=(f'http://{"127.0.0.1" if host in ("0.0.0.0", "::") else host}:{port}',)).start()
	uvicorn.run(app, host=host, port=port, log_level='warning', ws_max_size=16 * 1024 * 1024)
