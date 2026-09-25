"""Exit-IP geolocation, so the persona's timezone and language match where the proxy exits.

A US timezone on an Indian IP (or vice versa) is one of the cheapest cross-checks a
detector can run, so every session looks up its real egress before building the persona.
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx

from veil.proxy import Proxy

# Primary language tags per country, most-specific first. Anything missing falls back to en-US.
COUNTRY_LANGUAGES: dict[str, str] = {
	'US': 'en-US,en',
	'GB': 'en-GB,en',
	'CA': 'en-CA,en,fr-CA',
	'AU': 'en-AU,en',
	'NZ': 'en-NZ,en',
	'IE': 'en-IE,en',
	'IN': 'en-IN,en,hi',
	'SG': 'en-SG,en',
	'ZA': 'en-ZA,en',
	'DE': 'de-DE,de,en',
	'AT': 'de-AT,de,en',
	'CH': 'de-CH,de,en',
	'FR': 'fr-FR,fr,en',
	'BE': 'fr-BE,fr,nl,en',
	'NL': 'nl-NL,nl,en',
	'ES': 'es-ES,es,en',
	'MX': 'es-MX,es,en',
	'AR': 'es-AR,es,en',
	'CO': 'es-CO,es,en',
	'IT': 'it-IT,it,en',
	'PT': 'pt-PT,pt,en',
	'BR': 'pt-BR,pt,en',
	'PL': 'pl-PL,pl,en',
	'SE': 'sv-SE,sv,en',
	'NO': 'nb-NO,nb,en',
	'DK': 'da-DK,da,en',
	'FI': 'fi-FI,fi,en',
	'TR': 'tr-TR,tr,en',
	'RU': 'ru-RU,ru,en',
	'UA': 'uk-UA,uk,en',
	'JP': 'ja-JP,ja,en',
	'KR': 'ko-KR,ko,en',
	'CN': 'zh-CN,zh,en',
	'TW': 'zh-TW,zh,en',
	'HK': 'zh-HK,zh,en',
	'ID': 'id-ID,id,en',
	'VN': 'vi-VN,vi,en',
	'TH': 'th-TH,th,en',
	'PH': 'en-PH,en,fil',
	'AE': 'en-AE,en,ar',
	'SA': 'ar-SA,ar,en',
	'EG': 'ar-EG,ar,en',
	'IL': 'he-IL,he,en',
}

DEFAULT_LANGUAGES = 'en-US,en'


@dataclass(frozen=True)
class ExitInfo:
	ip: str
	country: str  # ISO 3166-1 alpha-2
	timezone: str  # IANA, e.g. Asia/Kolkata
	city: str | None = None

	@property
	def languages(self) -> str:
		return languages_for_country(self.country)


def languages_for_country(country: str | None) -> str:
	return COUNTRY_LANGUAGES.get((country or '').upper(), DEFAULT_LANGUAGES)


async def lookup_exit(proxy: Proxy | None, timeout: float = 15.0) -> ExitInfo:
	"""Resolve the public IP, country and timezone seen by sites when routing through `proxy`."""
	errors: list[str] = []
	async with httpx.AsyncClient(proxy=proxy.url if proxy else None, timeout=timeout) as client:
		for fetch in (_ipinfo, _ipapi):
			try:
				return await fetch(client)
			except Exception as e:  # try the next provider
				errors.append(f'{fetch.__name__}: {type(e).__name__}: {e}')
	raise RuntimeError('exit-IP lookup failed via ' + (proxy.redacted if proxy else 'direct connection') + ': ' + '; '.join(errors))


async def _ipinfo(client: httpx.AsyncClient) -> ExitInfo:
	r = await client.get('https://ipinfo.io/json')
	r.raise_for_status()
	d = r.json()
	return ExitInfo(ip=d['ip'], country=d['country'], timezone=d['timezone'], city=d.get('city'))


async def _ipapi(client: httpx.AsyncClient) -> ExitInfo:
	# ip-api's free tier is http-only; that's fine for a lookup that carries no secrets.
	r = await client.get('http://ip-api.com/json/?fields=status,message,query,countryCode,timezone,city')
	r.raise_for_status()
	d = r.json()
	if d.get('status') != 'success':
		raise RuntimeError(d.get('message', 'ip-api lookup failed'))
	return ExitInfo(ip=d['query'], country=d['countryCode'], timezone=d['timezone'], city=d.get('city'))
