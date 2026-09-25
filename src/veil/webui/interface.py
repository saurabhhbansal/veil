# App shell adapted from browser-use/web-ui (src/webui/interface.py, webui.py;
# MIT License, Copyright (c) 2024 Browser Use Inc.; see LICENSE-web-ui).
"""Build and launch the veil control panel."""
from __future__ import annotations

import gradio as gr

from veil.config import Settings
from veil.webui.manager import WebuiManager
from veil.webui.run_tab import create_run_tab
from veil.webui.runner import AgentRunner
from veil.webui.tabs import create_agent_settings_tab, create_config_tab, create_stealth_settings_tab

CSS = """
.gradio-container { max-width: 1500px !important; margin: 0 auto !important; padding-top: 10px !important; }
.header-text { text-align: center; margin-bottom: 12px; }
"""

# Dark mode by default, as in web-ui.
DARK_JS = """
function refresh() {
	const url = new URL(window.location);
	if (url.searchParams.get('__theme') !== 'dark') {
		url.searchParams.set('__theme', 'dark');
		window.location.href = url.href;
	}
}
"""


def create_ui(env: Settings) -> tuple[gr.Blocks, AgentRunner]:
	manager = WebuiManager(config_dir=env.data_dir / 'webui' / 'configs')
	runner = AgentRunner(runs_dir=env.data_dir / 'runs')
	with gr.Blocks(title='veil') as demo:
		gr.Markdown('# 🕵️ veil\n### Stealth browser agent: Fortress + browser-use, with a fresh coherent device and exit IP per identity', elem_classes=['header-text'])
		with gr.Tabs(selected='run'):
			# Settings tabs first: the run tab reads all their components.
			with gr.Tab('⚙️ Agent Settings', id='agent'):
				create_agent_settings_tab(manager, env)
			with gr.Tab('🕵️ Stealth & Network', id='stealth'):
				create_stealth_settings_tab(manager, env)
			with gr.Tab('🤖 Run Agent', id='run'):
				create_run_tab(manager, runner, env)
			with gr.Tab('📁 Load & Save Config', id='config'):
				create_config_tab(manager)
	return demo, runner


def launch(env: Settings, host: str = '127.0.0.1', port: int = 7788, open_browser: bool = False, theme: str = 'Ocean') -> None:
	demo, runner = create_ui(env)
	runner.runs_dir.mkdir(parents=True, exist_ok=True)
	demo.queue().launch(
		server_name=host,
		server_port=port,
		inbrowser=open_browser,
		allowed_paths=[str(runner.runs_dir)],  # step screenshots and GIFs
		theme=getattr(gr.themes, theme)(),
		css=CSS,
		js=DARK_JS,
	)
