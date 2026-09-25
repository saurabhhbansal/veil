"""Command line: `veil run`, `veil ui`, `veil check`, `veil persona`, `veil proxies`, `veil identities`."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time

from veil import persona as persona_mod
from veil.config import Settings
from veil.geo import lookup_exit
from veil.proxy import GatewayProxy, ProxyPool


def _cmd_run(args: argparse.Namespace, settings: Settings) -> int:
	from veil.agent import run_task

	result = asyncio.run(run_task(args.task, settings, identity_name=args.identity, max_steps=args.max_steps))
	for i, a in enumerate(result.attempts, 1):
		print(f'attempt {i}: {a.outcome:8} proxy={a.proxy or "direct"} exit={a.exit_ip or "?"} {a.detail}')
	print('\n' + (result.output or '(no output)'))
	return 0 if result.ok else 1


def _cmd_check(args: argparse.Namespace, settings: Settings) -> int:
	from veil.check import run_check

	return 0 if asyncio.run(run_check(settings, screenshots=args.screenshots)) else 1


def _cmd_persona(args: argparse.Namespace, settings: Settings) -> int:
	p = persona_mod.generate(seed=args.seed)
	print(p.summary())
	print(json.dumps(p.to_dict(), indent=2) if args.json else '\n'.join(p.flags()))
	return 0


def _cmd_proxies(args: argparse.Namespace, settings: Settings) -> int:
	if settings.proxy_gateway:
		gw = GatewayProxy(settings.proxy_gateway)
		proxies = [gw.acquire() for _ in range(args.samples)]
		print(f'gateway {proxies[0].redacted} (sticky sessions: {"yes" if gw.sticky else "NO - add {session} to the username"})')
	elif settings.proxy_file:
		proxies = ProxyPool.from_file(settings.proxy_file).proxies
	else:
		print('no proxies configured: set VEIL_PROXY_FILE or VEIL_PROXY_GATEWAY')
		return 1

	async def probe(p):
		t = time.perf_counter()
		try:
			info = await lookup_exit(p, timeout=20)
			return f'OK   {time.perf_counter() - t:5.1f}s  {p.redacted:55} -> {info.ip} {info.country} {info.timezone}'
		except Exception as e:
			return f'FAIL {time.perf_counter() - t:5.1f}s  {p.redacted:55} -> {e}'

	async def main():
		for line in await asyncio.gather(*(probe(p) for p in proxies)):
			print(line)

	asyncio.run(main())
	return 0


def _cmd_ui(args: argparse.Namespace, settings: Settings) -> int:
	from veil.webui.interface import launch

	if args.host not in ('127.0.0.1', 'localhost'):
		print(f'warning: the UI has no login and holds your API key; anyone who can reach {args.host}:{args.port} can drive it', file=sys.stderr)
	launch(settings, host=args.host, port=args.port, open_browser=not args.no_browser, theme=args.theme)
	return 0


def _cmd_identities(args: argparse.Namespace, settings: Settings) -> int:
	root = settings.data_dir / 'identities'
	for d in sorted(root.glob('*/identity.json')) if root.exists() else []:
		ident = json.loads(d.read_text())
		p = persona_mod.generate(seed=ident['persona_seed'])
		print(f'{ident["name"]:20} {p.summary()}  proxy={ident.get("proxy") and "pinned" or "gateway session " + ident["proxy_session"]}')
	return 0


def main(argv: list[str] | None = None) -> int:
	ap = argparse.ArgumentParser(prog='veil', description='Stealth browser sessions for AI agents (Fortress + browser-use).')
	ap.add_argument('-v', '--verbose', action='store_true')
	ap.add_argument('--engine', help='override VEIL_ENGINE (fortress | fortress-docker | cdp | chromium)')
	ap.add_argument('--headful', action='store_true', help='show the browser window')
	sub = ap.add_subparsers(dest='cmd', required=True)

	r = sub.add_parser('run', help='run an agent task')
	r.add_argument('task')
	r.add_argument('--identity', help='named sticky identity (same device, IP and cookies across runs)')
	r.add_argument('--max-steps', type=int)
	r.set_defaults(fn=_cmd_run)

	c = sub.add_parser('check', help='verify the fingerprint a page sees matches the persona')
	c.add_argument('--screenshots', action='store_true', help='also screenshot public bot-detector pages into reports/')
	c.set_defaults(fn=_cmd_check)

	p = sub.add_parser('persona', help='print a generated persona')
	p.add_argument('--seed', type=int)
	p.add_argument('--json', action='store_true')
	p.set_defaults(fn=_cmd_persona)

	x = sub.add_parser('proxies', help='test configured proxies: exit IP, country, latency')
	x.add_argument('--samples', type=int, default=3, help='gateway mode: how many sessions to sample')
	x.set_defaults(fn=_cmd_proxies)

	u = sub.add_parser('ui', help='open the web control panel (task box, live view, identity, pause/stop)')
	u.add_argument('--host', default='127.0.0.1', help='interface to bind (default: this PC only)')
	u.add_argument('--port', type=int, default=7788)
	u.add_argument('--theme', default='Ocean', help='Gradio theme: Ocean, Soft, Monochrome, Glass, Origin, Citrus, Base, Default')
	u.add_argument('--no-browser', action='store_true', help="don't open the page automatically")
	u.set_defaults(fn=_cmd_ui)

	i = sub.add_parser('identities', help='list saved sticky identities')
	i.set_defaults(fn=_cmd_identities)

	args = ap.parse_args(argv)
	logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format='%(levelname)-7s %(name)s: %(message)s')
	settings = Settings.from_env()
	if args.engine:
		settings.engine = args.engine
	if args.headful:
		settings.headless = False
	try:
		return args.fn(args, settings)
	except (RuntimeError, ValueError) as e:
		print(f'error: {e}', file=sys.stderr)
		return 2
