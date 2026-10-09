from types import SimpleNamespace

import pytest

from veil import accounts as A
from veil import planner
from veil.planner import Job


class _FakeLLM:
	"""Answers the planning call with fixed jobs, and remembers what it was shown."""

	def __init__(self, *jobs):
		self.jobs, self.seen = jobs, None

	async def ainvoke(self, messages, output_format):
		self.seen = ' '.join(m.content for m in messages)
		return SimpleNamespace(completion=output_format(jobs=[dict(zip(('action', 'url', 'profile_id', 'instructions'), j)) for j in self.jobs]))


PROFILES = [{'id': 'asha', 'email': 'asha@a.com', 'phone': '+91 99999 00000', 'password': 'Secret1!'}, {'id': 'work', 'email': 'asha@work.com'}]


@pytest.mark.asyncio
async def test_one_account_job_names_the_site_and_profile():
	llm = _FakeLLM(('signup', 'https://www.linkedin.com/signup', 'work', ''))
	jobs = await planner.plan('Go sign up on LinkedIn with my work email', PROFILES, [], llm)
	assert jobs == [Job('signup', 'https://www.linkedin.com/signup', 'work', '')]
	assert 'asha@work.com' in llm.seen and '+91' not in llm.seen and 'Secret1!' not in llm.seen  # ids and emails only
	# a profile named outright wins over the model's guess
	jobs = await planner.plan('register on example.com as asha', PROFILES, [], _FakeLLM(('signup', '', '', '')))
	assert jobs == [Job('signup', 'https://example.com', 'asha', '')]


@pytest.mark.asyncio
async def test_a_request_for_several_accounts_splits_into_parallel_jobs():
	known = [A.Signup('reddit.com', 'asha', 'asha@a.com', 'pw', 'signed_up')]
	llm = _FakeLLM(('signup', 'https://www.reddit.com/register', 'asha', ''), ('signup', 'https://www.reddit.com/register', 'work', ''))
	jobs = await planner.plan('Create 2 accounts on reddit and use all the profiles I have, one for each', PROFILES, known, llm)
	assert [(j.action, j.profile_id) for j in jobs] == [('signup', 'asha'), ('signup', 'work')]
	assert 'reddit.com, asha, signed_up' in llm.seen and "'pw'" not in llm.seen
	jobs = await planner.plan('compare the iPhone price on amazon.in and flipkart.com in parallel', PROFILES, [], _FakeLLM(
		('task', 'https://amazon.in', '', 'price of the iPhone on amazon.in'), ('task', 'https://flipkart.com', '', 'price of the iPhone on flipkart.com'),
	))
	assert [j.task for j in jobs] == ['price of the iPhone on amazon.in', 'price of the iPhone on flipkart.com'] and not jobs[0].is_account


@pytest.mark.asyncio
async def test_ordinary_tasks_skip_planning_and_it_degrades_without_the_llm():
	never = _FakeLLM(('signup', 'x', '', ''))
	assert await planner.plan('find the cheapest 1TB SSD on amazon.in', PROFILES, [], never) is None and never.seen is None
	assert await planner.plan('register for the webinar on zoom.us', PROFILES, [], _FakeLLM(('task', '', '', 'register for the webinar'))) is None
	assert await planner.plan('sign up on news.ycombinator.com', PROFILES, [], None) == [
		Job('signup', 'https://news.ycombinator.com', None, 'sign up on news.ycombinator.com')
	]
	assert (await planner.plan('go to twitter.com and login/signup as asha', PROFILES, [], None))[0].action == 'auto'
	assert (await planner.plan('log in to reddit.com', PROFILES, [], None))[0].action == 'login'
	assert await planner.plan('sign up on LinkedIn', PROFILES, [], None) is None  # no LLM, no written domain: a normal task
