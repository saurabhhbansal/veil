"""Review a finished run: what went wrong and what to change, for the person and for the next run's agent.

Opt-in (the "Review runs" toggle in the app, or "Review this run" on a past run). One LLM call over a compact
text transcript of the run (task, identities, each step's goal / actions / self-evaluation, blocks, human
handoffs, steers, result). Screenshots aren't sent. Sign-up runs only ever contain placeholders, never the
user's details, so the review doesn't either.
"""
from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

if TYPE_CHECKING:
	from browser_use.llm.base import BaseChatModel

MAX_TRANSCRIPT = 24_000  # characters; the oldest steps are trimmed first


class Judgement(BaseModel):
	verdict: Literal['success', 'partial', 'failure']
	summary: str = Field(description='One or two sentences: what happened.')
	what_went_wrong: list[str] = Field(description='Concrete problems, in order of impact. Empty if none.')
	fixes: list[str] = Field(description='Changes that would likely fix them: to the task wording, settings, or the approach.')
	advice_for_next_run: str = Field(description='Short direct instructions for the agent next time, written to the agent.')


_PROMPT = """You review runs of a browser automation agent (browser-use) driving a stealth browser for a person.
Judge the run below against the task. Be specific and practical: name the step, page or action where things went wrong.
Possible causes include: the agent's choices (wrong element, loops, gave up early, didn't verify), site defences
(bot blocks, CAPTCHAs), the task being ambiguous, missing details, or settings (too few steps, no vision).
Fixes can be for the person (task wording, settings) or for the agent (approach). advice_for_next_run is read by
the agent on the person's next task, so write it as instructions to the agent and keep it under 120 words.
If the run went fine, say so and keep the lists short or empty."""


def _clip(v: Any, n: int = 300) -> str:
	s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
	return s if len(s) <= n else s[:n] + '…'


def transcript(events: list[dict[str, Any]]) -> str:
	"""A compact text version of one run's events (as runner.py records them)."""
	lines: list[str] = []
	for e in events:
		t = e.get('type')
		if t == 'task':
			lines.append(f'TASK: {e.get("text", "")}')
		elif t == 'attempt':
			ident = e.get('identity') or {}
			ex, dev = ident.get('exit') or {}, ident.get('device') or {}
			lines.append(
				f'IDENTITY {ident.get("attempt")}/{ident.get("total")}: engine {ident.get("engine")}, exit {ex.get("ip", "?")} '
				f'{ex.get("country", "")}, device {dev.get("windows", "")} {dev.get("gpu", "")}'
			)
		elif t == 'step':
			acts = '; '.join(f'{a.get("name")} {_clip(a.get("args"), 160)}' for a in e.get('actions') or [])
			lines.append(f'STEP {e.get("n")} at {e.get("url", "")}')
			for label, key in (('  eval', 'evaluation'), ('  memory', 'memory'), ('  goal', 'next_goal')):
				if e.get(key):
					lines.append(f'{label}: {_clip(e[key])}')
			if acts:
				lines.append(f'  actions: {acts}')
		elif t == 'notice':
			lines.append(f'{str(e.get("level", "info")).upper()}: {e.get("text", "")}')
		elif t == 'human':
			who = 'person took over' if e.get('source') == 'user' else f'asked the person for help: {e.get("reason", "")}'
			lines.append(f'HANDOFF: {who} ({e.get("outcome", "open")}){" note: " + e["note"] if e.get("note") else ""}')
		elif t == 'steer':
			lines.append(f'PERSON STEERED: {e.get("text", "")}')
		elif t == 'result':
			state = 'stopped by the person' if e.get('stopped') else ('finished OK' if e.get('ok') else 'did not finish')
			lines.append(f'RESULT: {state}. Output: {_clip(e.get("output") or "(none)", 1500)}')
			for i, a in enumerate(e.get('attempts') or [], 1):
				lines.append(f'  attempt {i}: {a.get("outcome")} {_clip(a.get("detail") or "", 200)}')
	text = '\n'.join(lines)
	if len(text) > MAX_TRANSCRIPT:  # keep the task, the start and the end
		head, tail = text[: MAX_TRANSCRIPT // 3], text[-(2 * MAX_TRANSCRIPT // 3):]
		text = head + '\n…(middle steps trimmed)…\n' + tail
	return text


async def judge_run(llm: BaseChatModel, events: list[dict[str, Any]], timeout: float = 120) -> Judgement:
	from browser_use.llm.messages import SystemMessage, UserMessage

	res = await asyncio.wait_for(
		llm.ainvoke([SystemMessage(content=_PROMPT), UserMessage(content=transcript(events))], output_format=Judgement), timeout
	)
	return res.completion


def to_markdown(j: Judgement, title: str = '') -> str:
	parts = [f'# Run review{": " + title if title else ""}', f'**Verdict:** {j.verdict}', j.summary]
	if j.what_went_wrong:
		parts.append('## What went wrong\n' + '\n'.join(f'- {x}' for x in j.what_went_wrong))
	if j.fixes:
		parts.append('## Possible fixes\n' + '\n'.join(f'- {x}' for x in j.fixes))
	if j.advice_for_next_run:
		parts.append('## Passed to the next run\n' + j.advice_for_next_run)
	return '\n\n'.join(parts) + '\n'


def next_run_message(j: dict[str, Any], previous_task: str) -> str:
	"""What the next run's agent reads (appended to browser-use's system prompt)."""
	lines = [f'A review of your previous run in this conversation (task: "{_clip(previous_task, 200)}", verdict: {j.get("verdict")}):']
	if j.get('what_went_wrong'):
		lines.append('What went wrong: ' + ' | '.join(j['what_went_wrong'][:5]))
	if j.get('advice_for_next_run'):
		lines.append('Advice: ' + j['advice_for_next_run'])
	lines.append('Apply what is relevant to the new task; ignore it if the task is unrelated.')
	return '\n'.join(lines)
