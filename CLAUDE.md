# veil: project context for Claude Code

Research project: a stealth browser for autonomous AI agents. A browser-use agent drives the
Fortress stealth Chromium, while veil rotates coherent device fingerprints and proxy exit IPs and
keeps the two consistent. README.md covers usage and the design; docs/PROXY_RESEARCH.md covers proxy sourcing; docs/RESEARCH_FINDINGS.md
is the running log of limits and site defences (add a dated entry when a run teaches something).

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
  cli.py        run | ui | check | persona | proxies | identities
  webui/        `veil ui`: Gradio panel adapted from browser-use/web-ui (MIT). runner.py = UI-agnostic run state
                (RunHooks into agent.run_task); tabs/run_tab/interface = Gradio. web-ui itself pins browser-use 0.1.48,
                so only its layout was reused, not its agent/browser code.
```

## Status (Windows PC, 2026-10-09)

Verified on Windows x64 with the Fortress engine:
- 58 unit tests; `veil check` 18/18 across NVIDIA/AMD/Intel personas (incl. WebGPU adapter, screen avail/outer size).
- devices.json PCI IDs and Dawn WebGPU architecture names checked (no changes needed).
- `veil run` and `veil ui` complete real tasks with OpenAI gpt-6-luna; UI Stop/Pause/Resume tested.
- Fixed on first contact: the Windows launcher flags (a forced SwiftShader WebGPU adapter returned null), the host
  monitor's avail size leaking (now `--uxr-screen-avail-*` / `--uxr-outer-*`), and browser-use sending
  `frequency_penalty` to gpt-6 models (rejected with a 400).
- **NOT yet verified:** a real proxy, and a site with serious bot protection.
- 2026-10-09 review fixed: Accept-Language vs navigator.languages (geo.py now emits only `locale,base`, which is
  all Chromium 149 sends), temp profiles leaking on Windows, Turnstile widgets misread as challenge pages, SOCKS geo
  lookups (needs `httpx[socks]`), Stop between attempts, fake "result" on unfinished runs, UI field clearing.
- Open detector findings: BrowserScan (75%) flags a timezone mismatch (direct connection: the browser egresses over
  IPv6, the geo lookup over IPv4; see RESEARCH_FINDINGS) and canvas/audio/WebGL noise; CreepJS sees the canvas/audio noise. Win10 personas get the
  host's Win11 fonts. Chrome 149 is behind stable (155).

## Next steps, in order

1. Real proxy (phone first, see docs/PROXY_RESEARCH.md), then a harder target site.
2. Roadmap in README.md: user takeover mid-task, human-like mouse and typing, proxy bandwidth savers,
   phone-proxy IP rotation over ADB, concurrent sessions, growing devices.json from real device captures.

## Conventions

- Python 3.12, uv, tabs for indentation, single quotes (matching browser-use's style).
- Config comes only from env vars / .env (config.py). Secrets never go in git (.env and proxies.txt are gitignored).
- Keep pure logic (parsing, persona, classification) unit-testable without a browser or network.
- The repo is github.com/saurabhhbansal/veil (private). Commit and push to main.
