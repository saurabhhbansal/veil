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
  accounts.py   `veil signup` / `veil accounts`: the user's own profiles.csv -> domain-scoped sensitive_data placeholders;
                signups.csv logs site/profile/status, one account per (site, profile), each on its own sticky identity;
                build_login_task / plan_login log in with the row's password (or the profile's, for accounts made outside veil)
  planner.py    chat message -> None (ordinary task) or Jobs (signup | login | auto | task); several jobs run side by side
  envfile.py    the app writes API keys, model defaults and proxy settings back to .env (and os.environ)
  cli.py        run | ui | check | persona | proxies | identities
  webui/        `veil ui`: server.py = FastAPI API + the chat app (web/out); live.py = interactive live view (2nd CDP
                connection: screencast out, Input.* in); runner.py = UI-agnostic run state, events, take_over/hand_back
                (RunHooks into agent.run_task) and SessionHub (sessions: 'main' + one per split-off job, a browser gate of
                VEIL_MAX_PARALLEL, one shared proxy source); admin.py = /api/keys, /api/llm/default, /api/proxies*;
                manager.py = UI values -> Settings. The Gradio panel was removed 2026-10-09.
web/            Next.js static export adapted from browser-use/chat-ui-example (MIT); Browser Use Cloud SDK removed,
                talks only to the local API. `npm run build` -> web/out, served by `veil ui`.
```

Human takeover: RunHooks.request_human pauses the agent (status needs_human). _BlockGuard hands interactive
CAPTCHAs (detection.find_captcha) and unsolved challenges to the person once per page before rotating; the agent
also has a `request_human_help` action. hand_back() injects an ActionResult note into agent.state.last_result.
Not Browser Use Cloud: veil never uses hosted browser services (user decision, 2026-10-09).
Accounts in the app: /api/accounts* (server.py) over accounts.py; sign-ups and logins run through AgentRunner (RunRequest.title/
kind/on_done records the status). Passwords leave the server only via /api/accounts/reveal; API keys and proxy credentials never.
Sessions: endpoints take `session` (body or ?session=), default 'main'. A request the planner splits runs each job in its own
session/browser/identity (queued past VEIL_MAX_PARALLEL); the app shows them as tabs. Each run in the sidebar has a ⋯ menu
(open, rename, download recording, delete: /api/runs/<id>/rename|delete).
LLM effort: Settings.llm_effort (VEIL_LLM_EFFORT, llm.EFFORTS per provider): OpenAI reasoning_effort, Anthropic adaptive thinking +
output_config.effort, Google thinking_level. Proxies: VEIL_PROXY_MODE (direct | list | gateway) picks the source and keeps the others in .env.
Country-matched exits: account runs pass the profile's country (geo.country_code) as RunRequest.country -> run_task ->
StealthSession -> ProxySource.acquire(country=). Lists learn countries from the last test (proxy-status.json); gateways
fill {country}/{COUNTRY} (VEIL_PROXY_COUNTRY when none). No proxy there: any proxy, and the run posts a notice.
Runs: runner.py writes <runs_dir>/<id>/run.json on every event (list_runs/load_run read it; older runs fall back to
result.json). Steers: RunHooks.on_step_start injects them into agent.state.last_result; ones that arrive as the agent
finishes go through RunHooks.take_followups -> agent.add_new_task + another agent.run in the same browser.
Review: judge.py (opt-in RunRequest.review); runner keeps last_review and appends judge.next_run_message to the next
run's extend_system_message.

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
2. Roadmap in README.md: human-like mouse and typing, proxy bandwidth savers,
   phone-proxy IP rotation over ADB, concurrent sessions, growing devices.json from real device captures.

## Conventions

- Python 3.12, uv, tabs for indentation, single quotes (matching browser-use's style).
- Config comes only from env vars / .env (config.py). Secrets never go in git (.env and proxies.txt are gitignored).
- Keep pure logic (parsing, persona, classification) unit-testable without a browser or network.
- The repo is github.com/saurabhhbansal/veil (private). Commit and push to main.
