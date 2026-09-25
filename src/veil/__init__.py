"""veil: stealth browser sessions for AI agents (Fortress engine + browser-use + rotating identities)."""
from veil.config import Settings
from veil.session import Identity, StealthSession


def main() -> None:
	from veil.cli import main as cli_main

	raise SystemExit(cli_main())


__all__ = ['Identity', 'Settings', 'StealthSession', 'main']
