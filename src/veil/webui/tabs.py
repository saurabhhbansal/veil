# Settings and config tabs adapted from browser-use/web-ui (src/webui/components/agent_settings_tab.py,
# browser_settings_tab.py, load_save_config_tab.py; MIT License, Copyright (c) 2024 Browser Use Inc.; see LICENSE-web-ui).
# web-ui's browser options (binary path, user data dir, window size, disable security) are replaced by veil's
# stealth options: those would either bypass Fortress or reshape the persona's fingerprint.
"""Agent Settings, Stealth & Network, and Load & Save Config tabs."""
from __future__ import annotations

import gradio as gr

from veil.config import Settings
from veil.webui.manager import WebuiManager

PROVIDERS = ['openai', 'groq', 'anthropic', 'google', 'openai-compatible']
ENGINES = ['fortress', 'fortress-docker', 'cdp', 'chromium']


def saved_identities(settings: Settings) -> list[str]:
	root = settings.data_dir / 'identities'
	return sorted(p.parent.name for p in root.glob('*/identity.json')) if root.exists() else []


def create_agent_settings_tab(manager: WebuiManager, env: Settings) -> None:
	with gr.Group():
		with gr.Row():
			llm_provider = gr.Dropdown(PROVIDERS, value=env.llm_provider, label='LLM Provider', info='Which API drives the agent')
			llm_model = gr.Textbox(value=env.llm_model, label='LLM Model Name', info='Any model name the provider accepts')
		with gr.Row():
			llm_api_key = gr.Textbox(
				label='API Key', type='password', placeholder='(using the key from .env)' if env.llm_api_key else '(not set in .env)',
				info='Leave blank to use .env. Never saved to a config file.',
			)
			llm_base_url = gr.Textbox(value=env.llm_base_url or '', label='Base URL', info='Only for openai-compatible providers')
		with gr.Row():
			llm_temperature = gr.Number(
				value=env.llm_temperature, label='LLM Temperature', info='Blank = provider default (reasoning models reject custom values)'
			)
			use_vision = gr.Checkbox(value=True, label='Use Vision', info='Send page screenshots to the model each step')
	with gr.Row():
		max_steps = gr.Slider(1, 200, value=env.max_steps, step=1, label='Max Run Steps', info='Per attempt')
		max_actions = gr.Slider(1, 20, value=5, step=1, label='Max Actions per Step')
	extend_system_prompt = gr.Textbox(label='Extend system prompt', lines=3, info='Extra instructions appended to browser-use\'s system prompt')

	manager.add_components(
		'agent_settings',
		dict(
			llm_provider=llm_provider, llm_model=llm_model, llm_api_key=llm_api_key, llm_base_url=llm_base_url,
			llm_temperature=llm_temperature, use_vision=use_vision, max_steps=max_steps, max_actions=max_actions,
			extend_system_prompt=extend_system_prompt,
		),
	)


def create_stealth_settings_tab(manager: WebuiManager, env: Settings) -> None:
	gr.Markdown('Every attempt gets a coherent fake Windows device, launched in Fortress, with timezone and language matched to the exit IP.')
	with gr.Group():
		with gr.Row():
			engine = gr.Dropdown(ENGINES, value=env.engine, label='Engine', info='fortress = stealth. chromium = stock, NOT stealthy (testing only)')
			fortress_channel = gr.Dropdown(['stable', 'latest'], value=env.fortress_channel, label='Fortress channel')
			headful = gr.Checkbox(value=not env.headless, label='Show browser window', info='Also open the real window on this PC')
		cdp_url = gr.Textbox(value=env.cdp_url or '', label='CDP URL', info='Only for engine=cdp: a browser you started yourself')
	with gr.Group():
		with gr.Row():
			identity = gr.Dropdown(
				[''] + saved_identities(env), value='', allow_custom_value=True, label='Sticky identity',
				info='Blank = a fresh device and IP every run. A name keeps the same device, IP and cookies (logins survive).',
			)
			max_attempts = gr.Slider(1, 10, value=env.max_attempts, step=1, label='Max attempts', info='Fresh identities to try when a site blocks')
			geo_match = gr.Checkbox(value=env.geo_match, label='Match timezone/language to exit IP')
	with gr.Group():
		with gr.Row():
			proxy_gateway = gr.Textbox(
				label='Proxy gateway', type='password',
				placeholder='(using VEIL_PROXY_GATEWAY from .env)' if env.proxy_gateway else 'http://USER__cr.us;sessid.{session}:PASS@host:port',
				info='Blank = .env. {session} is replaced per identity. Never saved to a config file.',
			)
			proxy_file = gr.Textbox(value=str(env.proxy_file or ''), label='Proxy list file', info='One proxy per line (used if no gateway)')
			proxy_strategy = gr.Dropdown(['round_robin', 'random', 'least_used'], value=env.proxy_strategy, label='List rotation')

	manager.add_components(
		'stealth_settings',
		dict(
			engine=engine, fortress_channel=fortress_channel, headful=headful, cdp_url=cdp_url, identity=identity,
			max_attempts=max_attempts, geo_match=geo_match, proxy_gateway=proxy_gateway, proxy_file=proxy_file, proxy_strategy=proxy_strategy,
		),
	)


def create_config_tab(manager: WebuiManager) -> None:
	gr.Markdown('Save the current settings (API keys and proxy credentials are left out) and load them back later.')
	with gr.Row():
		save_button = gr.Button('💾 Save current settings', variant='primary')
		config_list = gr.Dropdown(manager.list_configs(), label='Saved configs', allow_custom_value=False)
		load_button = gr.Button('📂 Load')
	config_status = gr.Markdown()

	inputs = set(manager.get_components())

	def save(components):
		path = manager.save_config(manager.values_by_id(components))
		return gr.update(choices=manager.list_configs(), value=path.name), f'Saved `{path}`'

	save_button.click(save, inputs=inputs, outputs=[config_list, config_status])

	targets = manager.get_components()

	def load(name):
		if not name:
			return [gr.skip()] * len(targets) + ['Pick a saved config first.']
		values = manager.load_config(name)
		updates = [gr.update(value=values[manager.component_to_id[c]]) if manager.component_to_id[c] in values else gr.skip() for c in targets]
		return updates + [f'Loaded `{name}`']

	load_button.click(load, inputs=config_list, outputs=[*targets, config_status])
