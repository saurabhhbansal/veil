"""The layer between the agent and the human checks a site puts in front of an account run.

Two things happen here that the agent is never told about:

* an emailed one-time code or confirmation link is read from the person's own mailbox over IMAP
  (identities.LocalProfiles.code_fetcher; the agent sees the same neutral get_verification_code tool), and
* a CAPTCHA / "verify you are human" widget is solved by a token service (veil.captcha) and the token is put
  where the site's form reads it.

The agent keeps its plain task and only ever gets a neutral result ("the check passed" / "the code is ..."),
so a guardrailed model never has to agree to reading mail or solving CAPTCHAs: veil does that part. The person
is asked only when the automatic way fails or is not configured.
"""
from __future__ import annotations

import asyncio
import base64
import inspect
import json
import logging
import re
from typing import Any, Callable

from veil import captcha
from veil.config import Settings
from veil.detection import BlockSignal, classify, find_captcha
from veil.session import StealthSession, capture_region, evaluate_isolated, evaluate_page

log = logging.getLogger('veil.autopilot')

_CODE_WORDS = re.compile(r'\b(code|otp|one[- ]time|verification|2fa|two[- ]factor|passcode|confirm)\b', re.I)
_SMS_WORDS = re.compile(r'\b(sms|text message|phone|mobile)\b', re.I)
_CAPTCHA_WORDS = re.compile(r'\b(captcha|human|challenge|robot|bot[- ]check|verify)\b', re.I)


class AutoPilot:
	"""Per-attempt helper: try the invisible way first, fall back to the person through request_human."""

	def __init__(
		self,
		session: StealthSession,
		settings: Settings,
		*,
		fetch_code: Callable[[str], Any] | None = None,
		notify: Callable[[str, str], Any] | None = None,
	):
		self.session = session
		self.settings = settings
		self.fetch_code = fetch_code
		self.notify = notify
		self.solver = (
			captcha.make_solver(settings.captcha_provider, settings.captcha_api_key, timeout=settings.captcha_timeout)
			if settings.captcha_max_solves > 0
			else None
		)
		self._solves: dict[tuple[str, str], int] = {}  # (kind, page) -> paid solves this attempt

	async def _say(self, level: str, text: str) -> None:
		"""A notice for the person watching; the agent never sees it."""
		log.info('autopilot: %s', text)
		if self.notify is None:
			return
		try:
			res = self.notify(level, text)
			if inspect.isawaitable(res):
				await res
		except Exception as e:  # a UI hiccup must never break the run
			log.debug('autopilot notice failed: %s', e)

	# --- CAPTCHAs ------------------------------------------------------------------------------

	async def try_page(self, page: dict[str, Any] | None, signal: BlockSignal | None = None) -> bool:
		"""Try to clear the CAPTCHA/challenge on `page` without the person. True when the page is fit to continue."""
		if self.solver is None or not page:
			return False
		widget = captcha.detect(page.get('html') or '', page.get('url') or '')
		if widget is None:
			return False
		if widget.kind == 'geetest':  # no token to replay for GeeTest: don't pay for a solve we can't inject
			await self._say('blocked', 'A GeeTest challenge cannot be filled from a token; asking you.')
			return False
		key = (widget.kind, (page.get('url') or '').split('?')[0].split('#')[0])
		if self._solves.get(key, 0) >= self.settings.captcha_max_solves:
			return False
		self._solves[key] = self._solves.get(key, 0) + 1
		if widget.kind == 'image':
			if not await self._solve_image(widget):
				return False
			await self._say('info', 'Filled an image CAPTCHA automatically.')
			return True
		await self._say('info', f'Solving a {widget.kind.replace("_", " ")} automatically.')
		token = await self.solver.solve(widget)
		if not token:
			await self._say('blocked', 'The CAPTCHA could not be solved automatically; it will go to you.')
			return False
		js = captcha.injection_js(widget, token)
		if not js:
			return False
		try:
			await evaluate_page(self.session.browser, js)  # main world, so widgets' own callbacks see the token
		except Exception as e:
			log.warning('captcha token injection failed: %s', e)
			return False
		# The site's script validates the token and usually reloads the flow; give it a moment and look again.
		for _ in range(4):
			await asyncio.sleep(1.5)
			fresh = await self.session.probe_page()
			if not fresh:
				continue
			fresh_signal = classify(fresh.get('url') or '', fresh.get('title') or '', fresh.get('html') or '')
			if find_captcha(fresh.get('html') or '') is None and not (fresh_signal and fresh_signal.kind == 'challenge'):
				await self._say('info', 'The human-verification check cleared.')
				return True
		await self._say('blocked', 'The CAPTCHA was solved but the page did not clear; it will go to you.')
		return False

	async def _solve_image(self, widget: captcha.Widget) -> bool:
		"""Screenshot the image CAPTCHA, have the solver read it, and type the answer into its input."""
		if not widget.selector:
			return False
		try:
			rect = await evaluate_isolated(
				self.session.browser,
				f'(() => {{ const el = document.querySelector({json.dumps(widget.selector)}); if (!el) return null; '
				f'const r = el.getBoundingClientRect(); if (!r.width || !r.height) return null; '
				f'return {{x: r.x, y: r.y, width: r.width, height: r.height}}; }})()',
			)
		except Exception as e:
			log.debug('image CAPTCHA rectangle failed: %s', e)
			return False
		if not isinstance(rect, dict) or rect.get('width', 0) < 8 or rect.get('height', 0) < 8:
			return False
		try:
			png = await capture_region(self.session.browser, **rect)
		except Exception as e:
			log.warning('image CAPTCHA screenshot failed: %s', e)
			return False
		answer = await self.solver.solve_image(base64.b64encode(png).decode('ascii'))
		if not answer:
			return False
		try:
			await evaluate_isolated(self.session.browser, captcha.image_entry_js(widget, answer))
		except Exception as e:
			log.warning('image CAPTCHA entry failed: %s', e)
			return False
		return True

	# --- human requests the agent made ---------------------------------------------------------

	async def help(self, reason: str, detail: dict[str, Any]) -> str | None:
		"""Resolve a request_human_help the agent made, invisibly. Returns the neutral result for the agent, or None
		when the person really is needed."""
		text = f'{reason} {detail.get("kind") or ""} {detail.get("vendor") or ""}'
		if _CAPTCHA_WORDS.search(text):
			page = await self.session.probe_page()
			if page and await self.try_page(page, classify(page.get('url') or '', page.get('title') or '', page.get('html') or '')):
				return 'The human-verification check passed. Continue.'
		if self.fetch_code and _CODE_WORDS.search(text):
			channel = 'sms' if _SMS_WORDS.search(text) else 'email'
			try:
				code = await self.fetch_code(channel)
			except Exception as e:
				log.warning('verification code lookup failed: %s', e)
				code = None
			if code:
				await self._say('info', f'Read the {channel} verification code from the account itself; the person was not asked.')
				return f'The {channel} verification code is {code}'
		return None
