"""Build the browser-use chat model that drives the agent. The provider is a config choice, not a code change."""
from __future__ import annotations

import os
import re

from browser_use.llm.base import BaseChatModel

from veil.config import Settings

# Where each provider's SDK looks for its key when VEIL_LLM_API_KEY isn't set.
KEY_ENV = {
	'openai': 'OPENAI_API_KEY', 'groq': 'GROQ_API_KEY', 'anthropic': 'ANTHROPIC_API_KEY', 'google': 'GOOGLE_API_KEY', 'deepseek': 'DEEPSEEK_API_KEY',
}

# Reasoning effort each provider accepts, lowest first. Omitted = the provider's own default.
EFFORTS: dict[str, list[str]] = {
	'openai': ['minimal', 'low', 'medium', 'high', 'xhigh'],
	'anthropic': ['low', 'medium', 'high', 'xhigh', 'max'],
	'google': ['minimal', 'low', 'medium', 'high'],
	# DeepSeek has one thinking switch, not a ladder: high sends thinking on, anything else leaves it off.
	'deepseek': ['low', 'high'],
}

# Suggestions for the model box; any other model name works too.
MODELS: dict[str, list[str]] = {
	'openai': ['gpt-6-luna'],
	'anthropic': ['claude-opus-5-5', 'claude-sonnet-5-5', 'claude-haiku-5-5', 'claude-fable-5-1'],
	'google': [],
	'groq': [],
	'deepseek': ['deepseek-v4-flash'],
	'openai-compatible': [],
}

# Claude models that predate adaptive thinking (they take budget_tokens, or no thinking at all).
_OLD_CLAUDE = re.compile(r'claude-(3|instant|2)|claude-(opus|sonnet|haiku)-4-(0|1|5)\b|claude-(opus|sonnet|haiku)-4-\d{8}|claude-(opus|sonnet)-4$', re.I)


def effort_for(provider: str, effort: str | None) -> str | None:
	"""The effort to send, or None (provider default). An effort the provider doesn't have is dropped."""
	return effort if effort and effort in EFFORTS.get(provider, []) else None


def build_llm(settings: Settings) -> BaseChatModel:
	p = settings.llm_provider
	key = settings.llm_api_key or os.environ.get(KEY_ENV.get(p, ''), None)
	if not key and p != 'openai-compatible':
		raise RuntimeError(f'no API key: set VEIL_LLM_API_KEY (or {KEY_ENV.get(p, "the provider key")}) in .env, or add it under the model settings')
	effort = effort_for('openai' if p == 'openai-compatible' else p, settings.llm_effort)

	if p in ('openai', 'openai-compatible'):
		from browser_use import ChatOpenAI

		if p == 'openai-compatible' and not settings.llm_base_url:
			raise RuntimeError('VEIL_LLM_PROVIDER=openai-compatible needs VEIL_LLM_BASE_URL')
		# browser-use sends frequency_penalty=0.3 unless the model is in its reasoning list (which ends at
		# gpt-5); gpt-6 models reject it with a 400. Leave it unset for OpenAI, like temperature.
		extra: dict = {'frequency_penalty': None} if p == 'openai' else {}
		if effort:
			# browser-use only sends reasoning_effort for models in its reasoning list; this model is one.
			extra.update(reasoning_effort=effort, reasoning_models=[settings.llm_model])
		return ChatOpenAI(model=settings.llm_model, api_key=key, base_url=settings.llm_base_url, temperature=settings.llm_temperature, **extra)
	if p == 'groq':
		from browser_use import ChatGroq

		return ChatGroq(model=settings.llm_model, api_key=key, temperature=settings.llm_temperature)
	if p == 'deepseek':
		from browser_use import ChatDeepSeek

		# browser-use's DeepSeek client talks to https://api.deepseek.com/v1. Thinking is on or off there rather
		# than a level, so only 'high' turns it on (the wrapper ignores it on models without thinking).
		return ChatDeepSeek(model=settings.llm_model, api_key=key, temperature=settings.llm_temperature, thinking=effort == 'high')
	if p == 'anthropic':
		from browser_use import ChatAnthropic

		extra = {}
		if not _OLD_CLAUDE.search(settings.llm_model):
			# Adaptive thinking also makes browser-use use tool_choice=auto, which current Claude models require.
			extra['thinking'] = {'type': 'adaptive'}
			if effort:
				extra['output_config'] = {'effort': effort}
		return ChatAnthropic(model=settings.llm_model, api_key=key, temperature=settings.llm_temperature, **extra)
	if p == 'google':
		from browser_use import ChatGoogle

		return ChatGoogle(model=settings.llm_model, api_key=key, temperature=settings.llm_temperature, **({'thinking_level': effort} if effort else {}))
	raise ValueError(f'unknown VEIL_LLM_PROVIDER {p!r}; use openai, groq, anthropic, google, deepseek or openai-compatible')
