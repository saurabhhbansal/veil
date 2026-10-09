"""Settings, read from the environment (and a .env file in the working directory)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


def _opt(name: str) -> str | None:
	v = os.environ.get(name, '').strip()
	return v or None


def _bool(name: str, default: bool) -> bool:
	v = _opt(name)
	return default if v is None else v.lower() in {'1', 'true', 'yes', 'on'}


def _float(name: str) -> float | None:
	v = _opt(name)
	return None if v is None else float(v)


@dataclass
class Settings:
	# LLM
	llm_provider: str = 'openai'  # openai | groq | anthropic | google | openai-compatible
	llm_model: str = 'gpt-6-luna'
	llm_api_key: str | None = None
	llm_base_url: str | None = None
	llm_temperature: float | None = None  # None = provider default (reasoning models reject custom values)
	llm_effort: str | None = None  # reasoning effort (llm.EFFORTS[provider]); None = the provider's default

	# Browser engine
	engine: str = 'fortress'  # fortress | fortress-docker | cdp | chromium
	fortress_channel: str = 'stable'  # stable (Chromium 149, blends in best) | latest
	cdp_url: str | None = None  # engine=cdp: an already-running browser you manage yourself
	chromium_path: str | None = None  # engine=chromium: stock binary, for pipeline testing only
	headless: bool = True

	# Network identity
	proxy_file: Path | None = None
	proxy_gateway: str | None = None
	proxy_strategy: str = 'round_robin'
	geo_match: bool = True

	# Orchestration
	max_attempts: int = 3
	max_steps: int = 40
	max_parallel: int = 3  # browsers a split request may run at once (each is a full Fortress instance)
	data_dir: Path = field(default_factory=lambda: Path.home() / '.veil')
	profiles_file: Path | None = None  # your own sign-up details; default <data_dir>/accounts/profiles.csv
	env_file: Path | None = None  # the .env these settings came from; the web UI writes keys and proxies back to it

	@property
	def accounts_dir(self) -> Path:
		return self.data_dir / 'accounts'

	@classmethod
	def from_env(cls, env_file: str | Path | None = '.env') -> Settings:
		if env_file and Path(env_file).exists():
			load_dotenv(env_file, override=False)
		# browser-use phones home by default; a stealth tool shouldn't.
		os.environ.setdefault('ANONYMIZED_TELEMETRY', 'false')
		os.environ.setdefault('BROWSER_USE_CLOUD_SYNC', 'false')

		s = cls()
		s.env_file = Path(env_file).resolve() if env_file else None
		s.llm_provider = _opt('VEIL_LLM_PROVIDER') or s.llm_provider
		s.llm_model = _opt('VEIL_LLM_MODEL') or s.llm_model
		s.llm_api_key = _opt('VEIL_LLM_API_KEY')
		s.llm_base_url = _opt('VEIL_LLM_BASE_URL')
		s.llm_temperature = _float('VEIL_LLM_TEMPERATURE')
		s.llm_effort = _opt('VEIL_LLM_EFFORT')
		s.engine = _opt('VEIL_ENGINE') or s.engine
		s.fortress_channel = _opt('FORTRESS_CHANNEL') or s.fortress_channel
		s.cdp_url = _opt('VEIL_CDP_URL')
		s.chromium_path = _opt('VEIL_CHROMIUM_PATH')
		s.headless = _bool('VEIL_HEADLESS', s.headless)
		pf = _opt('VEIL_PROXY_FILE')
		s.proxy_file = Path(pf).expanduser() if pf else None
		s.proxy_gateway = _opt('VEIL_PROXY_GATEWAY')
		s.proxy_strategy = _opt('VEIL_PROXY_STRATEGY') or s.proxy_strategy
		# VEIL_PROXY_MODE (set by the app's proxy panel) picks one source while keeping the others in .env.
		mode = (_opt('VEIL_PROXY_MODE') or '').lower()
		if mode == 'direct':
			s.proxy_file = s.proxy_gateway = None
		elif mode == 'list':
			s.proxy_gateway = None
		s.geo_match = _bool('VEIL_GEO_MATCH', s.geo_match)
		s.max_attempts = int(_opt('VEIL_MAX_ATTEMPTS') or s.max_attempts)
		s.max_steps = int(_opt('VEIL_MAX_STEPS') or s.max_steps)
		s.max_parallel = max(1, int(_opt('VEIL_MAX_PARALLEL') or s.max_parallel))
		dd = _opt('VEIL_DATA_DIR')
		if dd:
			s.data_dir = Path(dd).expanduser()
		pr = _opt('VEIL_PROFILES_FILE')
		s.profiles_file = Path(pr).expanduser() if pr else None
		return s
