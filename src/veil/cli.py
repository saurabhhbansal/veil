"""Command line: `veil run`, `veil signup`, `veil accounts`, `veil ui`, `veil check`, `veil persona`, `veil proxies`, `veil identities`."""
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


def _cmd_signup(args: argparse.Namespace, settings: Settings) -> int:
	from veil import accounts
	from veil.agent import run_task

	url, site = accounts.normalize_url(args.url), accounts.site_of(args.url)
	profiles = accounts.load_profiles(accounts.profiles_path(settings))
	log = accounts.SignupLog(settings.accounts_dir / 'signups.csv')
	profile, signup, exists = accounts.plan_signup(profiles, log, site, args.profile)
	if exists:
		print(f'{site}: already {signup.status} with {signup.email} (profile {signup.profile_id}, {signup.updated}). Not creating another account.')
		return 0

	print(f'{site}: signing up as profile {profile["id"]} ({profile["email"]}), sticky identity {signup.identity}')
	values = accounts.placeholders(profile, signup.password)
	result = asyncio.run(
		run_task(
			accounts.build_task(url, values),
			settings,
			identity_name=signup.identity,  # same device, IP and cookies when we come back to this site
			max_steps=args.max_steps,
			agent_kwargs={'sensitive_data': accounts.sensitive_data(site, values)},
		)
	)
	log.record(accounts.finish(signup, result.output, result.attempts))
	print(f'{site}: {signup.status} - {signup.note}')
	return 0 if signup.status in accounts.DONE_STATUSES else 1


def _cmd_accounts(args: argparse.Namespace, settings: Settings) -> int:
	from veil import accounts

	path = accounts.profiles_path(settings)
	if args.action == 'init':
		print(f'{"created" if accounts.init_profiles(path) else "already exists:"} {path}')
		print('Fill in one row per email you own (dob as YYYY-MM-DD; leave password blank to get a random one per site).')
		return 0
	if args.action == 'profiles':
		for p in accounts.load_profiles(path):
			filled = [c for c in accounts.PROFILE_COLUMNS if p.get(c) and c not in ('id', 'email', 'password')]
			print(f'{p["id"]:12} {p["email"]:35} {", ".join(filled)}')
		return 0
	log = accounts.SignupLog(settings.accounts_dir / 'signups.csv')
	if not log.rows:
		print(f'no signups yet ({log.path})')
	for s in sorted(log.rows.values(), key=lambda s: (s.site, s.profile_id)):
		print(f'{s.site:30} {s.profile_id:12} {s.status:18} {s.email:35} {s.updated:16} {s.note[:80]}')
	return 0


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
	if args.host not in ('127.0.0.1', 'localhost'):
		print(f'warning: the UI has no login and holds your API key; anyone who can reach {args.host}:{args.port} can drive it', file=sys.stderr)
	from veil.webui.server import launch

	print(f'veil ui on http://{args.host}:{args.port}' + (' (API for `npm run dev` on :3000)' if args.dev else ''))
	launch(settings, host=args.host, port=args.port, open_browser=not args.no_browser and not args.dev, dev=args.dev)
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

	sg = sub.add_parser('signup', help='create an account on a site with one of your profiles (see `veil accounts`)')
	sg.add_argument('url')
	sg.add_argument('--profile', help='profile id to use (default: random, or the one already used on this site)')
	sg.add_argument('--max-steps', type=int)
	sg.set_defaults(fn=_cmd_signup)

	ac = sub.add_parser('accounts', help='your sign-up profiles and where each was used')
	ac.add_argument('action', nargs='?', default='list', choices=['list', 'profiles', 'init'], help='list signups (default) | profiles | init (create profiles.csv)')
	ac.set_defaults(fn=_cmd_accounts)

	c = sub.add_parser('check',help='verify the fingerprint a page sees matches the persona')
	c.add_argument('--screenshots', action='store_true', help='also screenshot public bot-detector pages into reports/')
	c.set_defaults(fn=_cmd_check)

	p = sub.add_parser('persona', help='print a generated persona')
	p.add_argument('--seed', type=int)
	p.add_argument('--json', action='store_true')
	p.set_defaults(fn=_cmd_persona)

	x = sub.add_parser('proxies', help='test configured proxies: exit IP, country, latency')
	x.add_argument('--samples', type=int, default=3, help='gateway mode: how many sessions to sample')
	x.set_defaults(fn=_cmd_proxies)

	u = sub.add_parser('ui', help='open the chat app: task chat, interactive live browser, take over / hand back, pause/stop')
	u.add_argument('--host', default='127.0.0.1', help='interface to bind (default: this PC only)')
	u.add_argument('--port', type=int, default=7788)
	u.add_argument('--dev', action='store_true', help='API only, for the chat app under `npm run dev` in web/')
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
