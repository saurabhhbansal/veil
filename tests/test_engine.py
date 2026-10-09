from pathlib import Path

from veil import persona as P
from veil.engine import _common_flags, fortress_command
from veil.proxy import Proxy


def _flags():
	return _common_flags(9333, Path('/tmp/p'), Proxy.parse('h:1:u:p'), headless=True) + P.generate(seed=5).flags()


def test_linux_goes_through_the_bash_launcher():
	args, env = fortress_command(Path('/opt/tilion-fortress/tilion'), _flags(), P.generate(seed=5))
	assert Path(args[0]) == Path('/opt/tilion-fortress/tilion')  # str(Path) uses backslashes on Windows
	assert env['TILION_NO_DEFAULTS'] == '1' and 'TZ' in env


def test_windows_runs_chrome_directly_like_tilion_cmd():
	launcher = Path('C:/cache/tilion-fortress/tilion.cmd')
	args, env = fortress_command(launcher, _flags(), P.generate(seed=5))
	assert Path(args[0]) == launcher.parent / 'chrome.exe'  # never `cmd /c`: it would treat <-loopback> as redirection
	assert 'cmd' not in args
	assert '--enable-unsafe-swiftshader' in args
	# a forced SwiftShader WebGPU adapter makes requestAdapter() return null on Windows
	assert not any(a.startswith(('--use-webgpu-adapter', '--enable-dawn-features')) for a in args)
	assert '--proxy-bypass-list=<-loopback>' in args
	assert 'TZ' not in env
	# persona webgpu identity must come after the launcher defaults (last switch wins)
	assert args.index('--enable-unsafe-swiftshader') < args.index(next(a for a in args if a.startswith('--uxr-webgpu-vendor=')))


def test_close_removes_the_profile_even_if_files_are_briefly_locked(tmp_path, monkeypatch):
	from veil import engine

	profile = tmp_path / 'veil-profile-x'
	(profile / 'Default').mkdir(parents=True)
	calls = []
	real_rmtree = engine.shutil.rmtree

	def flaky_rmtree(path, ignore_errors=False):
		calls.append(path)
		if len(calls) > 1:  # the first try hits a file chrome's children still hold open
			real_rmtree(path, ignore_errors=ignore_errors)

	monkeypatch.setattr(engine.shutil, 'rmtree', flaky_rmtree)
	monkeypatch.setattr(engine.time, 'sleep', lambda s: None)
	engine.LaunchedBrowser(cdp_url='', applies_persona=True, _cleanup=[profile]).close()
	assert not profile.exists() and len(calls) == 2
