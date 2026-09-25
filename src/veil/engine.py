"""Browser engines: something that launches a browser for a persona + proxy and hands back a CDP URL.

* ``fortress``         native Fortress binary (Linux x64 / Windows x64), fetched by the tilion-fortress SDK
* ``fortress-docker``  the tilion/fortress image (any OS with Docker; amd64 only)
* ``cdp``              a browser you started yourself; veil can't apply persona or proxy to it
* ``chromium``         stock Chromium, NOT stealthy: only for exercising the pipeline on unsupported hosts
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import socket
import subprocess
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from veil.config import Settings
from veil.persona import Persona
from veil.proxy import Proxy

log = logging.getLogger('veil.engine')


@dataclass
class LaunchedBrowser:
	cdp_url: str
	applies_persona: bool
	_proc: subprocess.Popen | None = None
	_container: str | None = None
	_cleanup: list[Path] = field(default_factory=list)

	def close(self) -> None:
		if self._proc and self._proc.poll() is None:
			if os.name == 'nt':  # the .cmd launcher runs chrome as a child of cmd.exe; kill the whole tree
				subprocess.run(['taskkill', '/F', '/T', '/PID', str(self._proc.pid)], capture_output=True)
			else:
				self._proc.terminate()
				try:
					self._proc.wait(timeout=10)
				except subprocess.TimeoutExpired:
					self._proc.kill()
		self._proc = None
		if self._container:
			subprocess.run(['docker', 'rm', '-f', self._container], capture_output=True)
			self._container = None
		for p in self._cleanup:
			shutil.rmtree(p, ignore_errors=True)
		self._cleanup.clear()


class Engine(Protocol):
	name: str

	async def launch(self, persona: Persona, proxy: Proxy | None, profile_dir: Path, ephemeral: bool) -> LaunchedBrowser: ...


def _free_port() -> int:
	with socket.socket() as s:
		s.bind(('127.0.0.1', 0))
		return s.getsockname()[1]


async def _wait_for_cdp(port: int, proc: subprocess.Popen | None = None, timeout: float = 90.0) -> str:
	url = f'http://127.0.0.1:{port}/json/version'
	deadline = asyncio.get_running_loop().time() + timeout
	while asyncio.get_running_loop().time() < deadline:
		if proc is not None and proc.poll() is not None:
			raise RuntimeError(f'browser exited with code {proc.returncode} before CDP came up')
		try:
			data = await asyncio.to_thread(lambda: json.load(urllib.request.urlopen(url, timeout=2)))
			if data.get('webSocketDebuggerUrl'):
				return f'http://127.0.0.1:{port}'
		except Exception:
			pass
		await asyncio.sleep(0.5)
	raise TimeoutError(f'CDP endpoint on port {port} did not come up within {timeout:.0f}s')


def _common_flags(port: int, profile_dir: Path, proxy: Proxy | None, headless: bool) -> list[str]:
	flags = [
		f'--remote-debugging-port={port}',
		f'--user-data-dir={profile_dir}',
		'--no-first-run',
		'--no-default-browser-check',
	]
	if headless:
		flags += ['--headless=new', '--no-sandbox']  # same as the Fortress SDK's headless launch
	if proxy:
		flags.append(f'--proxy-server={proxy.server}')  # credentials are answered over CDP by browser-use
		# Send loopback through the proxy too, so pages probing 127.0.0.1 (a known way to spot an
		# open CDP port) reach the proxy's host instead of this machine.
		flags.append('--proxy-bypass-list=<-loopback>')
	return flags


class FortressEngine:
	name = 'fortress'

	def __init__(self, settings: Settings):
		self.settings = settings

	def launcher(self) -> Path:
		try:
			import tilion_fortress as tf  # private helpers: the SDK has no public "just give me the binary" call
		except ImportError as e:
			raise RuntimeError('tilion-fortress is not installed') from e
		plat = tf.resolve_platform()
		if plat not in ('linux-x64', 'win-x64'):
			raise RuntimeError(
				f'Fortress has no native build for this host ({plat or "unknown"}); '
				'use VEIL_ENGINE=fortress-docker (x64 hosts / Docker Desktop), VEIL_ENGINE=cdp, or VEIL_ENGINE=chromium for testing'
			)
		ch = tf._CHANNELS[self.settings.fortress_channel]
		return tf._download(plat, tf._host(ch['tag']), ch['tag'])

	async def launch(self, persona: Persona, proxy: Proxy | None, profile_dir: Path, ephemeral: bool) -> LaunchedBrowser:
		launcher = await asyncio.to_thread(self.launcher)
		port = _free_port()
		args = [str(launcher), *_common_flags(port, profile_dir, proxy, self.settings.headless), *persona.flags()]
		if str(launcher).lower().endswith('.cmd'):
			args = ['cmd', '/c', *args]
		env = {**os.environ, **persona.env()}
		proc = subprocess.Popen(args, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
		browser = LaunchedBrowser(cdp_url='', applies_persona=True, _proc=proc, _cleanup=[profile_dir] if ephemeral else [])
		try:
			browser.cdp_url = await _wait_for_cdp(port, proc)
		except BaseException:
			browser.close()
			raise
		return browser


class FortressDockerEngine:
	name = 'fortress-docker'
	images = {'stable': 'tilion/fortress:149.0.7827.232', 'latest': 'tilion/fortress:151.0.7908.0'}

	def __init__(self, settings: Settings):
		self.settings = settings

	async def launch(self, persona: Persona, proxy: Proxy | None, profile_dir: Path, ephemeral: bool) -> LaunchedBrowser:
		if not shutil.which('docker'):
			raise RuntimeError('docker is not installed')
		port = _free_port()
		name = f'veil-{os.getpid()}-{port}'
		env_args = [a for k, v in persona.env().items() for a in ('-e', f'{k}={v}')]
		# The entrypoint already runs headless with its own profile dir inside the (throwaway) container.
		flags = [*persona.flags()]
		if proxy:
			flags += [f'--proxy-server={proxy.server}', '--proxy-bypass-list=<-loopback>']
		cmd = ['docker', 'run', '-d', '--rm', '--platform', 'linux/amd64', '--name', name, '-p', f'127.0.0.1:{port}:9222', *env_args, self.images[self.settings.fortress_channel], *flags]
		await asyncio.to_thread(subprocess.run, cmd, check=True, capture_output=True)
		browser = LaunchedBrowser(cdp_url='', applies_persona=True, _container=name)
		try:
			browser.cdp_url = await _wait_for_cdp(port)
		except BaseException:
			browser.close()
			raise
		return browser


class RemoteCDPEngine:
	name = 'cdp'

	def __init__(self, settings: Settings):
		if not settings.cdp_url:
			raise RuntimeError('VEIL_ENGINE=cdp needs VEIL_CDP_URL')
		self.settings = settings

	async def launch(self, persona: Persona, proxy: Proxy | None, profile_dir: Path, ephemeral: bool) -> LaunchedBrowser:
		log.warning('engine=cdp: persona and proxy are NOT applied; the remote browser keeps whatever it was launched with')
		return LaunchedBrowser(cdp_url=self.settings.cdp_url or '', applies_persona=False)


class ChromiumEngine:
	name = 'chromium'

	def __init__(self, settings: Settings):
		self.settings = settings

	def binary(self) -> str:
		if self.settings.chromium_path:
			return self.settings.chromium_path
		for name in ('chromium', 'chromium-browser', 'google-chrome'):
			if found := shutil.which(name):
				return found
		pw = sorted((Path.home() / '.cache' / 'ms-playwright').glob('chromium-*/chrome-*/chrome'))
		if pw:
			return str(pw[-1])
		raise RuntimeError('no Chromium found; set VEIL_CHROMIUM_PATH or run `uvx playwright install chromium`')

	async def launch(self, persona: Persona, proxy: Proxy | None, profile_dir: Path, ephemeral: bool) -> LaunchedBrowser:
		log.warning('engine=chromium: stock Chromium, fingerprint is NOT spoofed (only timezone/language/window follow the persona)')
		port = _free_port()
		w, h = persona.window_size
		args = [self.binary(), *_common_flags(port, profile_dir, proxy, self.settings.headless), f'--lang={persona.languages.split(",")[0]}', f'--accept-lang={persona.languages}', f'--window-size={w},{h}']
		proc = subprocess.Popen(args, env={**os.environ, 'TZ': persona.timezone}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
		browser = LaunchedBrowser(cdp_url='', applies_persona=False, _proc=proc, _cleanup=[profile_dir] if ephemeral else [])
		try:
			browser.cdp_url = await _wait_for_cdp(port, proc)
		except BaseException:
			browser.close()
			raise
		return browser


ENGINES = {e.name: e for e in (FortressEngine, FortressDockerEngine, RemoteCDPEngine, ChromiumEngine)}


def make_engine(settings: Settings) -> Engine:
	if settings.engine not in ENGINES:
		raise ValueError(f'unknown VEIL_ENGINE {settings.engine!r}; choose from {sorted(ENGINES)}')
	return ENGINES[settings.engine](settings)
