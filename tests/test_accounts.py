import pytest

from veil import accounts as A


def _profiles(tmp_path, body='p1,me@a.com,Asha,Rao,1999-03-07\np2,me@b.com,Asha,Rao,1999-03-07\n'):
	path = tmp_path / 'profiles.csv'
	path.write_text('﻿id,email,first_name,last_name,dob\n' + body, encoding='utf-8')  # BOM: how Excel saves CSV
	return A.load_profiles(path)


def test_site_of():
	assert A.site_of('https://www.Example.com/join?x=1') == 'example.com'
	assert A.site_of('shop.example.co.uk') == 'shop.example.co.uk'
	with pytest.raises(ValueError):
		A.site_of('https://')


def test_init_writes_headers_once(tmp_path):
	path = tmp_path / 'acc' / 'profiles.csv'
	assert A.init_profiles(path) and not A.init_profiles(path)
	assert path.read_text().strip() == ','.join(A.PROFILE_COLUMNS)


def test_load_profiles_validates(tmp_path):
	assert [p['id'] for p in _profiles(tmp_path)] == ['p1', 'p2']
	with pytest.raises(ValueError, match='required'):
		_profiles(tmp_path, ',me@a.com,,,\n')
	with pytest.raises(ValueError, match='duplicate'):
		_profiles(tmp_path, 'p1,a@a.com,,,\np1,b@b.com,,,\n')
	with pytest.raises(ValueError, match='YYYY-MM-DD'):
		_profiles(tmp_path, 'p1,a@a.com,,,07/03/1999\n')


def test_placeholders_split_the_date_and_skip_empty_fields(tmp_path):
	values = A.placeholders(_profiles(tmp_path)[0], 'pw')
	assert values['x_full_name'] == 'Asha Rao' and values['x_password'] == 'pw'
	assert (values['x_dob_day'], values['x_dob_month'], values['x_dob_month_name'], values['x_dob_year']) == ('7', '3', 'March', '1999')
	assert values['x_dob_slash'] == '07/03/1999'
	assert 'x_id' not in values and 'x_phone' not in values


def test_new_password_meets_common_rules():
	for _ in range(50):
		pw = A.new_password()
		assert len(pw) == 16
		assert any(c.isupper() for c in pw) and any(c.islower() for c in pw) and any(c.isdigit() for c in pw)
		assert any(c in '!@#$%*?' for c in pw)


def test_one_account_per_site(tmp_path):
	profiles = _profiles(tmp_path)
	log = A.SignupLog(tmp_path / 'signups.csv')
	profile, signup, exists = A.plan_signup(profiles, log, 'example.com', 'p1')
	assert not exists and signup.identity == 'acct-example.com-p1'

	signup.status = 'failed'
	log.record(signup)
	# A retry continues as the same person with the same password.
	again = A.plan_signup(profiles, A.SignupLog(log.path), 'example.com')
	assert again[0]['id'] == 'p1' and again[1].password == signup.password and not again[2]

	signup.status = 'signed_up'
	log.record(signup)
	log = A.SignupLog(log.path)
	assert A.plan_signup(profiles, log, 'example.com')[2] is True
	with pytest.raises(ValueError, match='not creating a second'):
		A.plan_signup(profiles, log, 'example.com', 'p2')
	# Other sites are independent.
	assert not A.plan_signup(profiles, log, 'other.org', 'p2')[2]


def test_profile_password_column_wins(tmp_path):
	path = tmp_path / 'p.csv'
	path.write_text('id,email,password\np1,a@a.com,MyOwnPass1!\n')
	_, signup, _ = A.plan_signup(A.load_profiles(path), A.SignupLog(tmp_path / 's.csv'), 'x.com')
	assert signup.password == 'MyOwnPass1!'


def test_sensitive_data_is_scoped_to_the_site():
	assert A.sensitive_data('example.com', {'x_email': 'e'}) == {'https://*.example.com': {'x_email': 'e'}}


def test_parse_status():
	assert A.parse_status('Filled the form.\nSIGNUP_STATUS: verify_email - check inbox') == ('verify_email', 'check inbox')
	assert A.parse_status('SIGNUP_STATUS: Signed_Up — welcome page shown')[0] == 'signed_up'
	assert A.parse_status('SIGNUP_STATUS: maybe')[0] == 'failed'
	assert A.parse_status(None) == ('failed', 'no answer from the agent')


def test_save_profiles_validates_and_round_trips(tmp_path):
	path = tmp_path / 'acc' / 'profiles.csv'
	assert A.load_profiles(path, allow_empty=True) == []
	A.save_profiles(path, [{'id': 'p1', 'email': 'me@a.com', 'dob': '1999-03-07', 'unknown': 'dropped'}, {}])
	(p,) = A.load_profiles(path)
	assert p['email'] == 'me@a.com' and p['dob'] == '1999-03-07' and 'unknown' not in p
	with pytest.raises(ValueError, match='not an email'):
		A.save_profiles(path, [{'id': 'p1', 'email': 'nope'}])
	with pytest.raises(ValueError, match='profile 2: duplicate'):
		A.save_profiles(path, [{'id': 'p1', 'email': 'a@a.com'}, {'id': 'p1', 'email': 'b@b.com'}])
	assert A.load_profiles(path)[0]['email'] == 'me@a.com'  # a rejected save leaves the file alone


def test_watched_signups_ask_the_person_instead_of_stopping():
	assert 'request_human_help' in A.build_task('https://a.test', {'x_email': 'e'}, ask_human=True)
	assert 'request_human_help' not in A.build_task('https://a.test', {'x_email': 'e'})


class _FakeLLM:
	"""Answers the sign-up intent call with a fixed verdict, and remembers what it was shown."""

	def __init__(self, **verdict):
		self.verdict, self.seen = verdict, None

	async def ainvoke(self, messages, output_format):
		self.seen = ' '.join(m.content for m in messages)
		from types import SimpleNamespace

		return SimpleNamespace(completion=output_format(**self.verdict))


PROFILES = [{'id': 'asha', 'email': 'asha@a.com', 'phone': '+91 99999 00000'}, {'id': 'work', 'email': 'asha@work.com'}]


@pytest.mark.asyncio
async def test_chat_signup_intent_names_the_site_and_profile():
	llm = _FakeLLM(is_signup=True, url='https://www.linkedin.com/signup', profile_id='work')
	intent = await A.detect_signup('Go sign up on LinkedIn with my work email', PROFILES, llm)
	assert intent == A.SignupIntent('https://www.linkedin.com/signup', 'work')
	assert 'asha@work.com' in llm.seen and '+91' not in llm.seen  # only ids and emails go to the intent call
	# a profile named outright wins over the model's guess
	intent = await A.detect_signup('register on example.com as asha', PROFILES, _FakeLLM(is_signup=True, url='', profile_id='work'))
	assert intent == A.SignupIntent('https://example.com', 'asha')


@pytest.mark.asyncio
async def test_chat_signup_intent_skips_other_tasks_and_degrades_without_the_llm():
	never = _FakeLLM(is_signup=True, url='x', profile_id='')
	assert await A.detect_signup('find the cheapest 1TB SSD on amazon.in', PROFILES, never) is None and never.seen is None
	assert await A.detect_signup('register for the webinar on zoom.us', PROFILES, _FakeLLM(is_signup=False, url='', profile_id='')) is None
	assert await A.detect_signup('sign up on news.ycombinator.com', PROFILES, None) == A.SignupIntent('https://news.ycombinator.com', None)
	assert await A.detect_signup('sign up on LinkedIn', PROFILES, None) is None  # no LLM, no written domain: a normal task


def test_domain_in_ignores_email_addresses():
	assert A.domain_in('sign up with me@gmail.com on github.com/signup') == 'github.com/signup'
	assert A.domain_in('sign up with me@gmail.com') is None
