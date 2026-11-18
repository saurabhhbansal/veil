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


def test_one_account_per_profile_per_site(tmp_path):
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
	assert A.plan_signup(profiles, log, 'example.com')[2] is True  # no profile named: the site's account
	assert A.plan_signup(profiles, log, 'example.com', 'p1')[2] is True  # never a second account for p1
	# Another profile may have its own account there, on its own identity.
	_, other, exists = A.plan_signup(profiles, log, 'example.com', 'p2')
	assert not exists and other.identity == 'acct-example.com-p2'
	# Unnamed jobs of one request get different profiles.
	assert A.plan_signup(profiles, A.SignupLog(tmp_path / 'fresh.csv'), 'other.org', exclude={'p1'})[0]['id'] == 'p2'
	with pytest.raises(ValueError, match='every profile'):
		A.plan_signup(profiles, A.SignupLog(tmp_path / 'fresh.csv'), 'other.org', exclude={'p1', 'p2'})


def test_parallel_runs_dont_lose_each_others_rows(tmp_path):
	path = tmp_path / 'signups.csv'
	first, second = A.SignupLog(path), A.SignupLog(path)  # both read before either wrote
	first.record(A.Signup('x.com', 'p1', 'a@a.com', 'pw1', 'signed_up'))
	second.record(A.Signup('x.com', 'p2', 'b@b.com', 'pw2', 'failed'))
	rows = A.SignupLog(path)
	assert {r.profile_id for r in rows.for_site('x.com')} == {'p1', 'p2'}
	assert rows.get('x.com').profile_id == 'p1' and rows.get('x.com', 'p2').password == 'pw2'  # the account first


def test_login_uses_the_signup_row_or_the_profile_password(tmp_path):
	path = tmp_path / 'p.csv'
	path.write_text('id,email,username,password\np1,a@a.com,asha_r,Own1!\np2,b@b.com,,\n')
	profiles = A.load_profiles(path)
	log = A.SignupLog(tmp_path / 's.csv')
	# an account made outside veil: the profile's own password, on that profile's identity for the site
	profile, row, logged = A.plan_login(profiles, log, 'x.com', 'p1')
	assert (row.password, row.identity, logged) == ('Own1!', 'acct-x.com-p1', False)
	assert A.placeholders(profile, row.password)['x_username'] == 'asha_r'
	with pytest.raises(ValueError, match='no password saved'):
		A.plan_login(profiles, log, 'x.com', 'p2')
	with pytest.raises(ValueError, match='say which profile'):
		A.plan_login(profiles, log, 'x.com')
	assert A.finish_login(row, 'LOGIN_STATUS: wrong_credentials - nope', logged=False) is None  # nothing recorded
	rec = A.finish_login(row, 'Home feed.\nLOGIN_STATUS: logged_in - saw the feed', logged=False)
	log.record(rec)
	# next time: the recorded row, found without naming the profile
	_, again, logged = A.plan_login(profiles, A.SignupLog(log.path), 'x.com')
	assert again.status == 'logged_in' and logged and again.password == 'Own1!'
	kept = A.finish_login(again, 'LOGIN_STATUS: needs_human - SMS code', logged=True)
	assert kept.status == 'logged_in' and kept.note.startswith('login needs human')  # a failed login keeps the account's status


def test_account_tasks_stay_on_the_form_and_can_log_in_instead():
	task = A.build_task('https://x.test', {'x_email': 'e'}, ask_human=True, if_registered='login')
	assert 'single sign-on' in task and 'one tab' in task and 'log in to that account instead' in task
	assert 'stop; do not try' in A.build_task('https://x.test', {'x_email': 'e'})
	login = A.build_login_task('https://x.test/login', {'x_email': 'e', 'x_password': 'p'}, ask_human=True, request='log in and like my post')
	assert 'LOGIN_STATUS: logged_in' in login and 'like my post' in login and 'request_human_help' in login
	assert A.parse_status('LOGIN_STATUS: logged_in - feed', A.LOGIN_STATUSES) == ('logged_in', 'feed')
	assert A.parse_status('LOGIN_STATUS: wrong_credentials - x')[0] == 'failed'  # not a sign-up status


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


def test_signup_log_forgets_one_row(tmp_path):
	log = A.SignupLog(tmp_path / 'signups.csv')
	log.record(A.Signup('shop.test', 'p1', 'a@a.com', 'pw1', 'failed', 'acct-shop.test-p1'))
	log.record(A.Signup('shop.test', 'p2', 'b@b.com', 'pw2', 'signed_up', 'acct-shop.test-p2'))
	assert log.remove('shop.test', 'p1')
	assert [row.profile_id for row in A.SignupLog(log.path).for_site('shop.test')] == ['p2']
	assert not log.remove('shop.test', 'p1')  # already gone
	assert A.SignupLog(log.path).get('shop.test', 'p2').password == 'pw2'  # the other row is untouched


def test_watched_signups_ask_the_person_instead_of_stopping():
	assert 'request_human_help' in A.build_task('https://a.test', {'x_email': 'e'}, ask_human=True)
	assert 'request_human_help' not in A.build_task('https://a.test', {'x_email': 'e'})


def test_domain_in_ignores_email_addresses():
	assert A.domain_in('sign up with me@gmail.com on github.com/signup') == 'github.com/signup'
	assert A.domain_in('sign up with me@gmail.com') is None


def test_per_account_instructions_never_say_how_many_accounts():
	# The app keeps the count; anything the person typed that would leak it is rewritten for one account.
	assert A.per_account('create 3 accounts on reddit with my work email') == 'create an account on reddit with my work email'
	assert A.per_account('create 2 new accounts, one per profile') == 'create an account'
	assert A.per_account('make two accounts in parallel') == 'make an account'
	assert A.per_account('sign up twice with a different profile for each account') == 'sign up once with a different profile'
	assert A.per_account('create an account with my work email') == 'create an account with my work email'  # already singular
	assert A.per_account('create five reddit accounts') == 'create an account'
	assert A.per_account('create accounts on reddit') == 'create an account on reddit'
	assert A.per_account('create accounts for 4 different people') == 'create an account'
	# A site name that happens to contain "accounts" (or the page's name) is not a count to rewrite.
	assert A.per_account('create an account at accounts.google.com') == 'create an account at accounts.google.com'
	assert A.per_account('open the accounts page') == 'open the accounts page'
	assert A.per_account('') == ''
	task = A.build_task('https://x.test', {'x_email': 'e'}, ask_human=True, request=A.per_account('create 3 accounts'))
	assert 'Create an account for me at https://x.test' in task and 'create an account' in task
	assert '3 accounts' not in task and '3 account' not in task


class _RemotePeople:
	"""An identity server that hands people out on demand and offers no listing (IdentityAPI.listing() -> [])."""

	remote = True

	def __init__(self, people):
		self.people = people
		self.assigned = []

	def listing(self):
		return []

	def assign(self, site, purpose, exclude=()):
		for person in self.people:
			if person['id'] not in exclude:
				self.assigned.append(person['id'])
				return person
		raise ValueError('no more identities')

	def get(self, profile_id):
		return next(p for p in self.people if p['id'] == profile_id)

	def report(self, row, action, *, background=True):
		pass

	def code_fetcher(self, profile_id, site):
		return None


def test_identity_server_bulk_signups_are_assigned_on_demand(tmp_path):
	log = A.SignupLog(tmp_path / 's.csv')
	log.record(A.Signup('shop.test', 'p1', 'a@a.com', 'pw', 'signed_up', 'acct-shop.test-p1'))
	source = _RemotePeople([{'id': 'p1', 'email': 'a@a.com'}, {'id': 'p2', 'email': 'b@b.com'}, {'id': 'p3', 'email': 'c@c.com'}])
	# No listing (the server doesn't offer one): people are assigned on demand, one per account. p1 has an
	# account there already, so it's skipped rather than logged in to.
	picked = A.plan_fresh_signups(source, log, 'shop.test', 2)
	assert [p['id'] for p, _ in picked] == ['p2', 'p3'] and picked[0][1] is None
	assert source.assigned == ['p1', 'p2', 'p3']



def test_bulk_signups_only_take_people_without_an_account_there(tmp_path):
	profiles = _profiles(tmp_path)
	log = A.SignupLog(tmp_path / 'bulk.csv')
	log.record(A.Signup('shop.test', 'p1', 'me@a.com', 'pw', 'signed_up', 'acct-shop.test-p1'))
	(picked, row), = A.plan_fresh_signups(profiles, log, 'shop.test', 1)
	assert picked['id'] == 'p2' and row is None  # p1 already has an account there: that would be a login
	with pytest.raises(ValueError, match='only 1 of your 2 profiles'):
		A.plan_fresh_signups(profiles, log, 'shop.test', 2)
	# A failed earlier attempt is still someone who has no account yet: the retry continues as them.
	log.record(A.Signup('shop.test', 'p2', 'me@b.com', 'pw', 'failed', 'acct-shop.test-p2'))
	(picked, row), = A.plan_fresh_signups(profiles, log, 'shop.test', 1)
	assert picked['id'] == 'p2' and row.status == 'failed'
	with pytest.raises(ValueError, match='at least one'):
		A.plan_fresh_signups(profiles, log, 'shop.test', 0)
	with pytest.raises(ValueError, match='3 new accounts'):
		A.plan_fresh_signups(profiles, log, 'fresh.test', 3)


def test_platform_picker_lists_known_sites_once():
	platforms = A.platforms(('shop.test', 'reddit.com'))
	assert [p['name'] for p in platforms][:2] == ['Reddit', 'GitHub']
	assert platforms[-1] == {'name': 'shop.test', 'url': 'https://shop.test'}
	assert sum(1 for p in platforms if A.site_of(p['url']) == 'reddit.com') == 1

