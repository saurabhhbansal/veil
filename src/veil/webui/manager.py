"""What the chat app may choose for a run, and the mapping from its values to veil Settings."""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from veil.config import Settings

PROVIDERS = ['openai', 'groq', 'anthropic', 'google', 'deepseek', 'openai-compatible']


def saved_identities(settings: Settings) -> list[str]:
	root = settings.data_dir / 'identities'
	return sorted(p.parent.name for p in root.glob('*/identity.json')) if root.exists() else []


def _blank(v: Any) -> bool:
	return v is None or (isinstance(v, str) and not v.strip())


def settings_from_ui(values: dict[str, Any], base: Settings) -> tuple[Settings, str | None, int | None, dict[str, Any]]:
	"""UI values keyed by component id -> (settings, identity name, max steps, extra Agent kwargs).

	Blank secret fields (API key, proxy gateway) keep what .env says; boxes pre-filled from .env are taken as shown.
	"""
	s = copy.deepcopy(base)
	v = values.get

	def text(key: str) -> str | None:
		x = v(key)
		return None if _blank(x) else str(x).strip()

	def prefilled(key: str, current: str | None) -> str | None:
		# Boxes the UI pre-fills from .env show what will run, so clearing one clears the setting.
		return text(key) if key in values else current

	s.llm_provider = text('agent_settings.llm_provider') or s.llm_provider
	s.llm_model = text('agent_settings.llm_model') or s.llm_model
	s.llm_api_key = text('agent_settings.llm_api_key') or s.llm_api_key
	if s.llm_provider != base.llm_provider and not text('agent_settings.llm_api_key'):
		# VEIL_LLM_API_KEY belongs to the .env provider; never send it to another one. build_llm then
		# falls back to that provider's own variable (GROQ_API_KEY, ANTHROPIC_API_KEY, ...).
		s.llm_api_key = None
	s.llm_base_url = prefilled('agent_settings.llm_base_url', s.llm_base_url)
	s.llm_effort = prefilled('agent_settings.llm_effort', s.llm_effort)
	temp = v('agent_settings.llm_temperature')
	s.llm_temperature = None if _blank(temp) else float(temp)

	# The app always drives the Fortress stealth browser; engine variants stay a CLI/.env choice for testing.
	s.engine = 'fortress'
	s.fortress_channel = text('stealth_settings.fortress_channel') or s.fortress_channel
	s.cdp_url = prefilled('stealth_settings.cdp_url', s.cdp_url)
	if v('stealth_settings.headful') is not None:
		s.headless = not v('stealth_settings.headful')
	if v('stealth_settings.geo_match') is not None:
		s.geo_match = bool(v('stealth_settings.geo_match'))
	s.proxy_gateway = text('stealth_settings.proxy_gateway') or s.proxy_gateway
	pf = prefilled('stealth_settings.proxy_file', str(s.proxy_file) if s.proxy_file else None)
	s.proxy_file = Path(pf).expanduser() if pf else None
	s.proxy_strategy = text('stealth_settings.proxy_strategy') or s.proxy_strategy
	if not _blank(v('stealth_settings.max_attempts')):
		s.max_attempts = max(1, int(v('stealth_settings.max_attempts')))

	max_steps = None if _blank(v('agent_settings.max_steps')) else max(1, int(v('agent_settings.max_steps')))
	agent_kwargs: dict[str, Any] = {}
	if v('agent_settings.use_vision') is not None:
		agent_kwargs['use_vision'] = bool(v('agent_settings.use_vision'))
	if not _blank(v('agent_settings.max_actions')):
		agent_kwargs['max_actions_per_step'] = max(1, int(v('agent_settings.max_actions')))
	if text('agent_settings.extend_system_prompt'):
		agent_kwargs['extend_system_message'] = text('agent_settings.extend_system_prompt')
	return s, text('stealth_settings.identity'), max_steps, agent_kwargs

