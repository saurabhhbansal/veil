from veil.config import Settings
from veil.llm import EFFORTS, MODELS, build_llm
from veil.webui.manager import PROVIDERS


def test_openai_sends_no_sampling_params_by_default():
	# gpt-6 models reject frequency_penalty (400); browser-use only drops it for models in its reasoning list
	llm = build_llm(Settings(llm_provider='openai', llm_model='gpt-6-luna', llm_api_key='sk-test'))
	assert llm.frequency_penalty is None and llm.temperature is None


def test_deepseek_models_and_effort_follow_the_api_guide():
	# https://api-docs.deepseek.com/guides/thinking_mode: thinking is on by default (default effort high),
	# reasoning_effort takes low/high/max, and the current models are deepseek-flash and deepseek-v4-pro.
	assert 'deepseek' in PROVIDERS and EFFORTS['deepseek'] == ['low', 'high', 'max']
	assert MODELS['deepseek'] == ['deepseek-flash', 'deepseek-v4-pro']
	llm = build_llm(Settings(llm_provider='deepseek', llm_model='deepseek-flash', llm_api_key='sk-ds'))
	assert (llm.model, llm.provider) == ('deepseek-flash', 'deepseek')
	assert llm.thinking is True and llm.reasoning_effort is None  # thinking on at the provider's default effort
	request = llm._request_kwargs()
	assert request['extra_body'] == {'thinking': {'type': 'enabled'}} and 'reasoning_effort' not in request
	llm = build_llm(Settings(llm_provider='deepseek', llm_model='deepseek-v4-pro', llm_api_key='sk-ds', llm_effort='max'))
	assert llm.reasoning_effort == 'max' and llm._request_kwargs()['reasoning_effort'] == 'max'
	llm = build_llm(Settings(llm_provider='deepseek', llm_model='deepseek-flash', llm_api_key='sk-ds', llm_effort='minimal'))
	assert llm.reasoning_effort is None  # not a DeepSeek level: dropped rather than sent

