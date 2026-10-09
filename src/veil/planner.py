"""Turn a chat message into jobs: one ordinary task, or account jobs (sign up / log in), or several jobs that
run side by side, each in its own fresh browser (own device, exit IP and cookies).

    "Create 2 accounts on reddit, one per profile"  -> signup(reddit, asha), signup(reddit, work)
    "log in to x.com and reddit.com as asha"         -> login(x.com, asha), login(reddit.com, asha)
    "find the cheapest SSD on amazon.in"             -> None (an ordinary task, no planning call)

A keyword check first, so ordinary tasks never pay for the LLM call. Only profile ids and emails, and the
site/profile/status of known accounts, go into that call: never passwords or other details.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel

from veil import accounts

if TYPE_CHECKING:
	from browser_use.llm.base import BaseChatModel

log = logging.getLogger('veil.planner')

ACTIONS = ('task', 'signup', 'login', 'auto')  # auto = "log in or sign up": log in if the account exists, else sign up

# Words that say "more than one thing at once".
_SPLIT_HINT = re.compile(
	r'\b(each|every|all\s+(of\s+)?(my\s+)?(the\s+)?profiles|parallel|simultaneous(ly)?|at\s+once|both|'
	r'(\d+|two|three|four|five|six|several|multiple)\s+(different\s+|separate\s+|new\s+)*(accounts|sessions|browsers|profiles|times|sites))\b',
	re.I,
)
MAX_JOBS = 10


@dataclass
class Job:
	action: str  # one of ACTIONS
	url: str = ''
	profile_id: str | None = None
	task: str = ''  # task jobs: the self-contained instruction; account jobs: anything else asked, in the user's words

	@property
	def is_account(self) -> bool:
		return self.action != 'task'


def worth_planning(text: str) -> bool:
	return accounts.looks_like_signup(text) or accounts.looks_like_login(text) or bool(_SPLIT_HINT.search(text or ''))


class _PlanJob(BaseModel):
	action: Literal['task', 'signup', 'login', 'auto']
	url: str
	profile_id: str
	instructions: str


class _Plan(BaseModel):
	jobs: list[_PlanJob]


_PROMPT = """You split requests typed into a browser agent into jobs. Each job runs in its own fresh browser (its own device, IP address and cookies), in parallel with the other jobs.

Actions:
- signup: create a NEW account for one of the user's profiles on a site.
- login: log in to an account the user ALREADY has on a site.
- auto: the user says "login/signup", "log in or sign up", "get me into", or it's unclear whether the account exists: log in if it exists, otherwise sign up.
- task: anything else (browse, search, compare, buy, read...).

Rules:
- Make several jobs only when the request asks for several independent things at once: several accounts, several sites, "each" or "all" of the profiles, "in parallel", "N times". Otherwise return exactly one job.
- "Use all my profiles, one for each account" means one job per profile. "Create N accounts" means N jobs, each with a different profile, taken in the listed order. Never more account jobs on one site than there are profiles, and never the same profile twice on the same site.
- profile_id is an id from the list below, or "" when the user didn't say which (only when there is one job for that site).
- url: the site's sign-up page for signup, its login page for login/auto, its home page if you don't know those; "" for a task job without one site.
- instructions: for an account job, anything else the user wants done on that site once in, in their words ("" if nothing). For a task job, the complete instruction for that job alone, written so it makes sense without the others.
- Not an account job: registering for an event or webinar, signing up for a newsletter without an account.
- At most {max_jobs} jobs.

The user's profiles (id: email):
{profiles}

Accounts veil already knows about (site, profile, status):
{known}"""


def _fallback(text: str, profiles: list[dict[str, str]]) -> list[Job] | None:
	"""No LLM: only a written domain is understood, as one account job."""
	url = accounts.domain_in(text)
	if not url:
		return None
	signup, login = accounts.looks_like_signup(text), accounts.looks_like_login(text)
	if not (signup or login):
		return None
	action = 'auto' if signup and login else 'login' if login else 'signup'
	return [Job(action, accounts.normalize_url(url), accounts.profile_in(text, profiles), text)]


async def plan(
	text: str, profiles: list[dict[str, str]], known: list[accounts.Signup], llm: BaseChatModel | None, timeout: float = 45
) -> list[Job] | None:
	"""The jobs a message asks for, or None for one ordinary task (run as typed)."""
	if not worth_planning(text):
		return None
	if llm is None:
		return _fallback(text, profiles)
	from browser_use.llm.messages import SystemMessage, UserMessage

	listing = '\n'.join(f'- {p["id"]}: {p.get("email", "")}' for p in profiles) or '(none)'
	known_listing = '\n'.join(f'- {r.site}, {r.profile_id}, {r.status}' for r in known) or '(none)'
	prompt = _PROMPT.format(profiles=listing, known=known_listing, max_jobs=MAX_JOBS)
	try:
		res = await asyncio.wait_for(llm.ainvoke([SystemMessage(content=prompt), UserMessage(content=text)], output_format=_Plan), timeout)
		raw = res.completion.jobs
	except Exception as e:
		log.warning('planning call failed (%s); falling back to a written domain', e)
		return _fallback(text, profiles)

	ids = {p['id'] for p in profiles}
	named = accounts.profile_in(text, profiles)
	jobs: list[Job] = []
	for j in raw[:MAX_JOBS]:
		pid = j.profile_id if j.profile_id in ids else None
		if j.action == 'task':
			jobs.append(Job('task', (j.url or '').strip(), None, (j.instructions or '').strip() or text))
			continue
		url = (j.url or '').strip() or accounts.domain_in(text) or ''
		if not url:
			continue  # an account job needs a site
		jobs.append(Job(j.action, accounts.normalize_url(url), pid, (j.instructions or '').strip()))
	if len(jobs) == 1 and jobs[0].is_account and not jobs[0].profile_id:
		jobs[0].profile_id = named  # a profile named outright wins over the model's guess
	if not jobs or (len(jobs) == 1 and not jobs[0].is_account):
		return None  # one ordinary task: run it as typed
	return jobs
