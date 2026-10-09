"""Interactive live view of the agent's browser: screencast frames out, the person's mouse and keyboard in.

A second CDP connection to the same browser, separate from browser-use's: cdp_use keeps one handler per event
per connection, so registering screencast frames on browser-use's client could clobber its own handlers.
It follows whichever tab the agent is focused on. Nothing here is visible to page scripts: no Runtime.enable,
the screencast is a compositor feature, and input arrives as ordinary trusted events.

Coordinates come in as fractions of the frame (0..1), so the viewer never needs to know the CSS viewport.
"""
from __future__ import annotations

import asyncio
import base64
import logging
from dataclasses import dataclass
from typing import Any

from cdp_use import CDPClient

log = logging.getLogger('veil.webui.live')

MOUSE_TYPES = {'mousePressed', 'mouseReleased', 'mouseMoved'}
BUTTONS = {'left', 'middle', 'right', 'back', 'forward', 'none'}
# Ctrl/Cmd + key -> Chromium editing command; CDP key events alone don't trigger these.
EDIT_COMMANDS = {'a': 'selectAll', 'c': 'copy', 'x': 'cut', 'z': 'undo', 'y': 'redo'}
CTRL, META = 2, 4


@dataclass
class Frame:
	jpeg: bytes
	width: float  # CSS viewport size the frame shows
	height: float


def _num(v: Any, lo: float, hi: float, default: float = 0.0) -> float:
	try:
		return min(hi, max(lo, float(v)))
	except (TypeError, ValueError):
		return default


def key_event_params(ev: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
	"""A viewer key event -> the CDP calls to make, as (method, params). Pure, for testing."""
	kind = ev.get('type')
	if kind not in ('keyDown', 'keyUp'):
		return []
	key, code = str(ev.get('key') or '')[:32], str(ev.get('code') or '')[:32]
	modifiers = int(_num(ev.get('modifiers'), 0, 15))
	keycode = int(_num(ev.get('keyCode'), 0, 255))
	params: dict[str, Any] = {'key': key, 'code': code, 'modifiers': modifiers, 'windowsVirtualKeyCode': keycode, 'nativeVirtualKeyCode': keycode}
	if kind == 'keyUp':
		return [('Input.dispatchKeyEvent', {'type': 'keyUp', **params})]
	command = EDIT_COMMANDS.get(key.lower()) if modifiers & (CTRL | META) and len(key) == 1 else None
	text = '' if modifiers & (CTRL | META) else ('\r' if key == 'Enter' else key if len(key) == 1 else '')
	if text:
		return [('Input.dispatchKeyEvent', {'type': 'keyDown', 'text': text, 'unmodifiedText': text, **params})]
	return [('Input.dispatchKeyEvent', {'type': 'rawKeyDown', **params, **({'commands': [command]} if command else {})})]


class LiveView:
	def __init__(self, browser_session):
		self.browser = browser_session  # browser-use BrowserSession: we read its focused tab and CDP URL
		self.client: CDPClient | None = None
		self.target_id: str | None = None
		self.session_id: str | None = None
		self.width = self.height = 0.0
		self.url = self.title = ''
		self._frames: asyncio.Queue[Frame] = asyncio.Queue(maxsize=1)
		self._lock = asyncio.Lock()
		self._acks: set[asyncio.Task] = set()

	async def start(self) -> None:
		url = self.browser.cdp_url  # browser-use resolved it to the browser's ws:// endpoint on start
		if not url:
			raise RuntimeError('the browser has no CDP endpoint')
		self.client = CDPClient(url)
		await self.client.start()
		self.client.register.Page.screencastFrame(self._on_frame)
		await self.follow()

	async def close(self) -> None:
		client, self.client = self.client, None
		if not client:
			return
		try:
			if self.session_id:
				await asyncio.wait_for(client.send.Page.stopScreencast(session_id=self.session_id), timeout=2)
		except Exception:
			pass
		try:
			await client.stop()
		except Exception as e:
			log.debug('live view close: %s', e)

	async def follow(self) -> bool:
		"""Attach to the agent's focused tab if it changed. True when the view switched tabs."""
		target = self.browser.agent_focus_target_id
		if not self.client or not target or target == self.target_id:
			return False
		async with self._lock:
			old = self.session_id
			res = await self.client.send.Target.attachToTarget(params={'targetId': target, 'flatten': True})
			self.target_id, self.session_id = target, res['sessionId']
			await self.client.send.Page.startScreencast(
				params={'format': 'jpeg', 'quality': 70, 'maxWidth': 1920, 'maxHeight': 1920, 'everyNthFrame': 1},
				session_id=self.session_id,
			)
			if old:
				try:
					await self.client.send.Target.detachFromTarget(params={'sessionId': old})
				except Exception:
					pass  # the old tab may already be gone
		await self.refresh_info()
		return True

	async def refresh_info(self) -> None:
		if not self.client or not self.target_id:
			return
		try:
			info = (await self.client.send.Target.getTargetInfo(params={'targetId': self.target_id}))['targetInfo']
			self.url, self.title = info.get('url', ''), info.get('title', '')
		except Exception as e:
			log.debug('target info: %s', e)

	async def _ack(self, frame_session: int, session_id: str) -> None:
		try:
			if self.client:
				await self.client.send.Page.screencastFrameAck(params={'sessionId': frame_session}, session_id=session_id)
		except Exception as e:
			log.debug('screencast ack: %s', e)

	def _on_frame(self, event: dict, session_id: str | None) -> None:
		if session_id != self.session_id or not self.client:
			return
		# cdp_use runs handlers inside its read loop: awaiting a reply here would deadlock it.
		task = asyncio.create_task(self._ack(event['sessionId'], session_id))
		self._acks.add(task)
		task.add_done_callback(self._acks.discard)
		meta = event.get('metadata') or {}
		self.width, self.height = float(meta.get('deviceWidth') or 0), float(meta.get('deviceHeight') or 0)
		frame = Frame(base64.b64decode(event['data']), self.width, self.height)
		if self._frames.full():  # a slow viewer only ever gets the newest frame
			self._frames.get_nowait()
		self._frames.put_nowait(frame)

	async def next_frame(self, timeout: float) -> Frame | None:
		try:
			return await asyncio.wait_for(self._frames.get(), timeout)
		except asyncio.TimeoutError:
			return None

	# --- input -------------------------------------------------------------------------------

	async def _send(self, method: str, params: dict[str, Any]) -> None:
		if not self.client or not self.session_id:
			return
		await self.client.send_raw(method, params, session_id=self.session_id)

	async def dispatch(self, ev: dict[str, Any]) -> None:
		"""One input event from the viewer. Unknown or malformed events are ignored."""
		kind = ev.get('t')
		if kind == 'mouse' and ev.get('type') in MOUSE_TYPES and self.width:
			button = ev.get('button') if ev.get('button') in BUTTONS else 'none'
			await self._send('Input.dispatchMouseEvent', {
				'type': ev['type'], 'x': _num(ev.get('x'), 0, 1) * self.width, 'y': _num(ev.get('y'), 0, 1) * self.height,
				'button': button, 'buttons': int(_num(ev.get('buttons'), 0, 31)), 'clickCount': int(_num(ev.get('clickCount'), 0, 3)),
				'modifiers': int(_num(ev.get('modifiers'), 0, 15)),
			})
		elif kind == 'wheel' and self.width:
			await self._send('Input.dispatchMouseEvent', {
				'type': 'mouseWheel', 'x': _num(ev.get('x'), 0, 1) * self.width, 'y': _num(ev.get('y'), 0, 1) * self.height,
				'deltaX': _num(ev.get('dx'), -5000, 5000), 'deltaY': _num(ev.get('dy'), -5000, 5000),
				'modifiers': int(_num(ev.get('modifiers'), 0, 15)),
			})
		elif kind == 'key':
			for method, params in key_event_params(ev):
				await self._send(method, params)
		elif kind == 'text' and isinstance(ev.get('text'), str):
			await self._send('Input.insertText', {'text': ev['text'][:10_000]})
		elif kind == 'nav':
			await self._navigate(ev)

	async def _navigate(self, ev: dict[str, Any]) -> None:
		action = ev.get('action')
		if action == 'reload':
			await self._send('Page.reload', {})
		elif action == 'goto':
			url = str(ev.get('url') or '').strip()
			if url and '://' not in url:
				url = 'https://' + url
			if url.startswith(('http://', 'https://')):
				await self._send('Page.navigate', {'url': url})
		elif action in ('back', 'forward') and self.client and self.session_id:
			hist = await self.client.send.Page.getNavigationHistory(session_id=self.session_id)
			i = hist['currentIndex'] + (-1 if action == 'back' else 1)
			if 0 <= i < len(hist['entries']):
				await self._send('Page.navigateToHistoryEntry', {'entryId': hist['entries'][i]['id']})
