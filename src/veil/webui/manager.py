# Component registry and config save/load adapted from browser-use/web-ui
# (src/webui/webui_manager.py, MIT License, Copyright (c) 2024 Browser Use Inc.; see LICENSE-web-ui).
"""Registry of the UI's input components, the mapping from their values to veil Settings, and config files."""
from __future__ import annotations

import copy
import json
import time
from pathlib import Path
from typing import Any

from veil.config import Settings

# Never written to a saved config: they belong in .env.
SECRET_IDS = {'agent_settings.llm_api_key', 'stealth_settings.proxy_gateway'}


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
	temp = v('agent_settings.llm_temperature')
	s.llm_temperature = None if _blank(temp) else float(temp)

	s.engine = text('stealth_settings.engine') or s.engine
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


class WebuiManager:
	def __init__(self, config_dir: Path):
		self.id_to_component: dict[str, Any] = {}
		self.component_to_id: dict[Any, str] = {}
		self.config_dir = config_dir

	def add_components(self, tab_name: str, components: dict[str, Any]) -> None:
		for name, comp in components.items():
			cid = f'{tab_name}.{name}'
			self.id_to_component[cid] = comp
			self.component_to_id[comp] = cid

	def get_components(self) -> list[Any]:
		return list(self.id_to_component.values())

	def get_component_by_id(self, cid: str) -> Any:
		return self.id_to_component[cid]

	def values_by_id(self, components: dict[Any, Any]) -> dict[str, Any]:
		return {self.component_to_id[c]: val for c, val in components.items() if c in self.component_to_id}

	# --- config files ------------------------------------------------------------------------

	def list_configs(self) -> list[str]:
		return sorted((p.name for p in self.config_dir.glob('*.json')), reverse=True) if self.config_dir.exists() else []

	def save_config(self, values: dict[str, Any]) -> Path:
		self.config_dir.mkdir(parents=True, exist_ok=True)
		keep = {k: val for k, val in values.items() if k not in SECRET_IDS}
		path = self.config_dir / f'{time.strftime("%Y%m%d-%H%M%S")}.json'
		path.write_text(json.dumps(keep, indent=2, default=str))
		return path

	def load_config(self, name: str) -> dict[str, Any]:
		path = self.config_dir / Path(name).name  # a bare file name: never read outside the config dir
		data = json.loads(path.read_text())
		return {k: val for k, val in data.items() if k in self.id_to_component and k not in SECRET_IDS}
