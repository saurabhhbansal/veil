"""`veil ui`: the chat app (web/, adapted from browser-use/chat-ui-example) and the local API it talks to.

Everything runs on this PC: the agent, Fortress, and this server. No hosted browser service is involved.

    GET  /api/config            defaults from .env, for the settings bar (no secrets)
    GET  /api/state             the current run (AgentRunner.snapshot)
    GET  /api/events            the same, pushed as server-sent events whenever it changes
    POST /api/run               {task, values} start a task (values use the setting ids of manager.settings_from_ui)
    POST /api/stop | /api/pause | /api/clear
    POST /api/takeover          the person takes the browser (pauses the agent)
    POST /api/handback          {note} the person is done; the agent carries on
    WS   /api/live              live view: JPEG frames out, mouse/keyboard in (see live.py)
    GET  /api/runs/<run>/<file> step screenshots, recording.gif, result.json
    GET  /api/accounts          your sign-up profiles and the sign-up log (passwords left out)
    POST /api/accounts/profiles {profiles} replace profiles.csv; a profile sent without "password" keeps its saved one
    POST /api/accounts/signup   {url, profile_id?, values} sign up on a site as one of your profiles (a normal run)
    POST /api/accounts/reveal   {site} the password veil used on that site

The UI has no login, so requests are only accepted from this PC's own origin (or the dev server's): another
website open in the same browser can't drive the agent or the live view.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from veil import accounts
from veil.config import Settings
from veil.webui.live import LiveView
from veil.webui.manager import ENGINES, PROVIDERS, saved_identities, settings_from_ui
from veil.webui.runner import AgentRunner, RunRequest

log = logging.getLogger('veil.webui.server')

WEB_DIST = Path(__file__).resolve().parents[3] / 'web' / 'out'  # `npm run build` in web/
DEV_ORIGINS = {'http://localhost:3000', 'http://127.0.0.1:3000'}  # `npm run dev` in web/
# Which UI values a request may set: the ids settings_from_ui reads. Secrets (API key) are allowed in, never out.
UI_VALUE_IDS = {
	'agent_settings.llm_provider', 'agent_settings.llm_model', 'agent_settings.llm_api_key', 'agent_settings.llm_temperature',
	'agent_settings.max_steps', 'agent_settings.use_vision', 'agent_settings.max_actions', 'agent_settings.extend_system_prompt',
	'stealth_settings.engine', 'stealth_settings.headful', 'stealth_settings.identity', 'stealth_settings.max_attempts',
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
	runner = runner or AgentRunner(runs_dir=env.data_dir / 'runs')
	runner.runs_dir.mkdir(parents=True, exist_ok=True)
	allowed = allowed_origins(host, port, dev)
	hosts = allowed_origins(host, port, dev=False)
	app = FastAPI(title='veil', docs_url=None, redoc_url=None, openapi_url=None)
	app.state.runner = runner

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
		return {
			'llm_provider': env.llm_provider, 'llm_model': env.llm_model, 'has_api_key': bool(env.llm_api_key),
			'providers': PROVIDERS, 'engine': env.engine, 'engines': ENGINES, 'headless': env.headless,
			'geo_match': env.geo_match, 'max_attempts': env.max_attempts, 'max_steps': env.max_steps,
			'identities': saved_identities(env),
			'proxy': 'gateway' if env.proxy_gateway else 'list' if env.proxy_file else None,
			'proxy_detail': str(env.proxy_file) if env.proxy_file and not env.proxy_gateway else None,
		}

	@app.get('/api/state')
	async def state():
		return runner.snapshot()

	@app.get('/api/events')
	async def events(request: Request):
		async def stream():
			last = None
			idle = 0
			while not await request.is_disconnected():
				key = (runner.version, runner.status)
				if key != last:
					last, idle = key, 0
					yield f'data: {json.dumps(runner.snapshot(), default=str)}\n\n'
				else:
					idle += 1
					if idle % 50 == 0:
						yield ': keep-alive\n\n'
				await asyncio.sleep(0.3)

		return StreamingResponse(stream(), media_type='text/event-stream', headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

	@app.post('/api/run')
	async def run(request: Request):
		body = await request.json()
		task = str(body.get('task') or '').strip()
		if not task:
			raise HTTPException(400, 'enter a task')
		if runner.busy:
			raise HTTPException(409, 'a task is already running: stop it first')
		values = {k: v for k, v in (body.get('values') or {}).items() if k in UI_VALUE_IDS}
		try:
			settings, identity, max_steps, agent_kwargs = settings_from_ui(values, env)
			runner.start(RunRequest(task=task, settings=settings, identity=identity, max_steps=max_steps, agent_kwargs=agent_kwargs))
		except (ValueError, RuntimeError) as e:
			raise HTTPException(400, str(e)) from e
		return runner.snapshot()

	@app.post('/api/stop')
	async def stop():
		runner.stop()
		return runner.snapshot()

	@app.post('/api/pause')
	async def pause():
		runner.pause_resume()
		return runner.snapshot()

	@app.post('/api/takeover')
	async def takeover():
		runner.take_over()
		return runner.snapshot()

	@app.post('/api/handback')
	async def handback(request: Request):
		body = await request.json() if request.headers.get('content-type', '').startswith('application/json') else {}
		runner.hand_back(str(body.get('note') or '')[:2000])
		return runner.snapshot()

	@app.post('/api/clear')
	async def clear():
		await runner.clear()
		return runner.snapshot()

	# --- accounts: your own sign-up details, and where each was used ---------------------------

	profiles_file, signups_file = accounts.profiles_path(env), env.accounts_dir / 'signups.csv'

	def read_profiles() -> tuple[list[dict[str, str]], str | None]:
		try:
			return accounts.load_profiles(profiles_file, allow_empty=True), None
		except ValueError as e:
			return [], str(e)

	@app.get('/api/accounts')
	async def accounts_state():
		profiles, error = read_profiles()
		signups = sorted(accounts.SignupLog(signups_file).rows.values(), key=lambda x: x.updated, reverse=True)
		return {
			'profiles_file': str(profiles_file), 'signups_file': str(signups_file), 'error': error,
			'columns': accounts.PROFILE_COLUMNS, 'statuses': accounts.STATUSES, 'done_statuses': accounts.DONE_STATUSES,
			'profiles': [{**{k: v for k, v in p.items() if k != 'password'}, 'has_password': bool(p.get('password'))} for p in profiles],
			'signups': [{**{k: v for k, v in asdict(x).items() if k != 'password'}, 'has_password': bool(x.password)} for x in signups],
		}

	@app.post('/api/accounts/profiles')
	async def save_profiles(request: Request):
		rows = (await request.json()).get('profiles')
		if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
			raise HTTPException(400, 'profiles must be a list')
		saved = {p['id']: p for p in read_profiles()[0]}
		merged = []
		for r in rows:
			row = {c: str(r.get(c) or '') for c in accounts.PROFILE_COLUMNS if c != 'password'}
			if r.get('password') is not None:
				row['password'] = str(r['password'])
			else:  # never sent to the page, so not sent back: keep what's saved (under the id it had before an edit)
				row['password'] = saved.get(str(r.get('original_id') or row['id']), {}).get('password', '')
			merged.append(row)
		try:
			accounts.save_profiles(profiles_file, merged)
		except ValueError as e:
			raise HTTPException(400, str(e)) from e
		return await accounts_state()

	@app.post('/api/accounts/reveal')
	async def reveal(request: Request):
		row = accounts.SignupLog(signups_file).get(str((await request.json()).get('site') or ''))
		if not row:
			raise HTTPException(404, 'no sign-up for that site')
		return {'site': row.site, 'email': row.email, 'password': row.password}

	@app.post('/api/accounts/signup')
	async def signup(request: Request):
		body = await request.json()
		if runner.busy:
			raise HTTPException(409, 'a task is already running: stop it first')
		try:
			raw = str(body.get('url') or '').strip()
			site, url = accounts.site_of(raw), accounts.normalize_url(raw)
			profiles = accounts.load_profiles(profiles_file)
			log_ = accounts.SignupLog(signups_file)
			profile, row, exists = accounts.plan_signup(profiles, log_, site, str(body.get('profile_id') or '') or None)
		except ValueError as e:
			raise HTTPException(400, str(e)) from e
		if exists:
			raise HTTPException(409, f'{site} already has an account ({row.status}) with {row.email}; not creating another')
		values = {k: v for k, v in (body.get('values') or {}).items() if k in UI_VALUE_IDS and k != 'stealth_settings.identity'}
		settings, _, max_steps, agent_kwargs = settings_from_ui(values, env)
		typed = accounts.placeholders(profile, row.password)
		agent_kwargs['sensitive_data'] = accounts.sensitive_data(site, typed)  # the LLM only ever sees the x_* names

		def done(result) -> None:
			log_.record(accounts.finish(row, result.output, result.attempts))

		runner.start(RunRequest(
			task=accounts.build_task(url, typed, ask_human=True), title=f'Sign up at {site} as {profile["id"]} ({profile["email"]})',
			kind='signup', settings=settings, identity=row.identity, max_steps=max_steps, agent_kwargs=agent_kwargs, on_done=done,
		))
		return runner.snapshot()

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
		await LiveSocket(ws, runner).serve()

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
