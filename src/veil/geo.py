"""Exit-IP geolocation, so the persona's timezone and language match where the proxy exits.

A US timezone on an Indian IP (or vice versa) is one of the cheapest cross-checks a
detector can run, so every session looks up its real egress before building the persona.
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx

from veil.proxy import Proxy

# The locale a typical browser in each country runs. Anything missing falls back to en-US.
#
# Only the locale and its base language go into navigator.languages. Chromium 149 reduces the
# Accept-Language header to exactly that pair (en-IN,en,hi is sent as `en-IN,en;q=0.9`), so any
# extra language would make navigator.languages disagree with the header (BrowserScan flags it).
COUNTRY_LOCALES: dict[str, str] = {
	'US': 'en-US',
	'GB': 'en-GB',
	'CA': 'en-CA',
	'AU': 'en-AU',
	'NZ': 'en-NZ',
	'IE': 'en-IE',
	'IN': 'en-IN',
	'SG': 'en-SG',
	'ZA': 'en-ZA',
	'DE': 'de-DE',
	'AT': 'de-AT',
	'CH': 'de-CH',
	'FR': 'fr-FR',
	'BE': 'fr-BE',
	'NL': 'nl-NL',
	'ES': 'es-ES',
	'MX': 'es-MX',
	'AR': 'es-AR',
	'CO': 'es-CO',
	'IT': 'it-IT',
	'PT': 'pt-PT',
	'BR': 'pt-BR',
	'PL': 'pl-PL',
	'SE': 'sv-SE',
	'NO': 'nb-NO',
	'DK': 'da-DK',
	'FI': 'fi-FI',
	'TR': 'tr-TR',
	'RU': 'ru-RU',
	'UA': 'uk-UA',
	'JP': 'ja-JP',
	'KR': 'ko-KR',
	'CN': 'zh-CN',
	'TW': 'zh-TW',
	'HK': 'zh-HK',
	'ID': 'id-ID',
	'VN': 'vi-VN',
	'TH': 'th-TH',
	'PH': 'en-PH',
	'AE': 'en-AE',
	'SA': 'ar-SA',
	'EG': 'ar-EG',
	'IL': 'he-IL',
}

DEFAULT_LANGUAGES = 'en-US,en'

# Country names people write in a profile's country field (ISO codes are accepted as they are).
_COUNTRY_NAMES: dict[str, str] = {
	'united states': 'US', 'united states of america': 'US', 'usa': 'US', 'united kingdom': 'GB', 'uk': 'GB', 'great britain': 'GB',
	'england': 'GB', 'canada': 'CA', 'australia': 'AU', 'new zealand': 'NZ', 'ireland': 'IE', 'india': 'IN', 'singapore': 'SG',
	'south africa': 'ZA', 'germany': 'DE', 'austria': 'AT', 'switzerland': 'CH', 'france': 'FR', 'belgium': 'BE', 'netherlands': 'NL',
	'the netherlands': 'NL', 'spain': 'ES', 'mexico': 'MX', 'argentina': 'AR', 'colombia': 'CO', 'italy': 'IT', 'portugal': 'PT',
	'brazil': 'BR', 'poland': 'PL', 'sweden': 'SE', 'norway': 'NO', 'denmark': 'DK', 'finland': 'FI', 'turkey': 'TR', 'russia': 'RU',
	'ukraine': 'UA', 'japan': 'JP', 'south korea': 'KR', 'korea': 'KR', 'china': 'CN', 'taiwan': 'TW', 'hong kong': 'HK',
	'indonesia': 'ID', 'vietnam': 'VN', 'thailand': 'TH', 'philippines': 'PH', 'united arab emirates': 'AE', 'uae': 'AE',
	'saudi arabia': 'SA', 'egypt': 'EG', 'israel': 'IL',
}


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
	locale = COUNTRY_LOCALES.get((country or '').upper())
	return f'{locale},{locale.split("-")[0]}' if locale else DEFAULT_LANGUAGES


def country_code(value: str | None) -> str | None:
	"""A profile's country ('India', 'in', 'IN') -> ISO code 'IN'; None when blank or unknown."""
	v = (value or '').strip()
	if len(v) == 2 and v.isalpha():
		return v.upper()
	return _COUNTRY_NAMES.get(v.lower()) if v else None


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
