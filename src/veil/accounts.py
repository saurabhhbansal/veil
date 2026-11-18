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
	from veil.identities import ProfileSource

log = logging.getLogger('veil.accounts')

PROFILE_COLUMNS = [
	'id', 'email', 'username', 'first_name', 'last_name', 'dob', 'gender', 'phone', 'address', 'city', 'state', 'postcode', 'country',
	'password',
	# optional: an app password + IMAP host so veil can read emailed one-time codes instead of asking the person
	'mailbox_password', 'mailbox_imap',
]
# Secrets: never typeable by the agent and never sent back to the page.
SECRET_COLUMNS = ('password', 'mailbox_password')
_NOT_PLACEHOLDERS = ('id', 'mailbox_imap', *SECRET_COLUMNS)

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


# The platform picker in the web app (and its "create accounts" control). A starting page for the sign-up
# flow, not a promise: the agent finds the site's own form from there, and any other domain can be typed in.
PLATFORMS = [
	{'name': 'Reddit', 'url': 'https://www.reddit.com/register'},
	{'name': 'GitHub', 'url': 'https://github.com/signup'},
	{'name': 'LinkedIn', 'url': 'https://www.linkedin.com/signup'},
	{'name': 'X (Twitter)', 'url': 'https://x.com/i/flow/signup'},
	{'name': 'Instagram', 'url': 'https://www.instagram.com/accounts/emailsignup/'},
	{'name': 'Facebook', 'url': 'https://www.facebook.com/r.php'},
	{'name': 'Google', 'url': 'https://accounts.google.com/signup'},
	{'name': 'Discord', 'url': 'https://discord.com/register'},
	{'name': 'TikTok', 'url': 'https://www.tiktok.com/signup'},
	{'name': 'Spotify', 'url': 'https://www.spotify.com/signup'},
	{'name': 'Twitch', 'url': 'https://www.twitch.tv/signup'},
	{'name': 'Pinterest', 'url': 'https://www.pinterest.com/signup/'},
]


def platforms(known_sites: tuple[str, ...] | list[str] = ()) -> list[dict[str, str]]:
	"""The picker's platforms: the list above, plus sites veil already signed up on (so repeats are easy)."""
	out: list[dict[str, str]] = []
	seen = set()
	for p in PLATFORMS:
		out.append(dict(p))
		seen.add(site_of(p['url']))
	for site in known_sites:
		if site not in seen:
			seen.add(site)
			out.append({'name': site, 'url': normalize_url(site)})
	return out


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
	"""The values the agent may type, keyed by the placeholder name it sees (`x_<field>`). Empty fields are left out.
	Mailbox credentials are never placeholders: the agent doesn't need them and must not learn they exist."""
	out = {f'x_{k}': v for k, v in profile.items() if k in PROFILE_COLUMNS and k not in _NOT_PLACEHOLDERS and v}
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

	def _write(self) -> None:
		self.path.parent.mkdir(parents=True, exist_ok=True)
		tmp = self.path.with_suffix('.tmp')
		with tmp.open('w', newline='', encoding='utf-8') as f:
			w = csv.DictWriter(f, fieldnames=[fl.name for fl in fields(Signup)])
			w.writeheader()
			w.writerows(asdict(s) for s in sorted(self.rows.values(), key=lambda s: (s.site, s.profile_id)))
		tmp.replace(self.path)

	def record(self, signup: Signup) -> None:
		signup.updated = time.strftime('%Y-%m-%d %H:%M')
		self.rows = self._load()  # a parallel run may have written its own row since this log was read
		self.rows[_row_key(signup.site, signup.profile_id)] = signup
		self._write()

	def remove(self, site: str, profile_id: str) -> bool:
		"""Forget one row (the account on the site, if any, is not touched). True if there was one."""
		self.rows = self._load()
		if self.rows.pop(_row_key(site, profile_id), None) is None:
			return False
		self._write()
		return True


def identity_name(site: str, profile_id: str) -> str:
	return re.sub(r'[^A-Za-z0-9_.-]', '-', f'acct-{site}-{profile_id}')[:64]


def _source(profiles: list[dict[str, str]] | ProfileSource) -> ProfileSource:
	from veil.identities import LocalProfiles

	return LocalProfiles(profiles) if isinstance(profiles, list) else profiles


def _known(source: ProfileSource, row: Signup) -> dict[str, str]:
	"""The profile behind a logged row; just its id and email if the source no longer has it (or can't be reached)."""
	try:
		return source.get(row.profile_id)
	except ValueError:
		return {'id': row.profile_id, 'email': row.email}


def plan_signup(
	profiles: list[dict[str, str]] | ProfileSource,
	log: SignupLog,
	site: str,
	profile_id: str | None = None,
	exclude: set[str] | frozenset[str] = frozenset(),
) -> tuple[dict[str, str], Signup, bool]:
	"""Pick the profile for `site`. Returns (profile, signup row, account_exists).

	`profiles` is the profiles.csv rows or a ProfileSource (identities.py: the company identity server).
	A named profile with a row for this site keeps that row: a retry continues as the same person, and a
	profile with an account there never gets a second one. With no profile named, the site's existing
	account (or its last attempt) wins; otherwise the source assigns one not in `exclude` (ids another job of
	the same request already took): a random free profile, or whoever the identity server hands out.
	"""
	source = _source(profiles)
	if profile_id:
		profile = source.get(profile_id)
		prev = log.get(site, profile_id)
		if prev:
			return profile, prev, prev.status in DONE_STATUSES
	else:
		for prev in log.for_site(site):
			if prev.status in DONE_STATUSES:
				return _known(source, prev), prev, True
			if prev.profile_id not in exclude:
				try:
					return source.get(prev.profile_id), prev, False
				except ValueError:
					continue  # that profile is gone: someone else this time
		profile = source.assign(site, 'signup', exclude)
		prev = log.get(site, profile['id'])
		if prev:  # the server handed out someone who already tried here: continue as them
			return profile, prev, prev.status in DONE_STATUSES
	signup = Signup(
		site=site,
		profile_id=profile['id'],
		email=profile['email'],
		password=profile.get('password') or new_password(),
		identity=identity_name(site, profile['id']),
	)
	return profile, signup, False


# --- several accounts at once (the app's "create N accounts" control) --------------------------------
#
# Each account is its own run: its own person (one of the profiles you own, or one the identity server
# hands out), its own fresh browser identity, and its own single-account instruction. The agent is never
# told how many accounts the request makes, so it can't write "3 of 3 done" or skip work for the others.


def _free_profiles(source: ProfileSource, log: SignupLog, site: str) -> list[dict[str, str]]:
	"""The people the source lists who don't have an account on `site` yet. Empty when it lists nobody
	(an identity server may not offer a listing: it can still assign people on demand)."""
	try:
		profiles = source.listing()
	except (ValueError, OSError):
		return []
	return [p for p in profiles if not (row := log.get(site, p['id'])) or row.status not in DONE_STATUSES]


def plan_fresh_signups(
	source: list[dict[str, str]] | ProfileSource, log: SignupLog, site: str, count: int
) -> list[tuple[dict[str, str], Signup | None]]:
	"""Pick `count` people for `count` NEW accounts on `site`: (profile, the row that person already tried with or None).

	Never someone who already has an account there (DONE_STATUSES): that would be a login, not a fresh account.
	A row from an earlier failed attempt is fine: the retry continues as the same person. Raises ValueError with
	what to do when the source can't supply enough people (profiles.csv is finite; the identity server isn't).
	"""
	source = _source(source)
	if count < 1:
		raise ValueError('ask for at least one account')
	if not source.remote:
		# A finite list (profiles.csv): pick from the free ones outright, rather than asking at random and
		# repeatedly drawing people who already have an account there.
		free = _free_profiles(source, log, site)
		if len(free) < count:
			total = len(source.listing())
			raise ValueError(
				f'{site}: {count} new account{"s" if count != 1 else ""} asked for, but only {len(free)} of your {total} profiles '
				f'{"is" if len(free) == 1 else "are"} free there (the rest already have one). Ask for fewer, add a profile under '
				'Accounts, or set VEIL_IDENTITY_API so people are handed out on demand.'
			)
		picked = secrets.SystemRandom().sample(free, count)
		return [(p, log.get(site, p['id'])) for p in picked]
	# The identity server assigns people on demand (its listing, when offered, is not a limit): ask it,
	# skipping anyone who already has an account here.
	taken: set[str] = set()
	out: list[tuple[dict[str, str], Signup | None]] = []
	tries = 0
	while len(out) < count:
		tries += 1
		if tries > count * 5 + 10:  # the source keeps offering people who already have an account here
			raise ValueError(f'{site}: could not find {count} people without an account there (try fewer accounts)')
		try:
			profile = source.assign(site, 'signup', taken)
		except ValueError as e:
			if getattr(e, 'status', None) is not None:
				raise  # the identity server's own refusal: the endpoint reports it as 502
			left = count - len(out)
			raise ValueError(f'{site}: no more people for a new account ({left} short): {e}') from e
		if profile['id'] in taken:  # a source that ignores `exclude`
			continue
		taken.add(profile['id'])
		row = log.get(site, profile['id'])
		if row and row.status in DONE_STATUSES:
			continue  # already has an account there: not a fresh one
		out.append((profile, row))
	return out


# What the person typed that would tell the agent how many accounts the request makes, or that there are
# others at all. Rewritten for a single, fresh sign-up before the task text is built.
_MANY = r'(?:\d{1,3}|a\s+few|several|multiple|two|three|four|five|six|seven|eight|nine|ten)'
_COUNT = rf'(?:{_MANY}|one|an?)'
_ACCOUNT_NOUN = r'(?:accounts?|sign[\s-]?ups?|registrations?)'
_CREATE = r'(?:create|make|open|set\s*up|register|sign\s*up|enrol)'
_MULTIPLE_PEOPLE = re.compile(
	rf'\b(?:for|with|across)\s+{_MANY}\s+(?:different\s+|separate\s+|other\s+|more\s+)?(?:people|persons|profiles|emails|accounts)\b', re.I
)
_ONE_PER = re.compile(r'\b(?:one|1|once)\s+(?:account\s+)?per\s+(?:profiles?|emails?|accounts?)\b', re.I)
_EACH_ACCOUNT = re.compile(r'\b(?:for|on|with)\s+each\s+(?:of\s+)?(?:my\s+|the\s+)?(?:profiles?|emails?|accounts?)\b', re.I)
_EACH_ONE = re.compile(r'\b(?:each|every)\s+(?:one|account)\b', re.I)
_CREATE_ACCOUNTS = re.compile(rf'\b(?P<verb>{_CREATE})\s+(?:me\s+)?{_ACCOUNT_NOUN}\b', re.I)
# A count, up to three words in between ("3 new accounts", "five reddit accounts"), then the noun. The
# words may not be prepositions or conjunctions, so "2 days to create accounts" isn't mistaken for a count.
_GAP_WORD = r'(?!(?:to|and|then|for|on|at|with|in|of|from|by|the)\b)[\w-]+\s+'
_COUNTED_ACCOUNTS = re.compile(rf'\b{_COUNT}\s+(?:{_GAP_WORD}){{0,3}}?{_ACCOUNT_NOUN}\b', re.I)
_TWICE = re.compile(r'\b(twice|thrice)\b', re.I)
_PARALLEL = re.compile(r'\b(?:in\s+parallel|at\s+once|simultaneously|side\s+by\s+side)\b', re.I)
_AS_MANY = re.compile(r'\bas\s+many\s+as\s+(?:possible|i\s+(?:asked|said|want)|you\s+can)\b', re.I)


def per_account(text: str) -> str:
	"""The person's own words reworded for ONE account, so every run reads as a lone, fresh sign-up.

	The count itself never reaches the agent (the app keeps it); this also rewrites any count the person typed
	into the prompt ("create 3 accounts with my work email" -> "create an account with my work email").
	"""
	out = _AS_MANY.sub('', text or '')
	out = _MULTIPLE_PEOPLE.sub('', out)
	out = _ONE_PER.sub('', out)
	out = _EACH_ACCOUNT.sub('', out)
	out = _EACH_ONE.sub('the account', out)
	out = _CREATE_ACCOUNTS.sub(r'\g<verb> an account', out)
	out = _COUNTED_ACCOUNTS.sub('an account', out)
	out = _TWICE.sub('once', out)
	out = _PARALLEL.sub('', out)
	out = re.sub(r'\s+([,.;:!?])', r'\1', out)
	out = re.sub(r'\s{2,}', ' ', out).strip()
	return re.sub(r'[\s,;]+$', '', out)


def sensitive_data(site: str, values: dict[str, str]) -> dict[str, dict[str, str]]:
	"""Scope the values to the site and its subdomains, so a redirect elsewhere can't receive them."""
	return {f'https://*.{site}': values}


def _blocker(ask_human: bool, codes: bool = False) -> str:
	"""codes: veil can read the person's one-time codes and confirmation links itself (identity server or their
	mailbox), so the agent gets get_verification_code / get_verification_link. The instructions never say how."""
	fetch = (
		"- If the site sends a one-time code to x_email or x_phone, call get_verification_code with channel 'email' or 'sms' "
		"(it waits for the code to arrive) and type what it returns. Only if it returns no code, treat it as something only I can do.\n"
		"- If the site says it emailed a link to confirm the address, call get_verification_link, open the link it returns in the "
		"current tab, and finish whatever that page asks. Once it confirms the account, the status is signed_up, not verify_email.\n"
		if codes
		else ''
	)
	if ask_human:
		return fetch + (
			"- If you hit a CAPTCHA, a one-time code sent by email or SMS, or anything else only I can do, call request_human_help "
			"saying exactly what is needed. I either do it in the browser or put the code in my note when I hand back: if my note "
			"has a code, type it into the code field. If it's still not done after that, stop there."
		)
	return fetch + "- If you hit a CAPTCHA, a phone/SMS code, an email code you can't see, or anything else only I can do, stop there."


# Rules for every account run. The failures seen so far: a "Sign up with Apple" popup taking over the flow,
# and the agent hopping between stale tabs (and calling the fresh one blank) after the person entered a code.
_ACCOUNT_RULES = """- Use the site's own email/password form. Never click "Continue with Google / Apple / Facebook / Microsoft" or any other single sign-on button.
- Work in one tab. Don't open new tabs; if a popup or new tab opens anyway, close it and go back to the tab with the form.
- After I hand the browser back, stay on the current tab and continue from what it shows now: I may have entered a code or moved the flow on. If the page looks blank or half-loaded, wait a few seconds before deciding anything."""


def _details(values: dict[str, str]) -> str:
	return f'My details are provided as placeholders ({", ".join(sorted(values))}). Type them with <secret>name</secret>.'


def build_task(
	url: str, values: dict[str, str], ask_human: bool = False, request: str | None = None, if_registered: str = 'stop', codes: bool = False
) -> str:
	"""ask_human: someone is watching (the web UI), so the agent hands CAPTCHAs and codes to them instead of stopping.
	codes: veil can fetch one-time codes and confirmation links for the agent (identity server or mailbox);
	the agent only sees neutral tools.
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
{_blocker(ask_human, codes)}
{registered}

Finish with done. The last line of your answer must be exactly one of:
SIGNUP_STATUS: signed_up - <what you saw that confirms it>
SIGNUP_STATUS: verify_email - <the site asked to confirm via an emailed link>
SIGNUP_STATUS: logged_in - <the account already existed and you are now logged in to it>
SIGNUP_STATUS: needs_human - <what I need to do, and on which page>
SIGNUP_STATUS: already_registered - <the site's message>
SIGNUP_STATUS: failed - <why>"""


def build_login_task(url: str, values: dict[str, str], ask_human: bool = False, request: str | None = None, codes: bool = False) -> str:
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
{_blocker(ask_human, codes)}

Finish with done. The last line of your answer must be exactly one of:
LOGIN_STATUS: logged_in - <what you saw that confirms it, e.g. the home feed or my profile>
LOGIN_STATUS: needs_human - <what I need to do, and on which page>
LOGIN_STATUS: wrong_credentials - <the site's message>
LOGIN_STATUS: no_account - <the site says there is no account for this email>
LOGIN_STATUS: failed - <why>"""


def plan_login(
	profiles: list[dict[str, str]] | ProfileSource, log: SignupLog, site: str, profile_id: str | None = None
) -> tuple[dict[str, str], Signup, bool]:
	"""Which account to log in to on `site`. Returns (profile, row, row_is_logged).

	A row from a veil sign-up gives the password and the sticky identity. Without one, the account was made
	outside veil: the profile's own password is used, on that profile's sticky identity for the site.
	With no profile named and no row, the source picks (profiles.csv: only when it has exactly one profile).
	"""
	source = _source(profiles)
	row = log.get(site, profile_id)
	if row:
		return _known(source, row), row, True
	profile = source.get(profile_id) if profile_id else source.assign(site, 'login')
	profile_id = profile['id']
	row = log.get(site, profile_id)
	if row:
		return profile, row, True
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
