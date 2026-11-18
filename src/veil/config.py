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
	llm_provider: str = 'openai'  # openai | groq | anthropic | google | deepseek | openai-compatible
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
	proxy_country: str | None = None  # gateway {country} when a session asks for none (default us)

	# Orchestration
	max_attempts: int = 3
	max_steps: int = 40
	max_parallel: int = 3  # browsers a split request may run at once (each is a full Fortress instance)
	max_bulk: int = 25  # accounts one "create accounts" request may make (each is a full Fortress instance)
	data_dir: Path = field(default_factory=lambda: Path.home() / '.veil')
	profiles_file: Path | None = None  # your own sign-up details; default <data_dir>/accounts/profiles.csv
	env_file: Path | None = None  # the .env these settings came from; the web UI writes keys and proxies back to it

	# Company identity server (identities.py, docs/IDENTITY_API.md): when set, account runs get their person from it, not profiles.csv
	identity_api: str | None = None  # base URL, e.g. http://10.0.0.5:8900
	identity_api_token: str | None = None  # sent as Authorization: Bearer <token>
	identity_api_timeout: float = 15.0
	identity_api_send_passwords: bool = False  # include the password veil used when reporting a run's outcome

	# Automatic handling of the human checks a sign-up hits: emailed one-time codes and CAPTCHAs.
	mailbox_wait: float = 90.0  # how long to poll the person's mailbox for an emailed code before falling back to them
	captcha_provider: str | None = None  # 2captcha | capsolver (None = CAPTCHA solving off; the person is asked)
	captcha_api_key: str | None = None
	captcha_timeout: float = 120.0  # how long a solver may take for one CAPTCHA
	captcha_max_solves: int = 2  # CAPTCHAs one run may pay to solve on the same page before asking the person

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
		s.proxy_country = _opt('VEIL_PROXY_COUNTRY')
		s.max_attempts = int(_opt('VEIL_MAX_ATTEMPTS') or s.max_attempts)
		s.max_steps = int(_opt('VEIL_MAX_STEPS') or s.max_steps)
		s.max_parallel = max(1, int(_opt('VEIL_MAX_PARALLEL') or s.max_parallel))
		s.max_bulk = max(1, int(_opt('VEIL_MAX_BULK') or s.max_bulk))
		dd = _opt('VEIL_DATA_DIR')
		if dd:
			s.data_dir = Path(dd).expanduser()
		pr = _opt('VEIL_PROFILES_FILE')
		s.profiles_file = Path(pr).expanduser() if pr else None
		s.identity_api = _opt('VEIL_IDENTITY_API')
		s.identity_api_token = _opt('VEIL_IDENTITY_API_TOKEN')
		s.identity_api_timeout = _float('VEIL_IDENTITY_API_TIMEOUT') or s.identity_api_timeout
		s.identity_api_send_passwords = _bool('VEIL_IDENTITY_API_SEND_PASSWORDS', s.identity_api_send_passwords)
		s.mailbox_wait = _float('VEIL_MAILBOX_WAIT') or s.mailbox_wait
		s.captcha_provider = (_opt('VEIL_CAPTCHA_PROVIDER') or '').lower() or None
		s.captcha_api_key = _opt('VEIL_CAPTCHA_API_KEY')
		s.captcha_timeout = _float('VEIL_CAPTCHA_TIMEOUT') or s.captcha_timeout
		s.captcha_max_solves = max(0, int(_opt('VEIL_CAPTCHA_MAX_SOLVES') or s.captcha_max_solves))
		return s
