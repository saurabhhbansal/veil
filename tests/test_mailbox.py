"""Tests for veil.mailbox: provider inference, message parsing, code/link extraction and IMAP polling.

No network and no waiting: the IMAP client, the clock and the sleep function are fakes, so the
polling tests finish instantly.
"""
from __future__ import annotations

import base64
import imaplib
import logging
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from email.utils import formatdate

import pytest

from veil.mailbox import (
	POLL_SECONDS,
	WAIT_SECONDS,
	Mailbox,
	MailReader,
	extract_code,
	extract_link,
	mailbox_for,
	message_text,
	provider_for,
)


# --- helpers ------------------------------------------------------------------------------------


def _message(
	body: str | bytes,
	*,
	subject: str = 'Your code',
	sender: str = 'noreply@example.com',
	date: str | None = None,
	content_type: str = 'text/plain; charset=utf-8',
	encoding: str | None = None,
) -> bytes:
	"""A minimal raw email."""
	payload = body.encode('utf-8') if isinstance(body, str) else body
	lines = [f'From: {sender}', 'To: person@example.com', f'Subject: {subject}', 'MIME-Version: 1.0']
	if date is not None:
		lines.append(f'Date: {date}')
	lines.append(f'Content-Type: {content_type}')
	if encoding:
		lines.append(f'Content-Transfer-Encoding: {encoding}')
	if encoding == 'base64':
		payload = base64.b64encode(payload)
	return ('\r\n'.join(lines) + '\r\n\r\n').encode('ascii') + payload


def _date(when: datetime) -> str:
	return formatdate(when.timestamp(), usegmt=True)


def _recent(body: str, **kw) -> bytes:
	"""An email dated now, so readers with an old `since` accept it."""
	return _message(body, date=_date(datetime.now(timezone.utc)), **kw)


def _dated(body: str, when: datetime, **kw) -> bytes:
	return _message(body, date=_date(when), **kw)


def _multipart_alternative(plain: str, html: str) -> bytes:
	boundary = 'veil-boundary'
	head = [
		'From: noreply@reddit.com',
		'To: person@example.com',
		'Subject: Verify your email',
		f'Date: {_date(datetime.now(timezone.utc))}',
		'MIME-Version: 1.0',
		f'Content-Type: multipart/alternative; boundary="{boundary}"',
	]
	body = (
		f'--{boundary}\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n{plain}\r\n'
		f'--{boundary}\r\nContent-Type: text/html; charset=utf-8\r\n\r\n{html}\r\n'
		f'--{boundary}--\r\n'
	)
	return ('\r\n'.join(head) + '\r\n\r\n' + body).encode('utf-8')


def _multipart_with_attachment(body: str, attachment: str) -> bytes:
	boundary = 'veil-mixed'
	head = [
		'From: noreply@example.com',
		'Subject: Your code',
		'Date: ' + _date(datetime.now(timezone.utc)),
		'MIME-Version: 1.0',
		f'Content-Type: multipart/mixed; boundary="{boundary}"',
	]
	parts = (
		f'--{boundary}\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n{body}\r\n'
		f'--{boundary}\r\nContent-Type: text/plain; charset=utf-8\r\n'
		f'Content-Disposition: attachment; filename="notes.txt"\r\n\r\n{attachment}\r\n'
		f'--{boundary}--\r\n'
	)
	return ('\r\n'.join(head) + '\r\n\r\n' + parts).encode('utf-8')


class FakeClock:
	"""A monotonic clock that only moves when the reader sleeps."""

	def __init__(self, start: float = 1000.0):
		self.now = start
		self.sleeps: list[float] = []

	def clock(self) -> float:
		return self.now

	def sleep(self, seconds: float) -> None:
		self.sleeps.append(seconds)
		self.now += seconds


class FakeIMAPClient:
	"""One connection. The fake server decides which messages each poll sees and how FETCH answers."""

	def __init__(self, server: FakeServer, index: int):
		self.server = server
		self.index = index
		self.messages = server.polls[index] if index < len(server.polls) else []
		self.login_calls: list[tuple[str, str]] = []
		self.select_calls: list[tuple[str, bool]] = []
		self.search_calls: list[tuple[object, str]] = []
		self.fetch_calls: list[tuple[str, str]] = []
		self.closed = False
		self.logged_out = False

	def login(self, user: str, password: str):
		self.login_calls.append((user, password))
		if self.server.fail_login or self.index in self.server.fail_login_polls:
			raise imaplib.IMAP4.error('authentication failed')
		return 'OK', [b'logged in']

	def select(self, mailbox: str = 'INBOX', readonly: bool = False):
		self.select_calls.append((mailbox, readonly))
		if self.server.fail_select:
			raise imaplib.IMAP4.error('SELECT failed')
		return 'OK', [b'1']

	def search(self, charset, criteria: str):
		self.search_calls.append((charset, criteria))
		if self.server.fail_search or self.index in self.server.fail_search_polls:
			raise imaplib.IMAP4.error('SEARCH failed')
		return 'OK', [' '.join(str(number) for number in range(1, len(self.messages) + 1)).encode()]

	def fetch(self, ids, parts: str):
		self.fetch_calls.append((str(ids), parts))
		wanted = [piece for piece in str(ids).split(',') if piece]
		payloads: list[tuple[bytes, bytes]] = []
		for message_id in wanted:
			position = int(message_id) - 1
			if 0 <= position < len(self.messages):
				raw = self.messages[position]
				payloads.append((f'{message_id} (BODY[] {{{len(raw)}}}'.encode(), raw))
		shape = self.server.fetch_shape
		if shape == 'list':
			return payloads + [b')']
		if shape == 'flat':
			flat: list[bytes] = []
			for head, raw in payloads:
				flat.extend([head, raw])
			return 'OK', flat
		if shape == 'bare' and payloads:
			return payloads[0]
		return 'OK', payloads + ([b')'] if payloads else [])

	def close(self):
		self.closed = True
		return 'OK', [b'']

	def logout(self):
		self.logged_out = True
		return 'BYE', [b'']


class SingleIDFakeIMAP(FakeIMAPClient):
	"""Accepts only one message id per FETCH: exercises the reader's per-message fallback."""

	def fetch(self, ids, parts: str):
		if ',' in str(ids):
			raise TypeError('this fake fetches one message at a time')
		return super().fetch(ids, parts)


class NoReadonlyIMAP(FakeIMAPClient):
	"""A fake whose select() does not take the readonly keyword."""

	def select(self, mailbox: str = 'INBOX'):
		self.select_calls.append((mailbox, False))
		return 'OK', [b'1']


class PositionalSelectIMAP(FakeIMAPClient):
	"""A fake whose select() only takes mailbox and readonly positionally, without defaults."""

	def select(self, mailbox: str, readonly: bool, /):
		self.select_calls.append((mailbox, readonly))
		return 'OK', [b'1']


class BareSelectIMAP(FakeIMAPClient):
	"""A fake whose select() takes no arguments at all."""

	def select(self):
		self.select_calls.append(('INBOX', False))
		return 'OK', [b'1']


class FakeServer:
	def __init__(
		self,
		polls: list[list[bytes]],
		*,
		fail_login: bool = False,
		fail_login_polls=(),
		fail_search: bool = False,
		fail_search_polls=(),
		fail_select: bool = False,
		fetch_shape: str = 'status',
		client_class=FakeIMAPClient,
	):
		self.polls = polls
		self.fail_login = fail_login
		self.fail_login_polls = set(fail_login_polls)
		self.fail_search = fail_search
		self.fail_search_polls = set(fail_search_polls)
		self.fail_select = fail_select
		self.fetch_shape = fetch_shape
		self.client_class = client_class
		self.attempts = 0
		self.clients: list[FakeIMAPClient] = []
		self.factory_args: list[tuple[str, int]] = []

	def factory(self, host: str, port: int):
		index = self.attempts
		self.attempts += 1
		self.factory_args.append((host, port))
		client = self.client_class(self, index)
		self.clients.append(client)
		return client


def _reader(
	server: FakeServer,
	*,
	timeout: float = 20.0,
	poll: float = POLL_SECONDS,
	mailbox: Mailbox | None = None,
	password: str = 'app-password',
	clock: FakeClock | None = None,
) -> tuple[MailReader, FakeClock]:
	clock = clock or FakeClock()
	reader = MailReader(
		mailbox or Mailbox('gmail', 'imap.gmail.com'),
		'person@gmail.com',
		password,
		imap_factory=server.factory,
		timeout=timeout,
		poll=poll,
		sleep=clock.sleep,
		clock=clock.clock,
	)
	return reader, clock


# --- provider inference and Mailbox -------------------------------------------------------------


@pytest.mark.parametrize(
	('address', 'provider'),
	[
		('person@gmail.com', 'gmail'),
		('person@googlemail.com', 'gmail'),
		('Person@GMAIL.COM', 'gmail'),
		('person@yahoo.com', 'yahoo'),
		('person@yahoo.co.uk', 'yahoo'),
		('person@ymail.com', 'yahoo'),
		('person@rocketmail.com', 'yahoo'),
		('person@outlook.com', 'outlook'),
		('person@hotmail.co.uk', 'outlook'),
		('person@live.com', 'outlook'),
		('person@msn.com', 'outlook'),
		('person@icloud.com', 'icloud'),
		('person@me.com', 'icloud'),
		('person@mac.com', 'icloud'),
		('person@example.com', 'custom'),
		('person@corp.example.org', 'custom'),
		('person@localhost', 'custom'),
		('Reddit Person <person@reddit.com>', 'custom'),
		('not-an-address', 'unknown'),
		('', 'unknown'),
		('person@', 'unknown'),
	],
)
def test_provider_for(address: str, provider: str):
	assert provider_for(address) == provider


@pytest.mark.parametrize(
	('address', 'host'),
	[
		('a@gmail.com', 'imap.gmail.com'),
		('a@googlemail.com', 'imap.gmail.com'),
		('a@yahoo.co.uk', 'imap.mail.yahoo.com'),
		('a@rocketmail.com', 'imap.mail.yahoo.com'),
		('a@outlook.com', 'outlook.office365.com'),
		('a@live.com', 'outlook.office365.com'),
		('a@icloud.com', 'imap.mail.me.com'),
		('a@me.com', 'imap.mail.me.com'),
	],
)
def test_mailbox_for_free_providers(address: str, host: str):
	box = mailbox_for(address)
	assert box is not None
	assert (box.host, box.port) == (host, 993)


def test_mailbox_for_custom_domain_needs_an_imap_host():
	assert mailbox_for('me@custom.test') is None
	assert mailbox_for('me@unknown.test') is None
	assert mailbox_for('not-an-address') is None
	assert mailbox_for('me@custom.test', 'imap.custom.test') == Mailbox('custom', 'imap.custom.test')
	assert mailbox_for('me@custom.test', 'imap.custom.test:143') == Mailbox('custom', 'imap.custom.test', 143)
	assert mailbox_for('me@custom.test', 'imap.custom.test', port=1993) == Mailbox('custom', 'imap.custom.test', 1993)


def test_imap_and_port_override_the_provider_default():
	assert mailbox_for('me@gmail.com', 'imap.fastmail.com') == Mailbox('gmail', 'imap.fastmail.com')
	assert mailbox_for('me@gmail.com', 'imap.fastmail.com:1143') == Mailbox('gmail', 'imap.fastmail.com', 1143)
	assert mailbox_for('me@gmail.com', 'imap.fastmail.com:1143', port=2993) == Mailbox('gmail', 'imap.fastmail.com', 2993)
	assert mailbox_for('me@gmail.com', port=1993) == Mailbox('gmail', 'imap.gmail.com', 1993)
	assert mailbox_for('me@icloud.com', '[::1]:143') == Mailbox('icloud', '::1', 143)


def test_blank_imap_means_the_provider_default():
	assert mailbox_for('me@gmail.com', '   ') == mailbox_for('me@gmail.com')
	assert mailbox_for('me@custom.test', '') is None


@pytest.mark.parametrize(
	'imap',
	[
		'http://mail.example.com',
		'imap host',
		'mail.example.com:notaport',
		'mail.example.com:70000',
		'user@mail.example.com',
		'mail.example.com/path',
		'[::1',
		':993',
	],
)
def test_mailbox_for_refuses_unusable_imap_input(imap: str):
	assert mailbox_for('me@gmail.com', imap) is None
	assert mailbox_for('me@custom.test', imap) is None


@pytest.mark.parametrize('port', [0, 70000, -1])
def test_mailbox_for_refuses_unusable_ports(port: int):
	assert mailbox_for('me@gmail.com', port=port) is None
	assert mailbox_for('me@custom.test', 'imap.custom.test', port=port) is None


def test_mailbox_defaults_and_is_frozen():
	box = Mailbox('gmail', 'imap.gmail.com')
	assert box == Mailbox('gmail', 'imap.gmail.com', 993)
	assert box.port == 993
	with pytest.raises(FrozenInstanceError):
		box.port = 1


# --- message_text -------------------------------------------------------------------------------


def test_message_text_plain_includes_subject_from_and_body():
	raw = _message(
		'Hello there.\nYour code is 482913.',
		subject='Your sign-in code',
		sender='Reddit <noreply@reddit.com>',
		date=_date(datetime.now(timezone.utc)),
	)
	text = message_text(raw)
	assert 'Your sign-in code' in text
	assert 'noreply@reddit.com' in text
	assert '482913' in text
	assert '\n' not in text and '\r' not in text
	assert text == ' '.join(text.split())


def test_message_text_html_only_strips_tags_and_keeps_links():
	html = (
		'<html><head><style>.x { color: red; }</style></head><body>'
		'<table><tr><td>Your code is</td><td><strong>482913</strong></td></tr></table>'
		'<a href="https://reddit.com/verify?token=abc123">Verify email</a>'
		'</body></html>'
	)
	text = message_text(_message(html, content_type='text/html; charset=utf-8', date=_date(datetime.now(timezone.utc))))
	assert '<' not in text and '>' not in text
	assert 'Your code is' in text and '482913' in text
	assert 'Verify email' in text
	assert 'https://reddit.com/verify?token=abc123' in text
	assert extract_code(text) == '482913'
	assert extract_link(text, 'reddit.com') == 'https://reddit.com/verify?token=abc123'


def test_message_text_prefers_the_plain_part_of_a_multipart_message():
	text = message_text(_multipart_alternative('Plain body marker 482913.', '<p>HTML body marker 111111</p>'))
	assert 'Plain body marker' in text
	assert 'HTML body marker' not in text


def test_message_text_decodes_base64():
	raw = _message(
		'Your verification code is 482913.',
		content_type='text/plain; charset=utf-8',
		encoding='base64',
	)
	assert '482913' in message_text(raw)


def test_message_text_falls_back_to_latin1_for_an_unknown_charset():
	raw = _message(b'Caf\xe9: your code is 482913.', content_type='text/plain; charset=x-nonsense')
	text = message_text(raw)
	assert 'Café' in text
	assert '482913' in text
	assert '\ufffd' not in text


def test_message_text_falls_back_to_latin1_for_a_bad_utf8_body():
	raw = _message(b'Caf\xe9: your code is 482913.', content_type='text/plain; charset=utf-8')
	assert 'Café' in message_text(raw)


def test_message_text_decodes_rfc2047_headers():
	subject = '=?utf-8?B?' + base64.b64encode('Vérifiez votre compte'.encode('utf-8')).decode('ascii') + '?='
	text = message_text(_message('Your code is 482913.', subject=subject))
	assert 'Vérifiez votre compte' in text


def test_message_text_ignores_attachments():
	text = message_text(_multipart_with_attachment('Body code 482913.', 'Attachment data 999999.'))
	assert '482913' in text
	assert '999999' not in text


def test_message_text_handles_empty_and_garbage_input():
	assert message_text(b'') == ''
	text = message_text(b'\xff\xfe not really mail')
	assert isinstance(text, str)


# --- extract_code -------------------------------------------------------------------------------


@pytest.mark.parametrize(
	('text', 'expected'),
	[
		('Your verification code is 123456.', '123456'),
		('Your code is 123456', '123456'),
		('Use 482913 to verify your account.', '482913'),
		('Security code: 1234', '1234'),
		('Your one-time passcode is 482913.', '482913'),
		('OTP: 555123', '555123'),
		('Your 2FA code is 4829.', '4829'),
		('Sign-in code: 987654', '987654'),
		('Your activation code is 654321', '654321'),
		('verification code: AB12CD', 'AB12CD'),
		('Your activation code is G-123 456.', '123456'),
		('Your code is 1234 5678.', '12345678'),
		('Enter the 6 digit verification code 482913 to continue.', '482913'),
		('Copyright 2024. Your verification code is 482913.', '482913'),
		('Your account number is 12345678 and your verification code is 482913.', '482913'),
		('Your Reddit confirmation code: 654321', '654321'),
		('Your\n\nverification   code\r\nis 482913', '482913'),
	],
)
def test_extract_code_finds_real_world_codes(text: str, expected: str):
	assert extract_code(text) == expected


def test_extract_code_prefers_the_code_near_the_site_mention():
	filler = 'Thank you for waiting. ' * 10
	text = f'Use 111111 to verify. {filler}According to example.com, your code is 222222.'
	assert extract_code(text) == '111111'
	assert extract_code(text, 'example.com') == '222222'
	assert extract_code(text, 'https://www.example.com/signup') == '222222'


def test_extract_code_uses_a_site_mention_in_the_from_or_subject():
	raw = _recent(
		'Your one-time code is 482913.',
		subject='Verify your Reddit email',
		sender='Reddit <noreply@reddit.com>',
	)
	assert extract_code(message_text(raw), 'reddit.com') == '482913'


def test_extract_code_prefers_digits_when_an_alphanumeric_also_matches():
	text = 'Your verification code is AB12CD. Ignore the code 482913.'
	assert extract_code(text) == '482913'


@pytest.mark.parametrize(
	'text',
	[
		'',
		'The number 482913 is prime.',
		'Your order #12345678 has shipped.',
		'Order confirmation: your order 12345678 is on its way.',
		'The order number 12345678 was confirmed.',
		'Your verification code is 2024.',
		'Call +1 555 123 4567 for verification help.',
		'Your tracking number is 1234567890.',
		'Verify at https://example.com/verify?code=482913',
		'Your ZIP code is 94103.',
		'Promo code: 1234',
		'Your verification code is 1299 USD.',
		'Your verification token is 9f8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c',
		'Your code is 1234567890.',
		'Your code is ABCDEFGH.',
	],
)
def test_extract_code_is_conservative(text: str):
	assert extract_code(text) is None


# --- extract_link -------------------------------------------------------------------------------


@pytest.mark.parametrize(
	('text', 'expected'),
	[
		('Verify your email: https://reddit.com/verify?token=abc123', 'https://reddit.com/verify?token=abc123'),
		('Click to confirm: https://shop.example.com/confirm/abc.', 'https://shop.example.com/confirm/abc'),
		('(<https://example.com/activate?code=1>)', 'https://example.com/activate?code=1'),
		('Open https://example.com/magic-link?key=xyz now', 'https://example.com/magic-link?key=xyz'),
		('See https://example.com/validate?token=abc, then continue', 'https://example.com/validate?token=abc'),
	],
)
def test_extract_link_finds_confirmation_links(text: str, expected: str):
	assert extract_link(text) == expected


def test_extract_link_prefers_the_site_domain():
	text = (
		'Read more: https://other.example.org/verify?token=111 '
		'Confirm here: https://shop.example.com/confirm?token=xyz'
	)
	assert extract_link(text, 'example.com') == 'https://shop.example.com/confirm?token=xyz'


def test_extract_link_works_on_html_only_mail():
	html = '<p>Confirm your email</p><a href="https://reddit.com/confirm?token=abc123">Confirm</a>'
	text = message_text(
		_message(html, content_type='text/html; charset=utf-8', subject='Confirm your Reddit email')
	)
	assert extract_link(text, 'reddit.com') == 'https://reddit.com/confirm?token=abc123'


@pytest.mark.parametrize(
	'text',
	[
		'',
		'Visit https://example.com/blog for news.',
		'Unsubscribe: https://example.com/unsubscribe?token=abc',
		'Manage preferences: https://example.com/preferences/verify?token=abc',
		'Help: https://help.example.com/verify?token=abc',
		'Trouble? https://support.example.com/verify?token=abc',
		'Track it: https://example.com/track/verify?token=abc',
		'Logo: https://example.com/assets/logo.png',
		'Privacy: https://example.com/privacy/verify?token=abc',
	],
)
def test_extract_link_is_conservative(text: str):
	assert extract_link(text) is None


# --- MailReader ---------------------------------------------------------------------------------


def test_constants():
	assert (WAIT_SECONDS, POLL_SECONDS) == (90, 5)


def test_reader_returns_a_code_from_a_later_poll():
	code_mail = _recent('Your verification code is 482913.')
	server = FakeServer([[], [code_mail]])
	reader, _ = _reader(server)
	assert reader.code('reddit.com', since=0.0) == '482913'
	assert server.attempts == 2
	assert server.factory_args[0] == ('imap.gmail.com', 993)
	first, second = server.clients
	assert first.closed and first.logged_out
	assert second.closed and second.logged_out


def test_reader_returns_a_confirmation_link():
	mail = _recent(
		'Confirm your email: https://reddit.com/confirm?token=abc123',
		subject='Confirm your Reddit email',
		sender='Reddit <noreply@reddit.com>',
	)
	server = FakeServer([[mail]])
	reader, _ = _reader(server)
	assert reader.link('reddit.com', since=0.0) == 'https://reddit.com/confirm?token=abc123'


def test_reader_checks_the_newest_message_first():
	older = _recent('Your verification code is 111111.')
	newer = _recent('Your verification code is 222222.')
	server = FakeServer([[older, newer]])
	reader, _ = _reader(server)
	assert reader.code('', since=0.0) == '222222'


def test_reader_does_not_reconsider_seen_messages():
	welcome = _recent('Welcome to Reddit. Thanks for joining.', subject='Welcome')
	code_mail = _recent('Your verification code is 482913.')
	server = FakeServer([[welcome], [welcome, code_mail]])
	reader, _ = _reader(server)
	assert reader.code('', since=0.0) == '482913'
	assert server.clients[0].fetch_calls[0][0] == '1'
	assert server.clients[1].fetch_calls[0][0] == '2'  # the welcome mail is not fetched a second time


def test_reader_returns_none_when_nothing_arrives():
	server = FakeServer([[]])
	reader, clock = _reader(server, timeout=10.0)
	assert reader.code('', since=0.0) is None
	assert server.attempts == 2
	assert clock.now == 1010.0


def test_reader_keeps_polling_after_a_search_error():
	mail = _recent('Your verification code is 482913.')
	server = FakeServer([[], [mail]], fail_search_polls={0})
	reader, _ = _reader(server)
	assert reader.code('', since=0.0) == '482913'
	assert server.attempts == 2


def test_reader_returns_none_after_a_login_failure_until_timeout():
	server = FakeServer([], fail_login=True)
	reader, clock = _reader(server, timeout=WAIT_SECONDS)
	assert reader.code('', since=0.0) is None
	assert server.attempts == 18  # polls at t=0, 5, ..., 85 with a 90 s timeout
	assert clock.sleeps == [float(POLL_SECONDS)] * 18
	assert clock.now == 1000.0 + WAIT_SECONDS
	assert server.clients[-1].closed and server.clients[-1].logged_out


def test_reader_closes_the_connection_when_select_fails():
	server = FakeServer([[]], fail_select=True)
	reader, _ = _reader(server, timeout=5.0)
	assert reader.code('', since=0.0) is None
	assert all(client.closed and client.logged_out for client in server.clients)


def test_reader_uses_a_readonly_inbox_body_peek_and_a_since_search():
	since = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc).timestamp()
	mail = _dated('Your verification code is 482913.', datetime(2026, 11, 18, 12, 1, tzinfo=timezone.utc))
	server = FakeServer([[mail]])
	reader, _ = _reader(server)
	assert reader.code('', since=since) == '482913'
	client = server.clients[0]
	assert client.select_calls == [('INBOX', True)]
	assert [parts for _, parts in client.fetch_calls] == ['(BODY.PEEK[])']
	assert client.search_calls[0][1] == '(SINCE 18-Nov-2026)'


def test_reader_allows_two_minutes_of_clock_skew_in_the_search():
	since = datetime(2026, 11, 18, 0, 1, 30, tzinfo=timezone.utc).timestamp()
	mail = _dated('Your verification code is 482913.', datetime(2026, 11, 18, 0, 1, 0, tzinfo=timezone.utc))
	server = FakeServer([[mail]])
	reader, _ = _reader(server)
	assert reader.code('', since=since) == '482913'
	assert server.clients[0].search_calls[0][1] == '(SINCE 17-Nov-2026)'  # two minutes before midnight


def test_reader_ignores_mail_older_than_since():
	since = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
	stale = _dated('Your verification code is 111111.', since - timedelta(hours=2))
	server = FakeServer([[stale]])
	reader, _ = _reader(server, timeout=10.0)
	assert reader.code('', since=since.timestamp()) is None
	assert server.attempts == 2


def test_reader_accepts_a_message_without_a_date_header():
	mail = _message('Your verification code is 482913.')  # no Date: the server-side SINCE search decided
	server = FakeServer([[mail]])
	reader, _ = _reader(server)
	assert reader.code('', since=0.0) == '482913'


@pytest.mark.parametrize('shape', ['status', 'list', 'flat', 'bare'])
def test_reader_parses_common_fetch_response_shapes(shape: str):
	mail = _recent('Your verification code is 482913.')
	server = FakeServer([[mail]], fetch_shape=shape)
	reader, _ = _reader(server)
	assert reader.code('', since=0.0) == '482913'


def test_reader_falls_back_to_one_fetch_per_message():
	first = _recent('Your verification code is 111111.')
	second = _recent('Your verification code is 222222.')
	server = FakeServer([[first, second]], client_class=SingleIDFakeIMAP)
	reader, _ = _reader(server)
	assert reader.code('', since=0.0) == '222222'
	assert [ids for ids, _ in server.clients[0].fetch_calls] == ['2', '1']


@pytest.mark.parametrize('client_class', [NoReadonlyIMAP, PositionalSelectIMAP, BareSelectIMAP])
def test_reader_tolerates_select_without_readonly_support(client_class):
	mail = _recent('Your verification code is 482913.')
	server = FakeServer([[mail]], client_class=client_class)
	reader, _ = _reader(server)
	assert reader.code('', since=0.0) == '482913'
	assert server.clients[0].select_calls == [('INBOX', False)]


def test_reader_with_a_zero_timeout_still_polls_once():
	mail = _recent('Your verification code is 482913.')
	server = FakeServer([[mail]])
	reader, clock = _reader(server, timeout=0.0)
	assert reader.code('', since=0.0) == '482913'
	assert server.attempts == 1
	assert clock.sleeps == []


def test_reader_never_logs_the_password(caplog):
	caplog.set_level(logging.DEBUG)
	server = FakeServer([], fail_login=True)
	reader, _ = _reader(server, timeout=WAIT_SECONDS, password='s3cret-app-pass')
	assert reader.code('', since=0.0) is None
	assert 's3cret-app-pass' not in caplog.text
	warnings = [record for record in caplog.records if record.levelno >= logging.WARNING]
	assert len(warnings) == 1  # a repeated failure is logged once, not once per poll


def test_reader_never_logs_the_code(caplog):
	caplog.set_level(logging.DEBUG)
	mail = _recent('Your verification code is 482913.')
	server = FakeServer([[mail]])
	reader, _ = _reader(server)
	assert reader.code('', since=0.0) == '482913'
	assert '482913' not in caplog.text
