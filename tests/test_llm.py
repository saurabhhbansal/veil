from veil.config import Settings
from veil.llm import EFFORTS, MODELS, build_llm
from veil.webui.manager import PROVIDERS


def test_openai_sends_no_sampling_params_by_default():
	# gpt-6 models reject frequency_penalty (400); browser-use only drops it for models in its reasoning list
	llm = build_llm(Settings(llm_provider='openai', llm_model='gpt-6-luna', llm_api_key='sk-test'))
	assert llm.frequency_penalty is None and llm.temperature is None


def test_deepseek_is_selectable_and_high_turns_thinking_on():
	assert 'deepseek' in PROVIDERS and 'deepseek' in EFFORTS and MODELS['deepseek']
	llm = build_llm(Settings(llm_provider='deepseek', llm_model='deepseek-v4-flash', llm_api_key='sk-ds'))
	assert llm.model == 'deepseek-v4-flash' and llm.provider == 'deepseek'
	assert llm.thinking is False and llm.temperature is None  # the wrapper's default: no thinking, provider temperature
	llm = build_llm(Settings(llm_provider='deepseek', llm_model='deepseek-v4-flash', llm_api_key='sk-ds', llm_effort='high'))
	assert llm.thinking is True
	llm = build_llm(Settings(llm_provider='deepseek', llm_model='deepseek-v4-flash', llm_api_key='sk-ds', llm_effort='max'))
	assert llm.thinking is False  # not a DeepSeek level: dropped rather than sent

