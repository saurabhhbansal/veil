"""Sign up on websites with the user's own details: a profiles file they fill in, and a log of where each was used.

profiles.csv (written by the user, one row per email they own):
    id,email,username,first_name,last_name,dob,gender,phone,address,city,state,postcode,country,password
signups.csv (written by veil, one row per site and profile):
    site,profile_id,email,password,status,identity,updated,note

The agent never sees the values. They go to browser-use as domain-scoped `sensitive_data`, the LLM
only sees placeholders like `x_email`, and the real text is typed only on the target site's domain.
One account per profile per site: a (site, profile) that already has a row keeps using that row's
password and sticky identity (same device, IP and cookies), so the next visit is the same person, and
a profile that already has an account there never gets a second one. Different profiles may each have
their own account on the same site. Logging in uses the same row (or the profile's own password for an
account made outside veil) on the same sticky identity.
"""
from __future__ import annotations

import csv
import logging
import re
import secrets
import string
import time
from dataclasses import asdict, dataclass, fields
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
	from veil.config import Settings

log = logging.getLogger('veil.accounts')

PROFILE_COLUMNS = ['id', 'email', 'username', 'first_name', 'last_name', 'dob', 'gender', 'phone', 'address', 'city', 'state', 'postcode', 'country', 'password']

# signed_up: account exists and works. verify_email: form sent, the site mailed a confirmation link.
# needs_human: stopped at something only the user can do (CAPTCHA, SMS code, a field with no value).
# already_registered: the site says this email already has an account. logged_in: veil logged in to it.
# failed: anything else.
STATUSES = ('signed_up', 'verify_email', 'logged_in', 'needs_human', 'already_registered', 'failed')
DONE_STATUSES = ('signed_up', 'verify_email', 'logged_in', 'already_registered')  # an account exists: don't make another
# What a login run can end with (the agent's LOGIN_STATUS line).
LOGIN_STATUSES = ('logged_in', 'needs_human', 'wrong_credentials', 'no_account', 'failed')

_STATUS_RE = re.compile(r'(?:SIGNUP|LOGIN)_STATUS:\s*([a-z_]+)\s*[-:–—]?\s*(.*)', re.IGNORECASE)
_MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December']


def site_of(url: str) -> str:
	"""'https://www.Example.com/join?x=1' -> 'example.com'. Accepts a bare domain too."""
	host = urlparse(url if '://' in url else f'https://{url}').hostname or ''
	host = host.lower().rstrip('.')
	if not host:
		raise ValueError(f'not a URL or domain: {url!r}')
	return host[4:] if host.startswith('www.') else host


def normalize_url(url: str) -> str:
	return url if '://' in url else f'https://{url}'


# --- profiles (the user's file) -----------------------------------------------------------------


def profiles_path(settings: Settings) -> Path:
	return settings.profiles_file or settings.accounts_dir / 'profiles.csv'


def init_profiles(path: Path) -> bool:
	"""Write an empty profiles.csv with the column headers. False if it already exists."""
	if path.exists():
		return False
	path.parent.mkdir(parents=True, exist_ok=True)
	with path.open('w', newline='', encoding='utf-8') as f:
		csv.writer(f).writerow(PROFILE_COLUMNS)
	return True


def load_profiles(path: Path, allow_empty: bool = False) -> list[dict[str, str]]:
	"""The user's profiles. allow_empty (the web UI): a missing or empty file is just no profiles yet."""
	if not path.exists():
		if allow_empty:
			return []
		raise ValueError(f'no profiles file at {path}: run `veil accounts init` and fill it in')
	with path.open(newline='', encoding='utf-8-sig') as f:  # -sig: Excel saves CSV with a BOM
		rows = [{(k or '').strip().lower(): (v or '').strip() for k, v in row.items()} for row in csv.DictReader(f)]
	profiles = validate_profiles(rows, where=lambda n: f'{path.name} line {n + 2}')
	if not profiles and not allow_empty:
		raise ValueError(f'{path} has no profiles yet: add a row per email')
	return profiles


def validate_profiles(rows: list[dict[str, str]], where=lambda n: f'profile {n + 1}') -> list[dict[str, str]]:
	"""Blank rows dropped; id and email required, ids unique, dob as YYYY-MM-DD. Raises ValueError naming the row."""
	profiles, seen = [], set()
	for n, row in enumerate(rows):
		if not any(row.values()):
			continue
		if not row.get('id') or not row.get('email'):
			raise ValueError(f'{where(n)}: "id" and "email" are required')
		if row['id'] in seen:
			raise ValueError(f'{where(n)}: duplicate id {row["id"]!r}')
		if '@' not in row['email']:
			raise ValueError(f'{where(n)}: {row["email"]!r} is not an email address')
		if row.get('dob'):
			_parse_dob(row['dob'], where=where(n))
		seen.add(row['id'])
		profiles.append(row)
	return profiles


def save_profiles(path: Path, rows: list[dict[str, str]]) -> list[dict[str, str]]:
	"""Validate and write profiles.csv (only the known columns), replacing it in one step."""
	clean = [{c: str(r.get(c) or '').strip() for c in PROFILE_COLUMNS} for r in rows]
	profiles = validate_profiles(clean)
	path.parent.mkdir(parents=True, exist_ok=True)
	tmp = path.with_suffix('.tmp')
	with tmp.open('w', newline='', encoding='utf-8') as f:
		w = csv.DictWriter(f, fieldnames=PROFILE_COLUMNS)
		w.writeheader()
		w.writerows(profiles)
	tmp.replace(path)
	return profiles


def _parse_dob(value: str, where: str = 'dob') -> date:
	try:
		return date.fromisoformat(value)
	except ValueError:
		raise ValueError(f'{where}: dob must be YYYY-MM-DD, got {value!r}') from None


def placeholders(profile: dict[str, str], password: str) -> dict[str, str]:
	"""The values the agent may type, keyed by the placeholder name it sees (`x_<field>`). Empty fields are left out."""
	out = {f'x_{k}': v for k, v in profile.items() if k in PROFILE_COLUMNS and k not in ('id', 'password') and v}
	if profile.get('first_name') or profile.get('last_name'):
		out['x_full_name'] = ' '.join(p for p in (profile.get('first_name'), profile.get('last_name')) if p)
	if profile.get('dob'):
		d = _parse_dob(profile['dob'])
		out.update(x_dob_day=str(d.day), x_dob_month=str(d.month), x_dob_month_name=_MONTHS[d.month - 1], x_dob_year=str(d.year))
		out['x_dob_slash'] = f'{d.day:02d}/{d.month:02d}/{d.year}'  # DD/MM/YYYY, for single-box date fields outside the US
	out['x_password'] = password
	return out


def new_password(length: int = 16) -> str:
	"""Random password that passes the usual rules (upper, lower, digit, symbol)."""
	alphabet = string.ascii_letters + string.digits
	body = [secrets.choice(alphabet) for _ in range(length - 4)]
	body += [secrets.choice(string.ascii_uppercase), secrets.choice(string.ascii_lowercase), secrets.choice(string.digits), secrets.choice('!@#$%*?')]
	secrets.SystemRandom().shuffle(body)
	return ''.join(body)


# --- signups (veil's log) -----------------------------------------------------------------------


@dataclass
class Signup:
	site: str
	profile_id: str
	email: str
	password: str
	status: str = 'failed'
	identity: str = ''
	updated: str = ''
	note: str = ''


def _row_key(site: str, profile_id: str) -> str:
	return f'{site}\t{profile_id}'


class SignupLog:
	"""signups.csv: one row per (site, profile). Plain CSV so it opens in Excel; keep it out of git (it holds passwords)."""

	def __init__(self, path: Path):
		self.path = path
		self.rows: dict[str, Signup] = self._load()

	def _load(self) -> dict[str, Signup]:
		rows: dict[str, Signup] = {}
		if self.path.exists():
			with self.path.open(newline='', encoding='utf-8-sig') as f:
				names = {fl.name for fl in fields(Signup)}
				for row in csv.DictReader(f):
					s = Signup(**{k: v or '' for k, v in row.items() if k in names})
					rows[_row_key(s.site, s.profile_id)] = s
		return rows

	def for_site(self, site: str) -> list[Signup]:
		"""That site's rows: accounts first, then the most recently updated."""
		rows = sorted((r for r in self.rows.values() if r.site == site), key=lambda r: r.updated, reverse=True)
		return sorted(rows, key=lambda r: r.status not in DONE_STATUSES)  # stable: keeps newest-first within each group

	def get(self, site: str, profile_id: str | None = None) -> Signup | None:
		"""The row for (site, profile), or with no profile named, the site's account (or latest attempt)."""
		if profile_id:
			return self.rows.get(_row_key(site, profile_id))
		rows = self.for_site(site)
		return rows[0] if rows else None

	def record(self, signup: Signup) -> None:
		signup.updated = time.strftime('%Y-%m-%d %H:%M')
		self.rows = self._load()  # a parallel run may have written its own row since this log was read
		self.rows[_row_key(signup.site, signup.profile_id)] = signup
		self.path.parent.mkdir(parents=True, exist_ok=True)
		tmp = self.path.with_suffix('.tmp')
		with tmp.open('w', newline='', encoding='utf-8') as f:
			w = csv.DictWriter(f, fieldnames=[fl.name for fl in fields(Signup)])
			w.writeheader()
			w.writerows(asdict(s) for s in sorted(self.rows.values(), key=lambda s: (s.site, s.profile_id)))
		tmp.replace(self.path)


def identity_name(site: str, profile_id: str) -> str:
	return re.sub(r'[^A-Za-z0-9_.-]', '-', f'acct-{site}-{profile_id}')[:64]


def plan_signup(
	profiles: list[dict[str, str]], log: SignupLog, site: str, profile_id: str | None = None, exclude: set[str] | frozenset[str] = frozenset()
) -> tuple[dict[str, str], Signup, bool]:
	"""Pick the profile for `site`. Returns (profile, signup row, account_exists).

	A named profile with a row for this site keeps that row: a retry continues as the same person, and a
	profile with an account there never gets a second one. With no profile named, the site's existing
	account (or its last attempt) wins; otherwise a random profile not in `exclude` (ids another job of the
	same request already took).
	"""
	by_id = {p['id']: p for p in profiles}
	if profile_id and profile_id not in by_id:
		raise ValueError(f'no profile with id {profile_id!r} (have: {", ".join(by_id)})')
	if profile_id:
		prev = log.get(site, profile_id)
		if prev:
			return by_id[profile_id], prev, prev.status in DONE_STATUSES
		profile = by_id[profile_id]
	else:
		for prev in log.for_site(site):
			if prev.status in DONE_STATUSES:
				return by_id.get(prev.profile_id) or {'id': prev.profile_id, 'email': prev.email}, prev, True
			if prev.profile_id in by_id and prev.profile_id not in exclude:
				return by_id[prev.profile_id], prev, False
		free = [p for p in profiles if p['id'] not in exclude]
		if not free:
			raise ValueError(f'every profile is already taken for {site}')
		profile = secrets.choice(free)
	signup = Signup(
		site=site,
		profile_id=profile['id'],
		email=profile['email'],
		password=profile.get('password') or new_password(),
		identity=identity_name(site, profile['id']),
	)
	return profile, signup, False


def sensitive_data(site: str, values: dict[str, str]) -> dict[str, dict[str, str]]:
	"""Scope the values to the site and its subdomains, so a redirect elsewhere can't receive them."""
	return {f'https://*.{site}': values}


def _blocker(ask_human: bool) -> str:
	if ask_human:
		return (
			"- If you hit a CAPTCHA, a one-time code sent by email or SMS, or anything else only I can do, call request_human_help "
			"saying exactly what is needed. I either do it in the browser or put the code in my note when I hand back: if my note "
			"has a code, type it into the code field. If it's still not done after that, stop there."
		)
	return "- If you hit a CAPTCHA, a phone/SMS code, an email code you can't see, or anything else only I can do, stop there."


# Rules for every account run. The failures seen so far: a "Sign up with Apple" popup taking over the flow,
# and the agent hopping between stale tabs (and calling the fresh one blank) after the person entered a code.
_ACCOUNT_RULES = """- Use the site's own email/password form. Never click "Continue with Google / Apple / Facebook / Microsoft" or any other single sign-on button.
- Work in one tab. Don't open new tabs; if a popup or new tab opens anyway, close it and go back to the tab with the form.
- After I hand the browser back, stay on the current tab and continue from what it shows now: I may have entered a code or moved the flow on. If the page looks blank or half-loaded, wait a few seconds before deciding anything."""


def _details(values: dict[str, str]) -> str:
	return f'My details are provided as placeholders ({", ".join(sorted(values))}). Type them with <secret>name</secret>.'


def build_task(url: str, values: dict[str, str], ask_human: bool = False, request: str | None = None, if_registered: str = 'stop') -> str:
	"""ask_human: someone is watching (the web UI), so the agent hands CAPTCHAs and codes to them instead of stopping.
	request: the user's own words when the sign-up came from a chat message, so anything else they asked is kept.
	if_registered: 'stop', or 'login' (the person asked to "log in or sign up": an existing account is used instead)."""
	asked = f'\nWhat I asked, in my words: "{request.strip()}"\n' if request else ''
	registered = (
		'- If the site says the email is already registered, log in to that account instead with x_email (or x_username) and x_password, '
		'then report SIGNUP_STATUS: logged_in. Never try a different email.'
		if if_registered == 'login'
		else '- If the site says the email is already registered, stop; do not try a different email.'
	)
	return f"""Create an account for me at {url} using my own details.
{asked}
{_details(values)}
For a date of birth split into parts use x_dob_day / x_dob_month (or x_dob_month_name) / x_dob_year; for one box use x_dob (YYYY-MM-DD) or x_dob_slash (DD/MM/YYYY), whichever the field's format asks for.
Use x_password for both password and confirm-password fields. If the site asks for a username or handle and x_username is given, use it.

Rules:
- Only use these values. Never invent, guess or change a detail. If a required field has no matching placeholder, stop.
- Leave optional newsletter/marketing boxes unticked. Tick the terms checkbox only if the form requires it.
{_ACCOUNT_RULES}
{_blocker(ask_human)}
{registered}

Finish with done. The last line of your answer must be exactly one of:
SIGNUP_STATUS: signed_up - <what you saw that confirms it>
SIGNUP_STATUS: verify_email - <the site asked to confirm via an emailed link>
SIGNUP_STATUS: logged_in - <the account already existed and you are now logged in to it>
SIGNUP_STATUS: needs_human - <what I need to do, and on which page>
SIGNUP_STATUS: already_registered - <the site's message>
SIGNUP_STATUS: failed - <why>"""


def build_login_task(url: str, values: dict[str, str], ask_human: bool = False, request: str | None = None) -> str:
	"""Log in to an account the person already has (made by veil, or by them with the profile's password)."""
	asked = f'\nWhat I asked, in my words: "{request.strip()}". Once you are logged in, also do whatever else that asks.\n' if request else ''
	return f"""Log in to my existing account at {url}.
{asked}
{_details(values)}
Log in with x_email and x_password. If the site asks for a username or phone number instead (or as an extra check), use x_username or x_phone when given.

Rules:
- Only use these values. Never create a new account and never change the password.
- Prefer logging in with the password: if the site offers a one-time code instead, look for "use password instead" first.
{_ACCOUNT_RULES}
{_blocker(ask_human)}

Finish with done. The last line of your answer must be exactly one of:
LOGIN_STATUS: logged_in - <what you saw that confirms it, e.g. the home feed or my profile>
LOGIN_STATUS: needs_human - <what I need to do, and on which page>
LOGIN_STATUS: wrong_credentials - <the site's message>
LOGIN_STATUS: no_account - <the site says there is no account for this email>
LOGIN_STATUS: failed - <why>"""


def plan_login(profiles: list[dict[str, str]], log: SignupLog, site: str, profile_id: str | None = None) -> tuple[dict[str, str], Signup, bool]:
	"""Which account to log in to on `site`. Returns (profile, row, row_is_logged).

	A row from a veil sign-up gives the password and the sticky identity. Without one, the account was made
	outside veil: the profile's own password is used, on that profile's sticky identity for the site.
	"""
	by_id = {p['id']: p for p in profiles}
	row = log.get(site, profile_id)
	if row:
		return by_id.get(row.profile_id) or {'id': row.profile_id, 'email': row.email}, row, True
	if profile_id and profile_id not in by_id:
		raise ValueError(f'no profile with id {profile_id!r} (have: {", ".join(by_id)})')
	if not profile_id:
		if len(profiles) != 1:
			raise ValueError(f'say which profile to log in to {site} with (have: {", ".join(by_id) or "none"})')
		profile_id = profiles[0]['id']
	profile = by_id[profile_id]
	if not profile.get('password'):
		raise ValueError(f'profile {profile_id!r} has no password saved: add the one you use on {site} under Accounts')
	row = Signup(site=site, profile_id=profile_id, email=profile['email'], password=profile['password'], identity=identity_name(site, profile_id))
	return profile, row, False


def finish_login(row: Signup, output: str | None, attempts: list | None = None, logged: bool = True) -> Signup | None:
	"""The row to record after a login run, or None (an account veil didn't know of, and the login didn't work)."""
	status, note = parse_status(output, LOGIN_STATUSES)
	if not output and attempts:
		a = attempts[-1]
		note = f'{a.outcome}: {a.detail}'[:300]
	if status == 'logged_in':
		row.status, row.note = 'logged_in', note
		return row
	if not logged:
		return None
	row.note = f'login {status.replace("_", " ")}: {note}'[:300]  # the account is still there: keep its status
	return row


def finish(signup: Signup, output: str | None, attempts: list | None = None) -> Signup:
	"""Fill in the signup's status and note from the agent's answer (or why there was none)."""
	signup.status, signup.note = parse_status(output)
	if not output and attempts:
		a = attempts[-1]
		signup.note = f'{a.outcome}: {a.detail}'[:300]
	return signup


def parse_status(output: str | None, allowed: tuple[str, ...] = STATUSES) -> tuple[str, str]:
	"""Read the agent's SIGNUP_STATUS / LOGIN_STATUS line. No line, or an unknown status, counts as failed."""
	for line in reversed((output or '').strip().splitlines()):
		m = _STATUS_RE.search(line)
		if m:
			status = m.group(1).lower()
			return (status, m.group(2).strip()) if status in allowed else ('failed', line.strip())
	return 'failed', (output or 'no answer from the agent').strip()[-300:]


# --- account requests typed in chat ("go sign up on LinkedIn", "log in to reddit") -------------------

_SIGNUP_HINT = re.compile(
	r'\b(sign[\s-]?up|signup|register|registration|enrol+|(create|make|open|set\s*up)\s+(\d+\s+|an?\s+|my\s+|new\s+|two\s+|three\s+)*accounts?|join)\b', re.I
)
_LOGIN_HINT = re.compile(r'\b(log[\s-]?in|login|sign[\s-]?in|signin)\b', re.I)
_DOMAIN = re.compile(r"""(?<![@\w.-])((?:https?://)?(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/[^\s,;"')]*)?)""", re.I)


def looks_like_signup(text: str) -> bool:
	"""Cheap first check, so ordinary tasks never pay for the planning call."""
	return bool(_SIGNUP_HINT.search(text or ''))


def looks_like_login(text: str) -> bool:
	return bool(_LOGIN_HINT.search(text or ''))


def domain_in(text: str) -> str | None:
	"""The first web address in the text ('linkedin.com', 'https://x.io/join'); email addresses don't count."""
	m = _DOMAIN.search(text or '')
	return m.group(1).rstrip('.') if m else None


def profile_in(text: str, profiles: list[dict[str, str]]) -> str | None:
	"""A profile the text names by email or by id ('as asha', 'with me@a.com')."""
	low = (text or '').lower()
	for p in profiles:
		if p.get('email') and p['email'].lower() in low:
			return p['id']
	for p in profiles:
		if re.search(rf'(?<![\w@.-]){re.escape(p["id"].lower())}(?![\w@-])', low):
			return p['id']
	return None
