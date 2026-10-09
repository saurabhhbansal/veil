"""Proxy parsing and rotation.

Two sources are supported:

* ``ProxyPool``: a list of static proxies (proxies.txt). Each session takes one, and failures
  or blocks put that proxy on an exponentially growing cooldown.
* ``GatewayProxy``: a provider's rotating gateway. Most residential providers select the exit
  IP from a session token embedded in the username (e.g. ``user-session-abc123``). Put
  ``{session}`` where that token goes, and each veil session gets its own sticky exit IP.
  Put ``{country}`` (or ``{COUNTRY}``) where the provider takes a country code, and each
  session exits in the country it asks for.

Both take an optional ISO country code on ``acquire``: a session for a person in Germany gets a
German exit when the source has one. A list learns each proxy's country from its last test.
"""
from __future__ import annotations

import random
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from urllib.parse import quote, unquote, urlsplit

CHROMIUM_SCHEMES = {'http', 'https', 'socks4', 'socks5'}


@dataclass(frozen=True)
class Proxy:
	scheme: str
	host: str
	port: int
	username: str | None = None
	password: str | None = None

	@property
	def _hostport(self) -> str:
		return f'[{self.host}]:{self.port}' if ':' in self.host else f'{self.host}:{self.port}'  # IPv6 needs brackets

	@property
	def server(self) -> str:
		"""Credential-free form for Chromium's --proxy-server (it cannot take inline credentials)."""
		return f'{self.scheme}://{self._hostport}'

	@property
	def url(self) -> str:
		if self.username:
			return f'{self.scheme}://{quote(self.username, safe="")}:{quote(self.password or "", safe="")}@{self._hostport}'
		return self.server

	@property
	def redacted(self) -> str:
		return f'{self.scheme}://{self.username + ":***@" if self.username else ""}{self._hostport}'

	@property
	def has_auth(self) -> bool:
		return bool(self.username)

	@classmethod
	def parse(cls, raw: str, default_scheme: str = 'http') -> Proxy:
		"""Accepts ``scheme://user:pass@host:port``, ``user:pass@host:port``, ``host:port`` and ``host:port:user:pass``."""
		s = raw.strip()
		if not s:
			raise ValueError('empty proxy string')
		if '://' not in s:
			parts = s.split(':', 3)  # the password may itself contain ':'
			if '@' not in s and len(parts) == 4 and parts[1].isdigit():  # host:port:user:pass, the usual provider export format
				host, port, user, pw = parts
				s = f'{default_scheme}://{quote(user, safe="")}:{quote(pw, safe="")}@{host}:{port}'
			else:
				s = f'{default_scheme}://{s}'
		u = urlsplit(s)
		scheme = u.scheme.lower()
		if scheme not in CHROMIUM_SCHEMES:
			raise ValueError(f'unsupported proxy scheme {scheme!r} in {raw!r}; use one of {sorted(CHROMIUM_SCHEMES)}')
		if not u.hostname or not u.port:
			raise ValueError(f'proxy needs host and port: {raw!r}')
		proxy = cls(
			scheme=scheme,
			host=u.hostname,
			port=u.port,
			username=unquote(u.username) if u.username else None,
			password=unquote(u.password) if u.password else None,
		)
		if proxy.has_auth and scheme.startswith('socks'):
			# Chromium has no SOCKS authentication at all; the connection would silently fail.
			raise ValueError(f'Chromium cannot authenticate SOCKS proxies ({proxy.redacted}); use the provider\'s HTTP port instead')
		return proxy


class ProxySource(Protocol):
	def acquire(self, session_key: str | None = None, country: str | None = None) -> Proxy | None: ...

	def report(self, proxy: Proxy | None, *, ok: bool, blocked: bool = False) -> None: ...


class NoProxy:
	"""Direct connection. Your own IP carries all the reputation, so expect blocks on hard targets."""

	def acquire(self, session_key: str | None = None, country: str | None = None) -> Proxy | None:
		return None

	def report(self, proxy: Proxy | None, *, ok: bool, blocked: bool = False) -> None:
		pass


@dataclass
class _Stats:
	uses: int = 0
	successes: int = 0
	failures: int = 0
	consecutive_failures: int = 0
	cooldown_until: float = 0.0


@dataclass
class ProxyPool:
	proxies: list[Proxy]
	strategy: str = 'round_robin'  # round_robin | random | least_used
	base_cooldown: float = 60.0
	max_cooldown: float = 3600.0
	countries: dict[Proxy, str] = field(default_factory=dict)  # ISO code of each proxy's exit, where known
	_stats: dict[Proxy, _Stats] = field(default_factory=dict, init=False)
	_cursor: int = field(default=0, init=False)
	_lock: threading.Lock = field(default_factory=threading.Lock, init=False)

	def __post_init__(self) -> None:
		if not self.proxies:
			raise ValueError('proxy pool is empty')
		if self.strategy not in {'round_robin', 'random', 'least_used'}:
			raise ValueError(f'unknown rotation strategy {self.strategy!r}')
		self._stats = {p: _Stats() for p in self.proxies}

	@classmethod
	def from_file(cls, path: str | Path, **kw) -> ProxyPool:
		lines = Path(path).read_text().splitlines()
		proxies = [Proxy.parse(line) for line in lines if line.strip() and not line.lstrip().startswith('#')]
		return cls(proxies=proxies, **kw)

	def available(self, now: float | None = None) -> list[Proxy]:
		now = time.monotonic() if now is None else now
		return [p for p in self.proxies if self._stats[p].cooldown_until <= now]

	def acquire(self, session_key: str | None = None, country: str | None = None) -> Proxy:
		"""The next proxy; with `country`, one that exits there if the list has any (otherwise any proxy)."""
		with self._lock:
			pool = self.proxies
			if country:
				pool = [p for p in self.proxies if self.countries.get(p) == country.upper()] or self.proxies
			ready = [p for p in self.available() if p in pool]
			if not ready:
				# Everything is cooling down: take the one that recovers first rather than stall.
				ready = [min(pool, key=lambda p: self._stats[p].cooldown_until)]
			if self.strategy == 'random':
				chosen = random.choice(ready)
			elif self.strategy == 'least_used':
				chosen = min(ready, key=lambda p: self._stats[p].uses)
			else:
				chosen = ready[self._cursor % len(ready)]
				self._cursor += 1
			self._stats[chosen].uses += 1
			return chosen

	def report(self, proxy: Proxy | None, *, ok: bool, blocked: bool = False) -> None:
		if proxy is None or proxy not in self._stats:
			return
		with self._lock:
			st = self._stats[proxy]
			if ok:
				st.successes += 1
				st.consecutive_failures = 0
				return
			st.failures += 1
			st.consecutive_failures += 1
			# A block says the IP is burned for this target; plain errors may be transient.
			exponent = st.consecutive_failures + (1 if blocked else -1)
			cooldown = min(self.max_cooldown, self.base_cooldown * 2 ** max(exponent, 0))
			st.cooldown_until = time.monotonic() + cooldown

	def stats(self) -> dict[str, _Stats]:
		return {p.redacted: s for p, s in self._stats.items()}


@dataclass
class GatewayProxy:
	"""Provider gateway with a ``{session}`` placeholder, e.g. ``http://user-session-{session}:pw@gw.example.com:823``."""

	template: str
	default_country: str = 'us'  # for {country} when a session asks for none

	def __post_init__(self) -> None:
		self.acquire('probe')  # validate early

	@property
	def sticky(self) -> bool:
		return '{session}' in self.template

	@property
	def by_country(self) -> bool:
		return '{country}' in self.template or '{COUNTRY}' in self.template

	def acquire(self, session_key: str | None = None, country: str | None = None) -> Proxy:
		token = session_key or secrets.token_hex(6)
		cc = (country or self.default_country).strip()
		url = self.template.replace('{session}', token).replace('{country}', cc.lower()).replace('{COUNTRY}', cc.upper())
		return Proxy.parse(url)

	def report(self, proxy: Proxy | None, *, ok: bool, blocked: bool = False) -> None:
		pass  # a fresh session token next time already means a fresh exit IP


# --- the proxy list file, as the web UI edits it -------------------------------------------------


def proxy_id(proxy: Proxy) -> str:
	"""Stable id for a proxy that doesn't reveal its credentials."""
	import hashlib

	return hashlib.sha1(proxy.url.encode()).hexdigest()[:12]


def read_list(path: Path) -> list[Proxy]:
	"""The proxies in a list file; lines that don't parse are skipped (the UI shows only what veil can use)."""
	if not path.exists():
		return []
	out: list[Proxy] = []
	for line in path.read_text(encoding='utf-8').splitlines():
		if line.strip() and not line.lstrip().startswith('#'):
			try:
				out.append(Proxy.parse(line))
			except ValueError:
				continue
	return out


def write_list(path: Path, proxies: list[Proxy]) -> None:
	"""Rewrite the list file (one proxy URL per line), replacing it in one step."""
	path.parent.mkdir(parents=True, exist_ok=True)
	tmp = path.with_suffix('.tmp')
	body = '# veil proxy list: one per line (scheme://user:pass@host:port). Edited from the veil app.\n'
	tmp.write_text(body + ''.join(p.url + '\n' for p in proxies), encoding='utf-8')
	tmp.replace(path)


def load_countries(status_file: Path, proxies: list[Proxy]) -> dict[Proxy, str]:
	"""Each proxy's exit country from its last successful test (the app's proxy-status.json)."""
	import json

	try:
		data = json.loads(status_file.read_text(encoding='utf-8'))
	except (OSError, ValueError):
		return {}
	out = {}
	for p in proxies:
		st = data.get(proxy_id(p)) if isinstance(data, dict) else None
		if isinstance(st, dict) and st.get('ok') and st.get('country'):
			out[p] = str(st['country']).upper()
	return out


def parse_many(text: str) -> tuple[list[Proxy], list[str]]:
	"""Pasted lines -> (proxies, one error per bad line). Blank lines and # comments are ignored."""
	ok, errors = [], []
	for n, line in enumerate((text or '').splitlines(), 1):
		if not line.strip() or line.lstrip().startswith('#'):
			continue
		try:
			ok.append(Proxy.parse(line))
		except ValueError as e:
			errors.append(f'line {n}: {e}')
	return ok, errors
