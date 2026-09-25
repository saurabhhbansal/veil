"""Coherent device personas, rendered as Fortress `--uxr-*` switches.

The open Fortress launcher ships a single fixed persona (same GPU, seeds and screen for
every user), which makes all its users one linkable fingerprint. This module picks a whole
device (GPU -> machine class -> CPU/RAM/screen, plus Windows version and noise seeds) from
data/devices.json so that each identity is distinct but internally consistent. Surfaces are
never randomised independently: a jointly impossible device is itself a detection signal.

Timezone and languages come from the proxy's exit IP (see geo.py), not from the device.
"""
from __future__ import annotations

import json
import random
from dataclasses import asdict, dataclass
from functools import cache
from importlib import resources
from typing import Any, TypeVar

from veil.geo import DEFAULT_LANGUAGES, ExitInfo

T = TypeVar('T')


@cache
def device_data() -> dict[str, Any]:
	return json.loads(resources.files('veil').joinpath('data/devices.json').read_text())


@dataclass
class Persona:
	seed: int
	gpu_id: str
	machine_class: str
	windows: str
	platform_version: str
	webgl_vendor: str
	webgl_renderer: str
	webgpu_vendor: str
	webgpu_architecture: str
	webgpu_description: str
	hw_concurrency: int
	device_memory: int
	screen_width: int
	screen_height: int
	canvas_seed: int
	audio_seed: int
	timezone: str = 'America/New_York'
	languages: str = DEFAULT_LANGUAGES

	def with_geo(self, exit_info: ExitInfo | None) -> Persona:
		"""Return a copy whose timezone/languages match the proxy exit (device stays the same)."""
		if exit_info is None:
			return self
		return Persona(**{**asdict(self), 'timezone': exit_info.timezone, 'languages': exit_info.languages})

	@property
	def window_size(self) -> tuple[int, int]:
		# A maximised window sits inside the screen minus the taskbar; innerHeight > availHeight is a tell.
		return self.screen_width, self.screen_height - device_data()['taskbar_height']

	def flags(self) -> list[str]:
		"""Chromium switches for a Fortress launch with TILION_NO_DEFAULTS=1."""
		w, h = self.window_size
		return [
			'--uxr-platform=Win32',
			'--uxr-ua-platform=Windows',
			'--uxr-ua-os=Windows NT 10.0; Win64; x64',
			'--uxr-ua-arch=x86',
			'--uxr-ua-bitness=64',
			f'--uxr-ua-platform-version={self.platform_version}',
			'--uxr-ua-brand=Google Chrome',
			f'--uxr-hw-concurrency={self.hw_concurrency}',
			f'--uxr-device-memory={self.device_memory}',
			f'--uxr-webgl-vendor={self.webgl_vendor}',
			f'--uxr-webgl-renderer={self.webgl_renderer}',
			'--uxr-webgl-fullparams=1',
			f'--uxr-canvas-seed={self.canvas_seed}',
			f'--uxr-audio-seed={self.audio_seed}',
			f'--uxr-timezone={self.timezone}',
			f'--uxr-languages={self.languages}',
			f'--accept-lang={self.languages}',
			f'--uxr-screen-width={self.screen_width}',
			f'--uxr-screen-height={self.screen_height}',
			'--uxr-webrtc-policy=disable_non_proxied_udp',
			# The launcher hardcodes an RTX 3060 WebGPU adapter outside its defaults block;
			# Chromium keeps the last value of a repeated switch, so these override it.
			f'--uxr-webgpu-vendor={self.webgpu_vendor}',
			f'--uxr-webgpu-architecture={self.webgpu_architecture}',
			f'--uxr-webgpu-description={self.webgpu_description}',
			f'--window-size={w},{h}',
		]

	def env(self) -> dict[str, str]:
		"""Environment for the launcher: skip its fixed persona, and set the process TZ from the first tick."""
		return {'TILION_NO_DEFAULTS': '1', 'TILION_TZ': self.timezone, 'TILION_LANG': self.languages, 'TZ': self.timezone}

	def to_dict(self) -> dict[str, Any]:
		return asdict(self)

	@classmethod
	def from_dict(cls, d: dict[str, Any]) -> Persona:
		return cls(**d)

	def summary(self) -> str:
		return (
			f'{self.windows} · {self.webgpu_description} · {self.hw_concurrency} cores/{self.device_memory}GB · '
			f'{self.screen_width}x{self.screen_height} · {self.timezone} · {self.languages}'
		)


def _weighted(rng: random.Random, pairs: list[tuple[T, int]] | list[list[Any]]) -> T:
	values = [p[0] for p in pairs]
	weights = [p[1] for p in pairs]
	return rng.choices(values, weights=weights, k=1)[0]


def generate(seed: int | None = None, exit_info: ExitInfo | None = None) -> Persona:
	"""Build a persona. The same seed always yields the same device (sticky identity)."""
	if seed is None:
		seed = random.SystemRandom().randrange(1, 2**31)
	rng = random.Random(seed)
	data = device_data()

	gpu = rng.choices(data['gpus'], weights=[g['weight'] for g in data['gpus']], k=1)[0]
	machine = data['machine_classes'][gpu['class']]
	win = rng.choices(data['windows_versions'], weights=[v['weight'] for v in data['windows_versions']], k=1)[0]
	screen_w, screen_h = _weighted(rng, machine['screens'])

	persona = Persona(
		seed=seed,
		gpu_id=gpu['id'],
		machine_class=gpu['class'],
		windows=win['label'],
		platform_version=win['platform_version'],
		webgl_vendor=gpu['vendor'],
		webgl_renderer=gpu['renderer'],
		webgpu_vendor=gpu['webgpu_vendor'],
		webgpu_architecture=gpu['webgpu_architecture'],
		webgpu_description=gpu['webgpu_description'],
		hw_concurrency=_weighted(rng, machine['hw_concurrency']),
		device_memory=_weighted(rng, machine['device_memory']),
		screen_width=screen_w,
		screen_height=screen_h,
		canvas_seed=rng.randrange(100_000, 999_999_999),
		audio_seed=rng.randrange(100_000, 999_999_999),
	)
	return persona.with_geo(exit_info)
