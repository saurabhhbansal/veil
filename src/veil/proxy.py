"""Proxy parsing and rotation.

Two sources are supported:

* ``ProxyPool``: a list of static proxies (proxies.txt). Each session takes one, and failures
  or blocks put that proxy on an exponentially growing cooldown.
* ``GatewayProxy``: a provider's rotating gateway. Most residential providers select the exit
  IP from a session token embedded in the username (e.g. ``user-session-abc123``). Put
  ``{session}`` where that token goes, and each veil session gets its own sticky exit IP.
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
	def acquire(self, session_key: str | None = None) -> Proxy | None: ...

	def report(self, proxy: Proxy | None, *, ok: bool, blocked: bool = False) -> None: ...


class NoProxy:
	"""Direct connection. Your own IP carries all the reputation, so expect blocks on hard targets."""

	def acquire(self, session_key: str | None = None) -> Proxy | None:
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

	def acquire(self, session_key: str | None = None) -> Proxy:
		with self._lock:
			ready = self.available()
			if not ready:
				# Everything is cooling down: take the one that recovers first rather than stall.
				ready = [min(self.proxies, key=lambda p: self._stats[p].cooldown_until)]
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

	def __post_init__(self) -> None:
		Proxy.parse(self.template.replace('{session}', 'probe'))  # validate early

	@property
	def sticky(self) -> bool:
		return '{session}' in self.template

	def acquire(self, session_key: str | None = None) -> Proxy:
		token = session_key or secrets.token_hex(6)
		return Proxy.parse(self.template.replace('{session}', token))

	def report(self, proxy: Proxy | None, *, ok: bool, blocked: bool = False) -> None:
		pass  # a fresh session token next time already means a fresh exit IP
