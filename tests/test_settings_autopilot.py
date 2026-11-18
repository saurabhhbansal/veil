def test_human_check_settings_from_env(monkeypatch):
	import os

	from veil.config import Settings

	for var in ('VEIL_MAILBOX_WAIT', 'VEIL_CAPTCHA_PROVIDER', 'VEIL_CAPTCHA_API_KEY', 'VEIL_CAPTCHA_TIMEOUT', 'VEIL_CAPTCHA_MAX_SOLVES'):
		monkeypatch.delenv(var, raising=False)
	monkeypatch.setenv('VEIL_MAILBOX_WAIT', '45')
	monkeypatch.setenv('VEIL_CAPTCHA_PROVIDER', 'CapSolver')
	monkeypatch.setenv('VEIL_CAPTCHA_API_KEY', 'cap-key-abcdef')
	monkeypatch.setenv('VEIL_CAPTCHA_TIMEOUT', '60')
	monkeypatch.setenv('VEIL_CAPTCHA_MAX_SOLVES', '1')
	try:
		s = Settings.from_env(env_file=None)
		assert s.mailbox_wait == 45.0
		assert s.captcha_provider == 'capsolver' and s.captcha_api_key == 'cap-key-abcdef'
		assert s.captcha_timeout == 60.0 and s.captcha_max_solves == 1
	finally:
		for var in ('VEIL_MAILBOX_WAIT', 'VEIL_CAPTCHA_PROVIDER', 'VEIL_CAPTCHA_API_KEY', 'VEIL_CAPTCHA_TIMEOUT', 'VEIL_CAPTCHA_MAX_SOLVES'):
			os.environ.pop(var, None)
