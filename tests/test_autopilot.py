"""The in-between layer: CAPTCHAs and emailed codes are resolved without telling the agent, and without the person
unless the automatic way fails. No browser and no network here: the page, the solver and the mailbox are fakes."""
import pytest

from veil import autopilot
from veil.autopilot import AutoPilot
from veil.config import Settings


class FakeSolver:
	def __init__(self, token='TOKEN'):
		self.token = token
		self.solved = []

	async def solve(self, widget):
		self.solved.append(widget)
		return self.token

	async def solve_image(self, png_base64):
		return 'ab12'


class FakeSession:
	def __init__(self, page):
		self.page = page
		self.browser = object()

	async def probe_page(self):
		return self.page


class Notifier:
	def __init__(self):
		self.messages = []

	def __call__(self, level, text):
		self.messages.append((level, text))


def _page(html='<html>sign up form</html>', url='https://shop.test/join'):
	return {'url': url, 'title': 'Sign up', 'html': html}


@pytest.mark.asyncio
async def test_a_captcha_is_solved_and_the_token_injected_without_the_agent_knowing(monkeypatch):
	injected, notices = [], Notifier()
	solver = FakeSolver('SOLVED-TOKEN')
	monkeypatch.setattr(autopilot.captcha, 'make_solver', lambda *a, **k: solver)
	monkeypatch.setattr(autopilot.captcha, 'detect', lambda html, url: autopilot.captcha.Widget('recaptcha_v2', 'KEY', url))
	monkeypatch.setattr(autopilot.captcha, 'injection_js', lambda widget, token: f'inject:{token}')

	async def injected_js(browser, js):
		injected.append(js)

	monkeypatch.setattr(autopilot, 'evaluate_page', injected_js)
	monkeypatch.setattr(autopilot, 'find_captcha', lambda html: None)
	monkeypatch.setattr(autopilot, 'classify', lambda *a: None)

	async def no_sleep(_):
		pass

	monkeypatch.setattr(autopilot.asyncio, 'sleep', no_sleep)
	page = _page('<div class="g-recaptcha" data-sitekey="KEY"></div>')
	ap = AutoPilot(FakeSession(page), Settings(captcha_provider='2captcha', captcha_api_key='k' * 10), notify=notices)
	assert ap.solver is solver
	assert await ap.try_page(page) is True
	assert injected == ['inject:SOLVED-TOKEN'] and solver.solved
	assert notices.messages and 'Solving a recaptcha v2 automatically.' in str(notices.messages)


@pytest.mark.asyncio
async def test_an_agent_request_for_an_email_code_is_answered_with_the_code_not_a_takeover():
	channels = []

	async def fetch(channel):
		channels.append(channel)
		return '654321' if channel == 'email' else None

	ap = AutoPilot(FakeSession(_page()), Settings(), fetch_code=fetch)
	msg = await ap.help('I need the email verification code to continue', {'source': 'agent'})
	assert msg == 'The email verification code is 654321'
	assert channels == ['email']
	assert 'mailbox' not in msg.lower() and 'gmail' not in msg.lower()  # the agent is not told how it was read
	assert await ap.help('The site wants the SMS code', {'source': 'agent'}) is None  # SMS still goes to the person
	assert channels == ['email', 'sms']


@pytest.mark.asyncio
async def test_without_a_solver_or_mailbox_the_person_is_still_asked():
	ap = AutoPilot(FakeSession(_page()), Settings())
	assert ap.solver is None
	assert await ap.help('Please solve this CAPTCHA for me', {'source': 'agent'}) is None
	assert await ap.help('I need the email verification code', {'source': 'agent'}) is None
	assert await ap.try_page(_page()) is False


@pytest.mark.asyncio
async def test_the_solve_budget_stops_paying_for_the_same_page(monkeypatch):
	monkeypatch.setattr(autopilot.captcha, 'make_solver', lambda *a, **k: FakeSolver())
	monkeypatch.setattr(autopilot.captcha, 'detect', lambda html, url: autopilot.captcha.Widget('recaptcha_v2', 'KEY', url))
	monkeypatch.setattr(autopilot.captcha, 'injection_js', lambda widget, token: '')
	ap = AutoPilot(FakeSession(_page()), Settings(captcha_provider='capsolver', captcha_api_key='k' * 10, captcha_max_solves=1))
	page = _page('<div class="g-recaptcha" data-sitekey="KEY"></div>')
	assert await ap.try_page(page) is False  # no injection script: one paid attempt used
	assert await ap.try_page(page) is False  # budget exhausted: no second solve
