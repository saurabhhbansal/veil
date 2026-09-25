# veil: project context for Claude Code

Research project: a stealth browser for autonomous AI agents. A browser-use agent drives the
Fortress stealth Chromium, while veil rotates coherent device fingerprints and proxy exit IPs and
keeps the two consistent. README.md covers usage and the design; docs/PROXY_RESEARCH.md covers proxy sourcing.

## Decisions already made (don't relitigate without a reason)

- **No repo merge.** Fortress (github.com/tiliondev/fortress) is a patched Chromium, used via its
  prebuilt binary from the `tilion-fortress` pip package. browser-use is used as a library, attached
  over CDP (`BrowserSession(cdp_url=...)`). veil is the orchestration layer in between.
- **veil generates the fingerprint itself.** The open Fortress launcher applies one fixed persona
  to everyone (RTX 3060, canvas seed 778899, 1920x1080), and its per-launch persona engine is closed-source.
  veil sets `TILION_NO_DEFAULTS=1` and passes a whole-device persona as `--uxr-*` switches
  (persona.py + data/devices.json). Never randomise surfaces independently: an impossible device is a tell.
- **Timezone and language follow the proxy exit IP** (geo.py). The device itself doesn't depend on geography.
- **Don't let browser-use reshape the fingerprint.** Keep `BrowserProfile(headless=False, no_viewport=True)`,
  otherwise it calls `Emulation.setDeviceMetricsOverride`. Never pass `--user-agent`: it desyncs UA from
  Client Hints. Avoid `Runtime.enable`, the classic CDP automation tell: page probes go through
  `evaluate_isolated()` (an isolated world, raw CDP). Don't use browser-use's `actor` Page API; it enables Runtime.
- **Windows:** start `chrome.exe` directly (engine.py `fortress_command`), never `cmd /c tilion.cmd`.
  cmd.exe treats `<-loopback>` as redirection, and TILION_NO_DEFAULTS would also drop the WebGPU switches.
- **The LLM is configurable.** Currently OpenAI `gpt-6-luna` (released Sept 2026). Groq, Anthropic, Gemini
  and OpenAI-compatible providers are supported in llm.py. Leave temperature unset by default.
- **Proxies:** there's no provider yet. The plan is the user's own phone as a free mobile proxy first, then DataImpulse
  ($1/GB, gateway `http://LOGIN__cr.us;sessid.{session}:PASS@gw.dataimpulse.com:823`). Chromium
  can't authenticate SOCKS proxies: use HTTP. browser-use answers HTTP proxy auth over CDP (Fetch domain).

## Layout

```
src/veil/
  session.py    StealthSession: proxy -> geo -> persona -> engine.launch -> browser-use; Identity (sticky)
  engine.py     fortress | fortress-docker | cdp | chromium (stock, NOT stealthy, testing only)
  persona.py    Persona dataclass, generate(seed, exit_info), flags(), env()
  proxy.py      Proxy.parse, ProxyPool (cooldowns), GatewayProxy ({session} sticky token)
  geo.py        exit-IP lookup (ipinfo, ip-api fallback) -> timezone, languages
  detection.py  classify(url, title, html) -> BlockSignal (Cloudflare/Akamai/PX/DataDome/Imperva/Google)
  agent.py      run_task: Agent + on_step_end block guard; rotates identity and retries on a block
  check.py      `veil check`: compares the page-visible fingerprint with the persona
  cli.py        run | check | persona | proxies | identities
```

## Status (at handoff from the ARM VPS, 2026-09-25)

Built and unit-tested on an ARM VPS, where Fortress can't run (x64 only), using `VEIL_ENGINE=chromium`.
- Verified: 39 unit tests; a full session with the stock engine; authenticated HTTP proxy routing.
- **NOT yet verified:** the Fortress engine itself (on this Windows PC, first run), whether every
  `--uxr-*` switch lands, the WebGPU architecture names and GPU PCI IDs in devices.json (seed data written from memory),
  a real `veil run` with an LLM, and a real proxy.

## Next steps, in order

1. `uv sync`, then `uv run pytest`, then `uv run veil check`. Fix every FAIL line. Each one is a surface where
   what the page sees differs from the persona we asked for.
2. `uv run veil check --screenshots`, then review reports/*/ (sannysoft, CreepJS, BrowserScan, rebrowser).
3. First agent run: `uv run veil run "..."` with the key in .env.
4. Roadmap in README.md: human-like mouse and typing, proxy bandwidth savers, phone-proxy IP rotation over ADB,
   concurrent sessions, growing devices.json from real device captures.

## Conventions

- Python 3.12, uv, tabs for indentation, single quotes (matching browser-use's style).
- Config comes only from env vars / .env (config.py). Secrets never go in git (.env and proxies.txt are gitignored).
- Keep pure logic (parsing, persona, classification) unit-testable without a browser or network.
- The repo is github.com/saurabhhbansal/veil (private). Commit and push to main.
