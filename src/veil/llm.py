"""Build the browser-use chat model that drives the agent. The provider is a config choice, not a code change."""
from __future__ import annotations

import os

from browser_use.llm.base import BaseChatModel

from veil.config import Settings

# Where each provider's SDK looks for its key when VEIL_LLM_API_KEY isn't set.
_KEY_ENV = {'openai': 'OPENAI_API_KEY', 'groq': 'GROQ_API_KEY', 'anthropic': 'ANTHROPIC_API_KEY', 'google': 'GOOGLE_API_KEY'}


def build_llm(settings: Settings) -> BaseChatModel:
	p = settings.llm_provider
	key = settings.llm_api_key or os.environ.get(_KEY_ENV.get(p, ''), None)
	if not key and p != 'openai-compatible':
		raise RuntimeError(f'no API key: set VEIL_LLM_API_KEY (or {_KEY_ENV.get(p, "the provider key")}) in .env')

	if p in ('openai', 'openai-compatible'):
		from browser_use import ChatOpenAI

		if p == 'openai-compatible' and not settings.llm_base_url:
			raise RuntimeError('VEIL_LLM_PROVIDER=openai-compatible needs VEIL_LLM_BASE_URL')
		return ChatOpenAI(model=settings.llm_model, api_key=key, base_url=settings.llm_base_url, temperature=settings.llm_temperature)
	if p == 'groq':
		from browser_use import ChatGroq

		return ChatGroq(model=settings.llm_model, api_key=key, temperature=settings.llm_temperature)
	if p == 'anthropic':
		from browser_use import ChatAnthropic

		return ChatAnthropic(model=settings.llm_model, api_key=key, temperature=settings.llm_temperature)
	if p == 'google':
		from browser_use import ChatGoogle

		return ChatGoogle(model=settings.llm_model, api_key=key, temperature=settings.llm_temperature)
	raise ValueError(f'unknown VEIL_LLM_PROVIDER {p!r}; use openai, groq, anthropic, google or openai-compatible')
