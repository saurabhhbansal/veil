"""`veil check`: launch one identity and verify that what a page sees matches the persona we asked for.

This catches the failure modes that matter most in practice: a surface the engine didn't
apply, a timezone that disagrees with the exit IP, browser-use resizing the viewport, or
navigator.webdriver leaking. Optionally screenshots public detector pages for a visual check.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path

from veil.config import Settings
from veil.session import StealthSession, evaluate_isolated

FINGERPRINT_JS = r"""
(async () => {
  const c = document.createElement('canvas');
  const gl = c.getContext('webgl');
  const dbg = gl && gl.getExtension('WEBGL_debug_renderer_info');
  let uaData = null;
  if (navigator.userAgentData) {
    uaData = await navigator.userAgentData.getHighEntropyValues(['platform', 'platformVersion', 'architecture', 'bitness']);
  }
  let webgpu = null;
  try {
    const a = navigator.gpu && await navigator.gpu.requestAdapter();
    if (a && a.info) webgpu = {vendor: a.info.vendor, architecture: a.info.architecture, description: a.info.description};
  } catch (e) {}
  return JSON.stringify({
    webdriver: navigator.webdriver,
    userAgent: navigator.userAgent,
    platform: navigator.platform,
    hardwareConcurrency: navigator.hardwareConcurrency,
    deviceMemory: navigator.deviceMemory,
    languages: navigator.languages,
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    screen: [screen.width, screen.height],
    avail: [screen.availWidth, screen.availHeight],
    outer: [outerWidth, outerHeight],
    inner: [innerWidth, innerHeight],
    webglVendor: dbg ? gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL) : null,
    webglRenderer: dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : null,
    uaData, webgpu,
  });
})()
"""

DETECTOR_SITES = {
	'sannysoft': 'https://bot.sannysoft.com/',
	'creepjs': 'https://abrahamjuliot.github.io/creepjs/',
	'browserscan': 'https://www.browserscan.net/',
	'rebrowser': 'https://bot-detector.rebrowser.net/',
}


# ICU (and so real Chrome) still reports some zones by their legacy IANA name.
TZ_ALIASES = {
	'Asia/Calcutta': 'Asia/Kolkata',
	'Asia/Saigon': 'Asia/Ho_Chi_Minh',
	'Asia/Katmandu': 'Asia/Kathmandu',
	'Asia/Rangoon': 'Asia/Yangon',
	'Europe/Kiev': 'Europe/Kyiv',
	'America/Buenos_Aires': 'America/Argentina/Buenos_Aires',
	'Atlantic/Faeroe': 'Atlantic/Faroe',
}


def same_tz(a: str | None, b: str | None) -> bool:
	return TZ_ALIASES.get(a or '', a) == TZ_ALIASES.get(b or '', b)


@dataclass
class Check:
	name: str
	ok: bool
	expected: object
	actual: object


def compare(persona, exit_info, fp: dict, applies_persona: bool) -> list[Check]:
	checks = [
		Check('navigator.webdriver is false', fp['webdriver'] is False, False, fp['webdriver']),
		Check('languages[0] matches persona', fp['languages'][:1] == persona.languages.split(',')[:1], persona.languages.split(',')[0], fp['languages'][:1]),
		Check('timezone matches persona', same_tz(fp['timezone'], persona.timezone), persona.timezone, fp['timezone']),
		Check('viewport fits inside screen', fp['inner'][0] <= fp['screen'][0] and fp['inner'][1] <= fp['screen'][1], f'<= {fp["screen"]}', fp['inner']),
	]
	if exit_info:
		checks.append(Check('timezone matches exit IP', same_tz(fp['timezone'], exit_info.timezone), f'{exit_info.timezone} ({exit_info.ip}, {exit_info.country})', fp['timezone']))
	if not applies_persona:
		return checks  # stock engines only get the environment-level checks
	ua = fp['uaData'] or {}
	checks += [
		Check('platform', fp['platform'] == 'Win32', 'Win32', fp['platform']),
		Check('UA says Windows', 'Windows NT 10.0; Win64; x64' in fp['userAgent'], 'Windows NT 10.0; Win64; x64', fp['userAgent']),
		Check('UA-CH platform', ua.get('platform') == 'Windows', 'Windows', ua.get('platform')),
		Check('UA-CH platformVersion', ua.get('platformVersion') == persona.platform_version, persona.platform_version, ua.get('platformVersion')),
		Check('hardwareConcurrency', fp['hardwareConcurrency'] == persona.hw_concurrency, persona.hw_concurrency, fp['hardwareConcurrency']),
		Check('deviceMemory', fp['deviceMemory'] == persona.device_memory, persona.device_memory, fp['deviceMemory']),
		Check('screen size', fp['screen'] == [persona.screen_width, persona.screen_height], [persona.screen_width, persona.screen_height], fp['screen']),
		Check('screen avail size', fp['avail'] == list(persona.window_size), list(persona.window_size), fp['avail']),
		Check('window outer size fits avail', fp['outer'][0] <= fp['avail'][0] and fp['outer'][1] <= fp['avail'][1], f'<= {fp["avail"]}', fp['outer']),
		Check('WebGL vendor', fp['webglVendor'] == persona.webgl_vendor, persona.webgl_vendor, fp['webglVendor']),
		Check('WebGL renderer', fp['webglRenderer'] == persona.webgl_renderer, persona.webgl_renderer, fp['webglRenderer']),
	]
	gpu = fp['webgpu'] or {}
	checks.append(Check('WebGPU adapter present', bool(gpu), 'an adapter (real Chrome on Windows always has one)', fp['webgpu']))
	if gpu:
		want = [persona.webgpu_vendor, persona.webgpu_architecture, persona.webgpu_description]
		got = [gpu['vendor'], gpu['architecture'], gpu['description']]
		checks.append(Check('WebGPU vendor/arch/description', got == want, want, got))
	return checks


async def run_check(settings: Settings, screenshots: bool = False, out_dir: Path = Path('reports')) -> bool:
	async with StealthSession(settings) as s:
		assert s.browser and s.persona
		print(f'engine   : {settings.engine}')
		print(f'proxy    : {s.proxy.redacted if s.proxy else "none (direct)"}')
		if s.exit:
			print(f'exit     : {s.exit.ip} {s.exit.country} {s.exit.city or ""} {s.exit.timezone}')
		print(f'persona  : {s.persona.summary()}\n')

		await s.browser.navigate_to('https://example.com/')  # a secure context, so UA-CH and WebGPU are exposed
		await asyncio.sleep(2)
		fp = json.loads(await evaluate_isolated(s.browser, FINGERPRINT_JS))
		checks = compare(s.persona, s.exit, fp, s._launched.applies_persona if s._launched else False)
		for c in checks:
			line = f'  {"PASS" if c.ok else "FAIL"}  {c.name}'
			print(line if c.ok else f'{line}\n          expected {c.expected!r}\n          got      {c.actual!r}')
		ok = all(c.ok for c in checks)
		print(f'\n{sum(c.ok for c in checks)}/{len(checks)} checks passed')

		if screenshots:
			run_dir = out_dir / time.strftime('%Y%m%d-%H%M%S')
			run_dir.mkdir(parents=True, exist_ok=True)
			(run_dir / 'fingerprint.json').write_text(json.dumps({'persona': s.persona.to_dict(), 'observed': fp}, indent=2))
			for name, url in DETECTOR_SITES.items():
				try:
					await s.browser.navigate_to(url)
					await asyncio.sleep(10)  # CreepJS in particular needs a while to finish its analysis
					await s.browser.take_screenshot(path=str(run_dir / f'{name}.png'), full_page=True)
					print(f'  screenshot {name:12} -> {run_dir / (name + ".png")}')
				except Exception as e:
					print(f'  screenshot {name:12} failed: {type(e).__name__}: {e}')
		s.report(ok=ok)
		return ok
