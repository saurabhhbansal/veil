from veil.config import Settings
from veil.llm import build_llm


def test_openai_sends_no_sampling_params_by_default():
	# gpt-6 models reject frequency_penalty (400); browser-use only drops it for models in its reasoning list
	llm = build_llm(Settings(llm_provider='openai', llm_model='gpt-6-luna', llm_api_key='sk-test'))
	assert llm.frequency_penalty is None and llm.temperature is None
