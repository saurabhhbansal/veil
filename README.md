# veil

Stealth browser sessions for autonomous AI agents: an LLM-driven [browser-use](https://github.com/browser-use/browser-use)
agent running inside the [Fortress](https://github.com/tiliondev/fortress) stealth Chromium engine, with rotating
device fingerprints, rotating proxy exit IPs, and the two kept consistent with each other.

*Research project. `veil` is a working name.*

```
 task ──▶ browser-use Agent ◀── LLM (OpenAI / Groq / Anthropic / Gemini / any OpenAI-compatible)
               │ CDP (no Runtime.enable, no viewport emulation)
               ▼
        StealthSession ─────────────────────────────────────────────────┐
          1. proxy   ← ProxyPool (list, cooldowns) | GatewayProxy (sticky {session})
          2. geo     ← exit-IP lookup through that proxy → timezone, languages
          3. persona ← coherent Windows device (GPU→CPU/RAM/screen, seeds) + geo
          4. engine  ← Fortress launched with --uxr-* persona + --proxy-server, fresh profile
          5. guard   ← after every agent step: challenge/block page? → rotate identity, retry
               ▼
        Fortress (patched Chromium: fingerprint corrected in C++, not JS)
```

## Why it's built this way (and not by merging the repos)

- **Fortress** is a Chromium fork: 35 C++ patches, rebased monthly on upstream. Vendoring it would mean
  maintaining a Chromium build (~100 GB, hours per build). We use its prebuilt binary instead.
- **browser-use** already accepts any browser over CDP (`cdp_url`) and answers proxy authentication
  challenges over CDP. We use it as a library.
- **veil** is the missing piece in between: identity management.

**The key finding:** the open-source Fortress launcher applies **one fixed persona** (RTX 3060, the same
canvas/audio seeds, 1920×1080) to every user, which makes every Fortress user one linkable fingerprint.
Its per-launch "persona engine" is closed-source. veil generates the persona itself
(`src/veil/persona.py` + `src/veil/data/devices.json`) and passes it in through Fortress's `--uxr-*`
switches, with `TILION_NO_DEFAULTS=1`.

## Setup (on your own computer)

Fortress ships native binaries for **Linux x64** and **Windows x64**. On macOS, use Docker Desktop
(`VEIL_ENGINE=fortress-docker`; on Apple Silicon it runs under emulation and will be slower). It does
**not** run on ARM Linux, which is why this was scaffolded on the ARM VPS using the stock-Chromium test engine.

```bash
# needs uv: https://docs.astral.sh/uv/
git clone <this repo> && cd major-project
uv sync
cp .env.example .env        # add your LLM key; proxy settings are optional
                            # Windows cmd: copy .env.example .env   (then: notepad .env)
uv run pytest               # 37 unit tests, no network needed
uv run veil check           # first run downloads Fortress (~300 MB, SHA-256 verified)
```

## Usage

### Web control panel

```bash
uv run veil ui                       # opens http://127.0.0.1:7788 in your browser
```

Type a task, press **Submit Task**, and watch:
- **Agent Interaction:** each step's goal, actions and screenshot, then the result and every attempt made.
- **Browser Live View:** a screenshot of the stealth browser every second.
- **Current identity:** the exit IP and the fake device the site is seeing right now.
- **Stop / Pause / Resume / Clear:** Stop finishes the current step and doesn't count against the proxy.

The **Agent Settings** and **Stealth & Network** tabs override `.env` for the next run (blank fields keep `.env`).
**Load & Save Config** saves those settings without API keys or proxy credentials. Runs are kept in `~/.veil/runs/`
(step screenshots, `result.json`, a GIF). The UI binds to this PC only and has no login, so don't expose it with
`--host 0.0.0.0` on a network you don't trust. It's adapted from [browser-use/web-ui](https://github.com/browser-use/web-ui)
(MIT): same layout, rebuilt on veil's `StealthSession` and browser-use 0.13.

### Command line

```bash
uv run veil check                    # launch one identity, verify the page sees the persona we set
uv run veil check --screenshots      # also screenshot sannysoft / CreepJS / BrowserScan / rebrowser into reports/
uv run veil run "find the cheapest 1TB NVMe on amazon.in and return its price"
uv run veil run --identity shopper1 "log in to ... and check my orders"   # sticky: same device, IP and cookies
uv run veil proxies                  # exit IP / country / latency for each configured proxy
uv run veil persona --seed 42        # inspect a generated persona and its flags
uv run veil identities               # list saved sticky identities
uv run veil --headful run "..."      # watch it work
```

As a library:

```python
from browser_use import Agent
from veil import Settings, StealthSession
from veil.llm import build_llm

settings = Settings.from_env()
async with StealthSession(settings) as s:          # proxy → geo → persona → Fortress → browser-use
    agent = Agent(task="...", llm=build_llm(settings), browser_session=s.browser)
    history = await agent.run()
```

## Configuration

Everything is set through environment variables; see [`.env.example`](.env.example). The main ones:

| Variable | Meaning |
|---|---|
| `VEIL_LLM_PROVIDER` / `VEIL_LLM_MODEL` / `VEIL_LLM_API_KEY` | Agent brain. Defaults to `openai` / `gpt-6-luna`. `groq` works the same way. |
| `VEIL_ENGINE` | `fortress` (default), `fortress-docker`, `cdp`, or `chromium` (testing only, not stealthy) |
| `VEIL_PROXY_GATEWAY` | Provider gateway URL with a `{session}` placeholder for sticky IPs |
| `VEIL_PROXY_FILE` | One proxy per line, rotated with cooldowns |
| `VEIL_GEO_MATCH` | Match persona timezone and language to the exit IP (default on) |
| `VEIL_MAX_ATTEMPTS` | Fresh identities to try when a site blocks |

For where to get proxies cheaply, see **[docs/PROXY_RESEARCH.md](docs/PROXY_RESEARCH.md)**. Short
version: start with your own phone as a free mobile proxy, then DataImpulse at $1/GB when you need
more IPs.

## What each layer defends against

| Detection layer | What catches bots | Handled by |
|---|---|---|
| JS fingerprint (canvas, WebGL, audio, navigator, fonts…) | Headless tells, `toString` of patched getters, cross-realm mismatches | Fortress (C++ patches) |
| Cross-surface coherence | GPU vs platform vs cores vs screen that no real device has | veil persona generator (whole-device profiles) |
| Linkability across sessions | Same fingerprint/cookies on many "users" | veil: fresh persona and profile per session; sticky identities when you want continuity |
| Network: IP reputation / ASN | Datacenter IPs, reused IPs | veil proxy rotation, block-driven cooldowns |
| Network vs locale | Timezone/language that doesn't match the IP's country | veil geo-match |
| CDP side-effects | `Runtime.enable` leak, viewport emulation | Fortress launcher + veil's browser-use config + isolated-world probes |
| WebRTC IP leak | Real IP exposed via STUN | `--uxr-webrtc-policy=disable_non_proxied_udp` |
| Behavior (mouse paths, timing) | Too-fast, too-regular interaction | **Not handled yet**; see Roadmap |

## Persona data

`src/veil/data/devices.json` is seed data: GPU strings in Chrome-on-Windows ANGLE format (vendor, renderer
with PCI device ID, WebGPU vendor/architecture), and CPU/RAM/screen options per machine class, with
rough popularity weights. **Verify the entries against real machines** (open
`chrome://gpu` or run `veil check` output against a real Windows Chrome) before relying on them, and add
more devices. The generator is deterministic per seed, so a sticky identity always gets the same device.

## Status

Verified on the ARM development VPS with the stock-Chromium engine:
- 37 unit tests (proxy parsing and rotation, persona coherence, block classification)
- End-to-end session: geo lookup → persona → browser launch → browser-use attach → isolated-world fingerprint probe
- Authenticated HTTP proxy: the browser routes through it and credentials are answered over CDP

**Not yet verified** (needs an x64 machine, an LLM key, and a proxy):
- The Fortress engine itself: whether every `--uxr-*` override lands as expected (`veil check` will tell you)
- A full agent run (`veil run`) with a real LLM
- A real residential or mobile proxy

## Roadmap

- [x] Run `veil check --screenshots` on Fortress and fix any surface that doesn't apply
- [x] Web control panel (`veil ui`)
- [ ] Let the user take over the browser mid-task (CAPTCHA, login), as web-ui's "ask for help" did
- [ ] Human-like input: curved mouse paths and typing cadence (browser-use clicks via CDP events)
- [ ] Proxy bandwidth savers: optional resource blocking, disabling Chromium background traffic
- [ ] Phone-proxy IP rotation via ADB airplane-mode toggle
- [ ] Concurrent sessions (a pool of identities working in parallel)
- [ ] Capture real-device fingerprints to grow `devices.json`

## Use responsibly

Automated access has to respect sites' terms, robots.txt and rate limits, and the law where you and the
site are. Evading detection doesn't make an otherwise disallowed use allowed. Keep this to research,
testing your own sites, and legitimate agent tasks.
