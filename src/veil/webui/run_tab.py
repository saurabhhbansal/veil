# Run tab adapted from browser-use/web-ui (src/webui/components/browser_use_agent_tab.py;
# MIT License, Copyright (c) 2024 Browser Use Inc.; see LICENSE-web-ui). Same chat + task box + Stop/Pause/Clear/
# Submit layout and live browser view; the agent/browser glue is replaced by veil's AgentRunner, and the
# long-lived generator loop by a 1 s gr.Timer poll so any open browser tab can watch the run.
"""Run Agent tab: chat log of steps, the identity in use, a live view of the stealth browser, and controls."""
from __future__ import annotations

import io

import gradio as gr
from PIL import Image

from veil.config import Settings
from veil.webui.manager import WebuiManager, settings_from_ui
from veil.webui.runner import AgentRunner, RunRequest

STATUS_TEXT = {
	'idle': '⚪ Idle',
	'starting': '🟡 Launching identity (proxy → geo → device → Fortress)…',
	'running': '🟢 Running',
	'paused': '⏸️ Paused (the browser stays open)',
	'stopping': '🟠 Stopping after the current step…',
}


def _controls(status: str) -> list:
	busy = status != 'idle'
	return [
		gr.update(interactive=not busy, placeholder='Agent is running…' if busy else 'Describe the task, e.g. "find the cheapest 1TB NVMe on amazon.in"'),
		gr.update(value='⏳ Running…' if busy else '▶️ Submit Task', interactive=not busy),
		gr.update(interactive=status in ('starting', 'running', 'paused')),
		gr.update(value='▶️ Resume' if status == 'paused' else '⏸️ Pause', interactive=status in ('running', 'paused')),
		gr.update(interactive=not busy),
	]


def create_run_tab(manager: WebuiManager, runner: AgentRunner, env: Settings) -> None:
	with gr.Row(equal_height=False):
		with gr.Column(scale=3):
			chatbot = gr.Chatbot(runner.messages, label='Agent Interaction', height=640, buttons=['copy'], elem_id='veil_chat')
			user_input = gr.Textbox(label='Your Task', lines=3, placeholder='Describe the task, e.g. "find the cheapest 1TB NVMe on amazon.in"')
			with gr.Row():
				stop_button = gr.Button('⏹️ Stop', interactive=False, variant='stop', scale=2)
				pause_button = gr.Button('⏸️ Pause', interactive=False, variant='secondary', scale=2)
				clear_button = gr.Button('🗑️ Clear', variant='secondary', scale=2)
				run_button = gr.Button('▶️ Submit Task', variant='primary', scale=3)
		with gr.Column(scale=2):
			status = gr.Markdown(STATUS_TEXT[runner.status])
			live_on = gr.Checkbox(value=True, label='Live browser view', info='A screenshot of the stealth browser every second')
			browser_view = gr.Image(label='Browser Live View', type='pil', interactive=False, height=380, buttons=[])
			with gr.Accordion('Current identity', open=True):
				identity_md = gr.Markdown(runner.identity_md)
			with gr.Accordion('Task outputs', open=False):
				result_file = gr.File(label='Result JSON', interactive=False)
				recording_gif = gr.Image(label='Task Recording GIF', type='filepath', interactive=False)

	manager.add_components('run', dict(user_input=user_input))
	controls = [user_input, run_button, stop_button, pause_button, clear_button]
	seen = gr.State((-1, ''))  # (runner.version, status) this browser tab last rendered

	# The settings components are registered before this tab is built.
	settings_inputs = set(manager.get_components())

	async def submit(components):
		values = manager.values_by_id(components)
		task = (values.get('run.user_input') or '').strip()
		if not task:
			gr.Warning('Please enter a task.')
			return
		if runner.busy:
			gr.Info('A task is already running. Stop it first.')
			return
		try:
			settings, identity, max_steps, agent_kwargs = settings_from_ui(values, env)
			runner.start(RunRequest(task=task, settings=settings, identity=identity, max_steps=max_steps, agent_kwargs=agent_kwargs))
		except Exception as e:
			gr.Warning(f'Could not start: {e}')

	run_button.click(submit, inputs=settings_inputs)
	user_input.submit(submit, inputs=settings_inputs)
	stop_button.click(runner.stop)
	pause_button.click(runner.pause_resume)

	async def clear():
		await runner.clear()

	clear_button.click(clear)

	async def tick(last, live):
		version, st = runner.version, runner.status
		shot = await runner.screenshot() if live else None
		view = Image.open(io.BytesIO(shot)) if shot else gr.skip()
		if (version, st) == tuple(last):
			return [gr.skip()] * (5 + len(controls)) + [view, last]
		return [
			gr.update(value=runner.messages),
			STATUS_TEXT.get(st, st),
			runner.identity_md,
			gr.update(value=runner.result_path),
			gr.update(value=runner.gif_path if st == 'idle' else None),  # the GIF is written when the agent finishes
			*_controls(st),
			view,
			(version, st),
		]

	timer = gr.Timer(1.0)
	timer.tick(
		tick,
		inputs=[seen, live_on],
		outputs=[chatbot, status, identity_md, result_file, recording_gif, *controls, browser_view, seen],
		show_progress='hidden',
	)
