"""StealthSession: one identity (proxy exit + matching persona + profile) wired into a browser-use BrowserSession.

    async with StealthSession(settings) as s:
        agent = Agent(task=..., llm=..., browser_session=s.browser)

Identities are ephemeral by default: a fresh device, exit IP and empty profile every time.
A named identity persists the persona seed, the gateway session token and the profile dir
under ``<data_dir>/identities/<name>`` so logins survive across runs and the site keeps
seeing the same device from the same place.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import tempfile
from dataclasses import dataclass
from pathlib import Path

from browser_use import BrowserProfile, BrowserSession
from browser_use.browser.profile import ProxySettings

from veil import persona as persona_mod
from veil.config import Settings
from veil.detection import PAGE_PROBE_JS, BlockSignal, classify
from veil.engine import Engine, LaunchedBrowser, make_engine
from veil.geo import ExitInfo, lookup_exit
from veil.persona import Persona
from veil.proxy import GatewayProxy, NoProxy, Proxy, ProxyPool, ProxySource, load_countries

log = logging.getLogger('veil.session')

_NAME_RE = re.compile(r'^[A-Za-z0-9_.-]{1,64}$')


def make_proxy_source(settings: Settings) -> ProxySource:
	if settings.proxy_gateway:
		return GatewayProxy(settings.proxy_gateway, default_country=settings.proxy_country or 'us')
	if settings.proxy_file:
		pool = ProxyPool.from_file(settings.proxy_file, strategy=settings.proxy_strategy)
		pool.countries = load_countries(settings.data_dir / 'proxy-status.json', pool.proxies)
		return pool
	return NoProxy()


@dataclass
class Identity:
	"""What makes a returning visitor look like the same person."""

	name: str
	persona_seed: int
	proxy_session: str  # gateway {session} token (sticky IP); ignored for static pools
	proxy: str | None = None  # pinned static proxy for pool mode

	@staticmethod
	def root(settings: Settings, name: str) -> Path:
		if not _NAME_RE.match(name):
			raise ValueError(f'identity names may only use letters, digits, "_", "." and "-": {name!r}')
		return settings.data_dir / 'identities' / name

	@classmethod
	def load_or_create(cls, settings: Settings, name: str) -> Identity:
		path = cls.root(settings, name) / 'identity.json'
		if path.exists():
			return cls(**json.loads(path.read_text()))
		ident = cls(name=name, persona_seed=secrets.randbelow(2**31 - 1) + 1, proxy_session=secrets.token_hex(6))
		ident.save(settings)
		return ident

	def save(self, settings: Settings) -> None:
		root = self.root(settings, self.name)
		root.mkdir(parents=True, exist_ok=True)
		(root / 'identity.json').write_text(json.dumps(self.__dict__, indent=2))

	def profile_dir(self, settings: Settings) -> Path:
		return self.root(settings, self.name) / 'profile'


class StealthSession:
	def __init__(
		self,
		settings: Settings,
		identity: Identity | None = None,
		proxy_source: ProxySource | None = None,
		engine: Engine | None = None,
		country: str | None = None,
	):
		"""country: ISO code of where the person using this session is; the exit is picked there when possible."""
		self.settings = settings
		self.identity = identity
		self.country = country
		self.proxy_source = proxy_source or make_proxy_source(settings)
		self.engine = engine or make_engine(settings)
		self.proxy: Proxy | None = None
		self.exit: ExitInfo | None = None
		self.persona: Persona | None = None
		self.browser: BrowserSession | None = None
		self._launched: LaunchedBrowser | None = None
		self._outcome_reported = False

	async def __aenter__(self) -> StealthSession:
		try:
			await self._open()
		except BaseException as e:
			if self.proxy and not isinstance(e, asyncio.CancelledError):  # e.g. the exit lookup or the browser couldn't get through it
				self.report(ok=False)
			await self.close()
			raise
		return self

	async def __aexit__(self, exc_type, exc, tb) -> None:
		# A cancellation is the user stopping the run, not the proxy failing.
		if exc_type is not None and not issubclass(exc_type, asyncio.CancelledError) and not self._outcome_reported:
			self.report(ok=False)
		await self.close()

	async def _open(self) -> None:
		s, ident = self.settings, self.identity

		# 1. Network identity
		if ident and ident.proxy:
			self.proxy = Proxy.parse(ident.proxy)
		else:
			key = ident.proxy_session if ident else None
			self.proxy = self.proxy_source.acquire(key, country=self.country) if self.country else self.proxy_source.acquire(key)
			if ident and isinstance(self.proxy_source, ProxyPool) and self.proxy:
				ident.proxy = self.proxy.url  # pin: a returning identity shouldn't hop IPs
				ident.save(s)

		# 2. Where does that exit? (drives timezone + languages)
		if s.geo_match:
			self.exit = await lookup_exit(self.proxy)
			log.info('exit %s (%s, %s) via %s', self.exit.ip, self.exit.country, self.exit.timezone, self.proxy.redacted if self.proxy else 'direct')

		# 3. Device persona, same device for a returning identity
		self.persona = persona_mod.generate(seed=ident.persona_seed if ident else None, exit_info=self.exit)
		log.info('persona %s', self.persona.summary())

		# 4. Launch the engine on a clean (or the identity's) profile
		if ident:
			profile_dir, ephemeral = ident.profile_dir(s), False
			profile_dir.mkdir(parents=True, exist_ok=True)
		else:
			profile_dir, ephemeral = Path(tempfile.mkdtemp(prefix='veil-profile-')), True
		self._launched = await self.engine.launch(self.persona, self.proxy, profile_dir, ephemeral)

		# 5. Attach browser-use without letting it re-shape the fingerprint
		profile = BrowserProfile(
			# headless=False + no_viewport stops browser-use from calling Emulation.setDeviceMetricsOverride,
			# which would contradict the persona's screen. The engine decides real headlessness.
			headless=False,
			no_viewport=True,
			keep_alive=True,
			proxy=ProxySettings(username=self.proxy.username, password=self.proxy.password) if self.proxy and self.proxy.has_auth else None,
		)
		self.browser = BrowserSession(cdp_url=self._launched.cdp_url, browser_profile=profile)
		await self.browser.start()

	async def probe_page(self) -> dict | None:
		"""{url, title, html} of the focused page, read in an isolated world invisible to page scripts."""
		if not self.browser:
			return None
		try:
			return json.loads(await evaluate_isolated(self.browser, PAGE_PROBE_JS))
		except Exception as e:
			log.debug('page probe failed: %s', e)
			return None

	async def check_block(self) -> BlockSignal | None:
		"""Classify the current page."""
		page = await self.probe_page()
		return classify(page['url'], page['title'], page['html']) if page else None

	def report(self, *, ok: bool, blocked: bool = False) -> None:
		self._outcome_reported = True
		self.proxy_source.report(self.proxy, ok=ok, blocked=blocked)

	async def close(self) -> None:
		if self.browser:
			try:
				await self.browser.stop()
			except Exception as e:
				log.debug('browser-use stop failed: %s', e)
			self.browser = None
		if self._launched:
			launched, self._launched = self._launched, None
			await asyncio.to_thread(launched.close)  # waits for the process to exit and the profile to be removed


async def evaluate_isolated(browser: BrowserSession, expression: str) -> object:
	"""Evaluate JS in a fresh isolated world of the focused page's main frame.

	Deliberately avoids Runtime.enable (the classic CDP automation tell) and the page's own
	JS realm (so prototype hooks set up by a detector never see the call).
	"""
	cdp = await browser.get_or_create_cdp_session()
	tree = await cdp.cdp_client.send.Page.getFrameTree(session_id=cdp.session_id)
	frame_id = tree['frameTree']['frame']['id']
	world = await cdp.cdp_client.send.Page.createIsolatedWorld(
		params={'frameId': frame_id, 'worldName': secrets.token_hex(4)},
		session_id=cdp.session_id,
	)
	res = await cdp.cdp_client.send.Runtime.evaluate(
		params={'expression': expression, 'contextId': world['executionContextId'], 'returnByValue': True, 'awaitPromise': True},
		session_id=cdp.session_id,
	)
	if 'exceptionDetails' in res:
		raise RuntimeError(res['exceptionDetails'].get('text', 'evaluation failed'))
	return res['result'].get('value')
