"""Settings the app can change and save to .env: LLM API keys, the default model and effort, CAPTCHA solving, and proxies.

    GET  /api/keys              which provider keys are set (masked: last 4 characters only)
    POST /api/keys              {provider, key} save a provider's key to .env ("" removes it)
    POST /api/llm/default       {provider, model, effort} make these the .env defaults
    GET  /api/captcha           whether CAPTCHA solving is on, the provider, and whether a key is saved (masked)
    POST /api/captcha           {provider, key?} switch solver provider and save its key to .env ("" removes it)
    GET  /api/proxies           mode (direct | list | gateway), the list (credentials left out), last test results
    POST /api/proxies/add       {text} paste proxies, one per line (any format Proxy.parse takes)
    POST /api/proxies/remove    {ids}
    POST /api/proxies/mode      {mode, gateway?, strategy?}
    POST /api/proxies/test      {ids?, gateway?} exit IP, country, city and latency through each

Keys and proxy credentials go in, never back out: the page only sees masked values and redacted proxies.
Changes apply to the next run straight away (this process's Settings and environment are updated too).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from veil import envfile
from veil.config import Settings
from veil.geo import lookup_exit
from veil.llm import EFFORTS, KEY_ENV
from veil.proxy import GatewayProxy, Proxy, parse_many, proxy_id, read_list, write_list
from veil.webui.manager import PROVIDERS

STRATEGIES = ['round_robin', 'random', 'least_used']
CAPTCHA_PROVIDERS = ['2captcha', 'capsolver']
_KEY_OK = re.compile(r'^[\x21-\x7e]{8,512}$')  # printable, no spaces or quotes problems for .env


async def _body(request: Request) -> dict[str, Any]:
	try:
		body = await request.json()
	except ValueError:
		body = None
	if not isinstance(body, dict):
		raise HTTPException(400, 'expected a JSON object')
	return body


def register(app: FastAPI, env: Settings) -> None:
	env_path = envfile.env_path(env.env_file)
	status_file = env.data_dir / 'proxy-status.json'

	# --- LLM keys and defaults -----------------------------------------------------------------

	def keys_state() -> dict[str, Any]:
		names = envfile.read_names(env_path)
		out = {}
		for p, var in KEY_ENV.items():
			via_veil = p == env.llm_provider and bool(env.llm_api_key)
			value = env.llm_api_key if via_veil else os.environ.get(var)
			source = 'VEIL_LLM_API_KEY' if via_veil else var if value else None
			out[p] = {'var': var, 'set': bool(value), 'masked': envfile.mask(value), 'source': source, 'in_file': bool(source and source in names)}
		return {'env_file': str(env_path), 'keys': out}

	@app.get('/api/keys')
	async def keys():
		return keys_state()

	@app.post('/api/keys')
	async def save_key(request: Request):
		body = await _body(request)
		provider, key = str(body.get('provider') or ''), str(body.get('key') or '').strip()
		var = KEY_ENV.get(provider)
		if not var:
			raise HTTPException(400, f'no key setting for {provider!r}')
		if key and (not _KEY_OK.match(key) or '"' in key or "'" in key):
			raise HTTPException(400, "that doesn't look like an API key (no spaces or quotes, 8-512 characters)")
		values: dict[str, str | None] = {var: key or None}
		if provider == env.llm_provider and os.environ.get('VEIL_LLM_API_KEY'):
			values['VEIL_LLM_API_KEY'] = key or None  # it overrides the provider's own variable: keep them the same
		try:
			envfile.write(env_path, values)
		except OSError as e:
			raise HTTPException(500, f'could not write {env_path}: {e}') from e
		if provider == env.llm_provider:
			env.llm_api_key = os.environ.get('VEIL_LLM_API_KEY') or None
		return keys_state()

	@app.post('/api/llm/default')
	async def llm_default(request: Request):
		body = await _body(request)
		provider, model, effort = str(body.get('provider') or ''), str(body.get('model') or '').strip(), str(body.get('effort') or '')
		if provider not in PROVIDERS:
			raise HTTPException(400, f'unknown provider {provider!r}')
		if not model or len(model) > 120 or not re.match(r'^[\w.:/@-]+$', model):
			raise HTTPException(400, 'enter a model name')
		if effort and effort not in EFFORTS.get(provider, []):
			raise HTTPException(400, f'{provider} has no effort {effort!r}')
		values: dict[str, str | None] = {'VEIL_LLM_PROVIDER': provider, 'VEIL_LLM_MODEL': model, 'VEIL_LLM_EFFORT': effort or None}
		if provider != env.llm_provider and os.environ.get('VEIL_LLM_API_KEY'):
			# VEIL_LLM_API_KEY belongs to the old default provider: move it to that provider's own variable
			# rather than send it to the new one.
			old_var = KEY_ENV.get(env.llm_provider)
			if old_var and not os.environ.get(old_var):
				values[old_var] = os.environ['VEIL_LLM_API_KEY']
			values['VEIL_LLM_API_KEY'] = None
		envfile.write(env_path, values)
		env.llm_provider, env.llm_model, env.llm_effort = provider, model, effort or None
		env.llm_api_key = os.environ.get('VEIL_LLM_API_KEY') or None
		return {'ok': True, **keys_state()}

	# --- CAPTCHA solving ----------------------------------------------------------------------

	def captcha_state() -> dict[str, Any]:
		return {
			'provider': env.captcha_provider, 'has_key': bool(env.captcha_api_key), 'masked': envfile.mask(env.captcha_api_key),
			'providers': CAPTCHA_PROVIDERS, 'timeout': env.captcha_timeout, 'env_file': str(env_path),
		}

	@app.get('/api/captcha')
	async def captcha():
		return captcha_state()

	@app.post('/api/captcha')
	async def save_captcha(request: Request):
		body = await _body(request)
		provider = str(body.get('provider') or '').strip().lower()
		if provider and provider not in CAPTCHA_PROVIDERS:
			raise HTTPException(400, f'unknown CAPTCHA provider {provider!r}')
		values: dict[str, str | None] = {'VEIL_CAPTCHA_PROVIDER': provider or None}
		if 'key' in body:
			key = str(body.get('key') or '').strip()
			if key and (not _KEY_OK.match(key) or '"' in key or "'" in key):
				raise HTTPException(400, "that doesn't look like an API key (no spaces or quotes, 8-512 characters)")
			values['VEIL_CAPTCHA_API_KEY'] = key or None
		try:
			envfile.write(env_path, values)
		except OSError as e:
			raise HTTPException(500, f'could not write {env_path}: {e}') from e
		env.captcha_provider = provider or None
		if 'key' in body:
			env.captcha_api_key = os.environ.get('VEIL_CAPTCHA_API_KEY') or None
		return captcha_state()

	# --- proxies -------------------------------------------------------------------------------

	def list_file() -> Path:
		pf = os.environ.get('VEIL_PROXY_FILE', '').strip()
		return Path(pf).expanduser() if pf else env.data_dir / 'proxies.txt'

	def gateway_template() -> str | None:
		return os.environ.get('VEIL_PROXY_GATEWAY', '').strip() or None

	def mode() -> str:
		return 'gateway' if env.proxy_gateway else 'list' if env.proxy_file else 'direct'

	def statuses() -> dict[str, Any]:
		try:
			data = json.loads(status_file.read_text(encoding='utf-8'))
			return data if isinstance(data, dict) else {}
		except (OSError, ValueError):
			return {}

	def proxies_state(errors: list[str] | None = None, note: str | None = None) -> dict[str, Any]:
		st = statuses()
		gw, gateway = gateway_template(), None
		if gw:
			try:
				g = GatewayProxy(gw)
				gateway = {'server': Proxy.parse(gw.replace('{session}', 'SESSION')).redacted, 'sticky': g.sticky, 'status': st.get('gateway')}
			except ValueError as e:
				gateway = {'server': None, 'sticky': False, 'error': str(e), 'status': None}
		path = list_file()
		return {
			'mode': mode(), 'file': str(path), 'strategy': env.proxy_strategy, 'strategies': STRATEGIES, 'gateway': gateway,
			'proxies': [
				{'id': proxy_id(p), 'server': p.redacted, 'scheme': p.scheme, 'host': p.host, 'port': p.port, 'auth': p.has_auth, 'status': st.get(proxy_id(p))}
				for p in read_list(path)
			],
			'errors': errors or [], 'note': note, 'env_file': str(env_path),
		}

	def use(new_mode: str) -> None:
		"""Switch the source the next runs use, and remember it in .env."""
		if new_mode == 'list':
			values: dict[str, str | None] = {'VEIL_PROXY_MODE': 'list'}
			if not os.environ.get('VEIL_PROXY_FILE'):
				values['VEIL_PROXY_FILE'] = str(list_file())
			envfile.write(env_path, values)
			env.proxy_file, env.proxy_gateway = list_file(), None
		elif new_mode == 'gateway':
			envfile.write(env_path, {'VEIL_PROXY_MODE': 'gateway'})
			env.proxy_gateway = gateway_template()
		else:
			envfile.write(env_path, {'VEIL_PROXY_MODE': 'direct'})
			env.proxy_file = env.proxy_gateway = None

	@app.get('/api/proxies')
	async def proxies():
		return proxies_state()

	@app.post('/api/proxies/add')
	async def add_proxies(request: Request):
		new, errors = parse_many(str((await _body(request)).get('text') or '')[:200_000])
		if not new:
			raise HTTPException(400, '; '.join(errors) if errors else 'paste at least one proxy')
		path = list_file()
		current = read_list(path)
		seen = {p.url for p in current}
		added = [p for p in new if p.url not in seen and not seen.add(p.url)]
		write_list(path, current + added)
		note = f'Added {len(added)} prox{"y" if len(added) == 1 else "ies"}' + (f' ({len(new) - len(added)} already listed)' if len(added) < len(new) else '') + '.'
		if mode() == 'direct':
			use('list')
			note += ' Runs now use the list.'
		return proxies_state(errors, note)

	@app.post('/api/proxies/remove')
	async def remove_proxies(request: Request):
		ids = set((await _body(request)).get('ids') or [])
		path = list_file()
		keep = [p for p in read_list(path) if proxy_id(p) not in ids]
		write_list(path, keep)
		note = None
		if not keep and mode() == 'list':
			use('direct')
			note = 'The list is empty: runs use this PC\'s own connection now.'
		return proxies_state(note=note)

	@app.post('/api/proxies/mode')
	async def proxy_mode(request: Request):
		body = await _body(request)
		new_mode = str(body.get('mode') or mode())
		if new_mode not in ('direct', 'list', 'gateway'):
			raise HTTPException(400, f'unknown mode {new_mode!r}')
		strategy = body.get('strategy')
		if strategy:
			if strategy not in STRATEGIES:
				raise HTTPException(400, f'unknown rotation {strategy!r}')
			envfile.write(env_path, {'VEIL_PROXY_STRATEGY': strategy})
			env.proxy_strategy = strategy
		gateway = str(body.get('gateway') or '').strip()
		if gateway:
			try:
				GatewayProxy(gateway)
			except ValueError as e:
				raise HTTPException(400, str(e)) from e
			envfile.write(env_path, {'VEIL_PROXY_GATEWAY': gateway})
		if new_mode == 'list' and not read_list(list_file()):
			raise HTTPException(400, 'add a proxy to the list first')
		if new_mode == 'gateway' and not gateway_template():
			raise HTTPException(400, 'enter the gateway address first')
		use(new_mode)
		return proxies_state()

	@app.post('/api/proxies/test')
	async def test_proxies(request: Request):
		body = await _body(request)
		ids = set(body.get('ids') or [])
		targets: list[tuple[str, Proxy]] = [(proxy_id(p), p) for p in read_list(list_file()) if not ids or proxy_id(p) in ids]
		if body.get('gateway') and gateway_template():
			targets = ([] if body.get('ids') is None else targets) + [('gateway', GatewayProxy(gateway_template()).acquire())]
		targets = targets[:50]

		async def probe(p: Proxy) -> dict[str, Any]:
			t = time.perf_counter()
			try:
				info = await lookup_exit(p, timeout=20)
				out = {'ok': True, 'ip': info.ip, 'country': info.country, 'city': info.city, 'timezone': info.timezone}
			except Exception as e:
				out = {'ok': False, 'error': f'{type(e).__name__}: {e}'[:240]}
			return {**out, 'ms': round((time.perf_counter() - t) * 1000), 'at': time.time()}

		results = await asyncio.gather(*(probe(p) for _, p in targets))
		st = statuses()
		st.update({tid: r for (tid, _), r in zip(targets, results)})
		status_file.parent.mkdir(parents=True, exist_ok=True)
		status_file.write_text(json.dumps(st, indent=1), encoding='utf-8')
		return proxies_state()
