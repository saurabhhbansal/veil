"""Write settings back to the .env file (API keys, proxies) from the web UI, and apply them to this process.

Lines for other variables and comments are left as they are. The values never leave this PC.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import set_key, unset_key


def env_path(path: Path | None) -> Path:
	return path or Path('.env').resolve()


def write(path: Path | None, values: dict[str, str | None]) -> Path:
	"""Set (or, for None/'', remove) each variable in the .env file and in os.environ."""
	p = env_path(path)
	if not p.exists():
		p.parent.mkdir(parents=True, exist_ok=True)
		p.write_text('', encoding='utf-8')
	for name, value in values.items():
		if value:
			set_key(str(p), name, value, quote_mode='auto')
			os.environ[name] = value
		else:
			if name in read_names(p):
				unset_key(str(p), name, quote_mode='auto')
			os.environ.pop(name, None)
	return p


def read_names(path: Path) -> set[str]:
	"""Variable names set in the file (values not read)."""
	if not path.exists():
		return set()
	names = set()
	for line in path.read_text(encoding='utf-8').splitlines():
		line = line.strip()
		if line and not line.startswith('#') and '=' in line:
			names.add(line.split('=', 1)[0].removeprefix('export ').strip())
	return names


def mask(secret: str | None) -> str | None:
	"""'sk-proj-abc...wxyz' -> '••••wxyz': enough to tell keys apart, not to use one."""
	if not secret:
		return None
	return '••••' + secret[-4:] if len(secret) > 8 else '••••'
