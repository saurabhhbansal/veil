"""Sign up on websites with the user's own details: a profiles file they fill in, and a log of where each was used.

profiles.csv (written by the user, one row per email they own):
    id,email,first_name,last_name,dob,gender,phone,address,city,state,postcode,country,password
signups.csv (written by veil, one row per site):
    site,profile_id,email,password,status,identity,updated,note

The agent never sees the values. They go to browser-use as domain-scoped `sensitive_data`, the LLM
only sees placeholders like `x_email`, and the real text is typed only on the target site's domain.
One account per site: a site that already has a row keeps using that row's profile, password and
sticky identity (same device, IP and cookies), so the next visit is the same person, not a new one.
"""
from __future__ import annotations

import csv
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

PROFILE_COLUMNS = ['id', 'email', 'first_name', 'last_name', 'dob', 'gender', 'phone', 'address', 'city', 'state', 'postcode', 'country', 'password']

# signed_up: account exists and works. verify_email: form sent, the site mailed a confirmation link.
# needs_human: stopped at something only the user can do (CAPTCHA, SMS code, a field with no value).
# already_registered: the site says this email already has an account. failed: anything else.
STATUSES = ('signed_up', 'verify_email', 'needs_human', 'already_registered', 'failed')
DONE_STATUSES = ('signed_up', 'verify_email', 'already_registered')  # an account exists: don't make another

_STATUS_RE = re.compile(r'SIGNUP_STATUS:\s*([a-z_]+)\s*[-:–—]?\s*(.*)', re.IGNORECASE)
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


class SignupLog:
	"""signups.csv: one row per site. Plain CSV so it opens in Excel; keep it out of git (it holds passwords)."""

	def __init__(self, path: Path):
		self.path = path
		self.rows: dict[str, Signup] = {}
		if path.exists():
			with path.open(newline='', encoding='utf-8-sig') as f:
				names = {fl.name for fl in fields(Signup)}
				for row in csv.DictReader(f):
					s = Signup(**{k: v or '' for k, v in row.items() if k in names})
					self.rows[s.site] = s

	def get(self, site: str) -> Signup | None:
		return self.rows.get(site)

	def record(self, signup: Signup) -> None:
		signup.updated = time.strftime('%Y-%m-%d %H:%M')
		self.rows[signup.site] = signup
		self.path.parent.mkdir(parents=True, exist_ok=True)
		tmp = self.path.with_suffix('.tmp')
		with tmp.open('w', newline='', encoding='utf-8') as f:
			w = csv.DictWriter(f, fieldnames=[fl.name for fl in fields(Signup)])
			w.writeheader()
			w.writerows(asdict(s) for s in sorted(self.rows.values(), key=lambda s: s.site))
		tmp.replace(self.path)


def identity_name(site: str, profile_id: str) -> str:
	return re.sub(r'[^A-Za-z0-9_.-]', '-', f'acct-{site}-{profile_id}')[:64]


def plan_signup(profiles: list[dict[str, str]], log: SignupLog, site: str, profile_id: str | None = None) -> tuple[dict[str, str], Signup, bool]:
	"""Pick the profile for `site`. Returns (profile, signup row, account_exists).

	A site already in the log keeps its profile: retrying a failed signup continues as the same person,
	and a site with an account never gets a second one. Otherwise `profile_id`, or a random profile.
	"""
	by_id = {p['id']: p for p in profiles}
	if profile_id and profile_id not in by_id:
		raise ValueError(f'no profile with id {profile_id!r} (have: {", ".join(by_id)})')
	prev = log.get(site)
	if prev:
		if prev.status in DONE_STATUSES:
			if profile_id and profile_id != prev.profile_id:
				raise ValueError(f'{site} already has an account ({prev.status}) with profile {prev.profile_id!r}; not creating a second one')
			return by_id.get(prev.profile_id) or {'id': prev.profile_id, 'email': prev.email}, prev, True
		if not profile_id or profile_id == prev.profile_id:
			if prev.profile_id in by_id:
				return by_id[prev.profile_id], prev, False
	profile = by_id[profile_id] if profile_id else secrets.choice(profiles)
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


def build_task(url: str, values: dict[str, str], ask_human: bool = False) -> str:
	"""ask_human: someone is watching (the web UI), so the agent hands CAPTCHAs and codes to them instead of stopping."""
	names = ', '.join(sorted(values))
	blocker = (
		"- If you hit a CAPTCHA, a phone/SMS code, an email code you can't see, or anything else only I can do, call request_human_help "
		"saying exactly what is needed, then continue once I've handed back. If it's still not done after that, stop there."
		if ask_human
		else "- If you hit a CAPTCHA, a phone/SMS code, an email code you can't see, or anything else only I can do, stop there."
	)
	return f"""Create an account for me at {url} using my own details.

My details are provided as placeholders ({names}). Type them with <secret>name</secret>.
For a date of birth split into parts use x_dob_day / x_dob_month (or x_dob_month_name) / x_dob_year; for one box use x_dob (YYYY-MM-DD) or x_dob_slash (DD/MM/YYYY), whichever the field's format asks for.
Use x_password for both password and confirm-password fields.

Rules:
- Only use these values. Never invent, guess or change a detail. If a required field has no matching placeholder, stop.
- Leave optional newsletter/marketing boxes unticked. Tick the terms checkbox only if the form requires it.
{blocker}
- If the site says the email is already registered, stop; do not try a different email.

Finish with done. The last line of your answer must be exactly one of:
SIGNUP_STATUS: signed_up - <what you saw that confirms it>
SIGNUP_STATUS: verify_email - <the site asked to confirm via an emailed link>
SIGNUP_STATUS: needs_human - <what I need to do, and on which page>
SIGNUP_STATUS: already_registered - <the site's message>
SIGNUP_STATUS: failed - <why>"""


def finish(signup: Signup, output: str | None, attempts: list | None = None) -> Signup:
	"""Fill in the signup's status and note from the agent's answer (or why there was none)."""
	signup.status, signup.note = parse_status(output)
	if not output and attempts:
		a = attempts[-1]
		signup.note = f'{a.outcome}: {a.detail}'[:300]
	return signup


def parse_status(output: str | None) -> tuple[str, str]:
	"""Read the agent's SIGNUP_STATUS line. No line, or an unknown status, counts as failed."""
	for line in reversed((output or '').strip().splitlines()):
		m = _STATUS_RE.search(line)
		if m:
			status = m.group(1).lower()
			return (status, m.group(2).strip()) if status in STATUSES else ('failed', line.strip())
	return 'failed', (output or 'no answer from the agent').strip()[-300:]
