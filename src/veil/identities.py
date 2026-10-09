"""Where sign-up details come from: the local profiles.csv, or your company's identity server over HTTP.

With VEIL_IDENTITY_API set, veil asks that server for a person each time an account run needs one, instead of
reading profiles.csv. The full contract (requests, responses, errors) is in docs/IDENTITY_API.md:

    POST {base}/v1/identities/assign      {site, url, purpose, country, exit_countries, exclude}  -> identity
    GET  {base}/v1/identities/{id}                                                              -> identity
    GET  {base}/v1/identities                                       -> {"identities": [{id, email}, ...]}  (optional)
    POST {base}/v1/identities/{id}/events  {site, action, status, note, browser_identity, password?}      (optional)
    GET  {base}/v1/identities/{id}/codes?channel=sms|email&site=&since=  -> {"code": "..."} or 404     (optional)
    GET  {base}/v1/identities/{id}/links?site=&since=                    -> {"link": "..."} or 404     (optional)

An identity is a JSON object with the profiles.csv columns (accounts.PROFILE_COLUMNS); `id` and `email` are required.
Every request carries `Authorization: Bearer <VEIL_IDENTITY_API_TOKEN>` when a token is set.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import threading
import time
from collections.abc import Awaitable, Callable, Collection
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import quote

import httpx

from veil import accounts

if TYPE_CHECKING:
	from veil.config import Settings
	from veil.proxy import ProxySource

log = logging.getLogger('veil.identities')

API_VERSION = 'v1'
CODE_CHANNELS = ('sms', 'email')
CODE_WAIT_SECONDS = 90  # how long get_verification_code keeps asking before giving up
CODE_POLL_SECONDS = 5


class IdentityAPIError(ValueError):
	"""The identity server said no, or couldn't be reached. A ValueError, so callers that show ValueErrors show this."""

	def __init__(self, message: str, status: int | None = None):
		super().__init__(message)
		self.status = status  # the HTTP status, when the server answered


class ProfileSource(Protocol):
	"""Who veil signs up / logs in as. get() raises ValueError for an id it doesn't know."""

	remote: bool

	def get(self, profile_id: str) -> dict[str, str]: ...

	def assign(self, site: str, purpose: str, exclude: Collection[str] = ()) -> dict[str, str]: ...

	def listing(self) -> list[dict[str, str]]: ...

	def report(self, row: accounts.Signup, action: str, *, background: bool = True) -> None: ...

	def code_fetcher(self, profile_id: str, site: str) -> Callable[[str], Awaitable[str | None]] | None: ...


class LocalProfiles:
	"""The rows of profiles.csv (or any list of profiles). New sign-ups take a random profile not already in use."""

	remote = False

	def __init__(self, profiles: list[dict[str, str]]):
		self.profiles = profiles
		self.by_id = {p['id']: p for p in profiles}

	def get(self, profile_id: str) -> dict[str, str]:
		if profile_id not in self.by_id:
			raise ValueError(f'no profile with id {profile_id!r} (have: {", ".join(self.by_id)})')
		return self.by_id[profile_id]

	def assign(self, site: str, purpose: str, exclude: Collection[str] = ()) -> dict[str, str]:
		if purpose == 'login':  # logging in with no profile named only works when there's no choice to make
			if len(self.profiles) != 1:
				raise ValueError(f'say which profile to log in to {site} with (have: {", ".join(self.by_id) or "none"})')
			return self.profiles[0]
		free = [p for p in self.profiles if p['id'] not in exclude]
		if not free:
			raise ValueError(f'every profile is already taken for {site}')
		return secrets.choice(free)

	def listing(self) -> list[dict[str, str]]:
		return self.profiles

	def report(self, row: accounts.Signup, action: str, *, background: bool = True) -> None:
		pass  # signups.csv is the record

	def code_fetcher(self, profile_id: str, site: str) -> Callable[[str], Awaitable[str | None]] | None:
		return None  # codes go to the person, who types them in (request_human_help)


class IdentityAPI:
	"""Client for the company identity server (docs/IDENTITY_API.md). Blocking calls: run them off the event loop."""

	remote = True

	def __init__(
		self,
		base_url: str,
		token: str | None = None,
		*,
		timeout: float = 15,
		country: str | None = None,
		exit_countries: list[str] | None = None,
		send_passwords: bool = False,
		transport: httpx.BaseTransport | None = None,
	):
		self.base_url = base_url.rstrip('/')
		self.country = country.upper() if country else None
		self.exit_countries = exit_countries
		self.send_passwords = send_passwords
		headers = {'Accept': 'application/json', 'User-Agent': f'veil/{_version()}'}
		if token:
			headers['Authorization'] = f'Bearer {token}'
		self._client = httpx.Client(base_url=f'{self.base_url}/{API_VERSION}', headers=headers, timeout=timeout, transport=transport)

	# --- the calls ---------------------------------------------------------------------------------

	def _call(self, method: str, path: str, *, json: Any = None, params: dict[str, Any] | None = None) -> httpx.Response:
		try:
			res = self._client.request(method, path, json=json, params=params)
		except httpx.HTTPError as e:
			raise IdentityAPIError(f'identity server unreachable at {self.base_url} ({type(e).__name__}: {e})') from e
		if res.status_code >= 400:
			raise IdentityAPIError(f'identity server: {res.status_code} {_error_text(res)}', res.status_code)
		return res

	def assign(self, site: str, purpose: str, exclude: Collection[str] = ()) -> dict[str, str]:
		"""A person to sign up (or log in) as on `site`. exclude: ids this request already uses there."""
		body = {
			'site': site, 'url': accounts.normalize_url(site), 'purpose': purpose,
			'country': self.country, 'exit_countries': self.exit_countries, 'exclude': sorted(exclude),
		}
		return _identity(_json(self._call('POST', '/identities/assign', json=body)), f'assign for {site}')

	def get(self, profile_id: str) -> dict[str, str]:
		return _identity(_json(self._call('GET', f'/identities/{quote(profile_id, safe="")}')), f'identity {profile_id!r}')

	def listing(self) -> list[dict[str, str]]:
		"""Who the server can hand out (id and email only), for the chat planner. Optional: [] when not offered."""
		try:
			data = _json(self._call('GET', '/identities'))
		except IdentityAPIError as e:
			log.info('no identity listing (%s)', e)
			return []
		rows = data.get('identities') if isinstance(data, dict) else None
		out = []
		for r in rows if isinstance(rows, list) else []:
			if isinstance(r, dict) and r.get('id') and r.get('email'):
				out.append({'id': str(r['id']), 'email': str(r['email'])})
		return out

	def report(self, row: accounts.Signup, action: str, *, background: bool = True) -> None:
		"""Tell the server how a run ended. Best effort: a failure is logged, never raised."""
		body = {
			'site': row.site, 'action': action, 'status': row.status, 'note': row.note, 'email': row.email,
			'browser_identity': row.identity, 'at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
		}
		if self.send_passwords:
			body['password'] = row.password

		def send() -> None:
			try:
				self._call('POST', f'/identities/{quote(row.profile_id, safe="")}/events', json=body)
			except IdentityAPIError as e:
				log.warning('could not report %s %s for %s: %s', action, row.status, row.profile_id, e)

		if background:
			threading.Thread(target=send, name='veil-identity-report', daemon=True).start()
		else:
			send()

	def _latest(self, kind: str, profile_id: str, site: str, since: float, channel: str | None = None) -> str | None:
		"""kind 'code' or 'link': the newest one the person received after `since` (unix seconds), or None yet."""
		params: dict[str, Any] = {'site': site, 'since': int(since)}
		if channel:
			params['channel'] = channel
		try:
			res = self._call('GET', f'/identities/{quote(profile_id, safe="")}/{kind}s', params=params)
		except IdentityAPIError as e:
			if e.status == 404:
				return None
			raise
		if res.status_code == 204:
			return None
		data = _json(res)
		value = str(data.get(kind) or '').strip() if isinstance(data, dict) else ''
		return value or None

	def code(self, profile_id: str, channel: str, site: str, since: float) -> str | None:
		"""The newest one-time code sent to the person's phone ('sms') or mailbox ('email') after `since`."""
		return self._latest('code', profile_id, site, since, channel)

	def link(self, profile_id: str, site: str, since: float) -> str | None:
		"""The newest verification / confirmation link emailed to the person after `since`."""
		return self._latest('link', profile_id, site, since)

	def code_fetcher(self, profile_id: str, site: str) -> Callable[[str], Awaitable[str | None]]:
		"""For the agent's get_verification_code / get_verification_link actions: fetch('sms' | 'email' | 'link') waits
		up to CODE_WAIT_SECONDS for one that arrived since this run started."""
		since = time.time() - 60  # the form may have been sent just before this run's identity was assigned

		async def fetch(channel: str) -> str | None:
			if channel == 'link':
				lookup = lambda: self.link(profile_id, site, since)  # noqa: E731
			else:
				ch = channel if channel in CODE_CHANNELS else 'sms'
				lookup = lambda: self.code(profile_id, ch, site, since)  # noqa: E731
			deadline = time.monotonic() + CODE_WAIT_SECONDS
			while True:
				found = await asyncio.to_thread(lookup)
				if found or time.monotonic() >= deadline:
					return found
				await asyncio.sleep(CODE_POLL_SECONDS)

		return fetch


def _version() -> str:
	from importlib.metadata import PackageNotFoundError, version

	try:
		return version('veil')
	except PackageNotFoundError:
		return 'dev'


def _json(res: httpx.Response) -> Any:
	try:
		return res.json()
	except ValueError as e:
		raise IdentityAPIError(f'identity server sent something that is not JSON ({res.status_code}, {res.text[:120]!r})') from e


def _error_text(res: httpx.Response) -> str:
	try:
		data = res.json()
		if isinstance(data, dict) and (data.get('error') or data.get('detail')):
			return str(data.get('error') or data.get('detail'))[:300]
	except ValueError:
		pass
	return res.text[:300] or res.reason_phrase


def _identity(data: Any, what: str) -> dict[str, str]:
	"""The server's identity object -> a profile row (the profiles.csv columns as strings), validated the same way."""
	if not isinstance(data, dict):
		raise IdentityAPIError(f'identity server: {what}: expected a JSON object, got {type(data).__name__}')
	row = {c: '' if data.get(c) is None else str(data[c]).strip() for c in accounts.PROFILE_COLUMNS}
	try:
		(profile,) = accounts.validate_profiles([row], where=lambda n: f'identity server ({what})')
	except ValueError as e:
		raise IdentityAPIError(str(e)) from e
	return profile


def exit_countries(proxy_source: ProxySource | None) -> list[str] | None:
	"""The countries veil can exit from, for the server to match: a proxy list's tested countries. None: any / unknown."""
	from veil.proxy import ProxyPool

	if isinstance(proxy_source, ProxyPool) and proxy_source.countries:
		return sorted(set(proxy_source.countries.values()))
	return None


def profile_source(settings: Settings, proxy_source: ProxySource | None = None) -> ProfileSource:
	"""The identity server when VEIL_IDENTITY_API is set, else profiles.csv (raises ValueError if that's missing or empty)."""
	if settings.identity_api:
		return IdentityAPI(
			settings.identity_api, settings.identity_api_token, timeout=settings.identity_api_timeout,
			country=settings.proxy_country, exit_countries=exit_countries(proxy_source), send_passwords=settings.identity_api_send_passwords,
		)
	return LocalProfiles(accounts.load_profiles(accounts.profiles_path(settings)))
