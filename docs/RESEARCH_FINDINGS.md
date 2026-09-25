# Research findings: what veil can and can't do

*A living log. Add a dated entry whenever a run teaches us something about the system's limits or about how
sites defend themselves. Keep **observed** (we ran it, with evidence) separate from **expected** (reasoning or
public knowledge we haven't tested yet).*

## Summary so far

Sites defend in layers, and each layer asks a different question:

| Layer | The question the site asks | veil's answer | Status |
|---|---|---|---|
| 1. Network | Is this IP a datacenter, a known proxy, or abusive? | Residential/mobile proxy, rotated, geo-matched | Built. Not yet tested with a real proxy |
| 2. Browser fingerprint | Is this a real browser on a real device? | Fortress + a coherent fake device per identity | Built and measured (see detector results) |
| 3. Automation tells | Is something driving this browser? | No `Runtime.enable`, no viewport emulation, no webdriver flag | Built. rebrowser passes |
| 4. Behaviour | Does this move, type and pace itself like a person? | Nothing yet (browser-use clicks and types instantly) | **Gap**, on the roadmap |
| 5. Challenges | Can you solve this puzzle (CAPTCHA, Turnstile)? | Waits for self-clearing challenges, otherwise rotates identity | Partial. No solving, by design |
| 6. Identity proof | Can you prove you hold a phone, a card, an ID? | **Nothing**, and by design nothing will be built | **Hard limit** |

Layers 1–5 are about looking like *a* real person. Layer 6 is about being *a specific, accountable* person. A
better browser can't produce a SIM card, a payment card or a passport, so no amount of work on layers 1–5 moves
layer 6. That's the most important finding so far.

## What automated agents fundamentally can't do

These need something that exists outside the browser, so fingerprints and IPs are irrelevant to them:

- **Receive an SMS or call on a real phone number** tied to a SIM and a carrier account.
- **Scan a QR code with a phone** that's signed in or attested (Google's device verification, WhatsApp/Telegram
  web linking, bank app approvals).
- **Pass hardware-backed attestation**: passkeys/WebAuthn, Android Play Integrity, iOS App Attest. The key
  lives in a device's secure hardware.
- **Provide a real payment instrument or government ID** (card checks, KYC, selfie and liveness checks).
- **Have history**: account age, years of normal use, a reputation that's expensive to fake and slow to build.
- **Approve a login from an already-trusted device**, i.e. push-notification 2FA.

The agent *can* do everything in between: navigate, read, fill forms, compare, extract. For tasks that hit one
of the gates above, the realistic design is a **human handoff**: the agent pauses, you complete the step on
your own phone, and the agent carries on. (Roadmap: "let the user take over mid-task".)

## How protected are sites? (tiers)

| Tier | Typical sites | Defences | veil today |
|---|---|---|---|
| 0: open | Wikipedia, docs, most blogs | Rate limits at most | **Observed: works** (several Wikipedia runs, no blocks) |
| 1: CDN bot management | Many e-commerce, travel and ticketing sites | Cloudflare, Akamai, DataDome, PerimeterX/HUMAN, Imperva: IP reputation, fingerprint, JS challenges | Expected to be the main battleground. **Not yet tested** |
| 2: behavioural risk scoring | Big marketplaces, social networks, login pages | Mouse/typing telemetry, session history, velocity checks | Expected weak spot (layer 4 gap). Not yet tested |
| 3: identity-bound actions | **Account creation** at Google, Microsoft, social networks; banks; payments | Phone/SMS, device attestation, payment or ID checks | **Observed: hard stop** at Google (below) |

Tier 3 applies per *action*, not per site. The same Google that lets anyone read search results gates account
creation behind a phone.

## Detector results (2026-09-25, Fortress 149, Windows 11 host, direct connection)

- **rebrowser:** everything passes (no `Runtime.enable` leak, webdriver false, viewport OK).
- **sannysoft:** everything passes after the screen avail/outer fix.
- **CreepJS:** 0% headless, 0% stealth, "19% like headless". It detects the **canvas and audio noise**.
- **BrowserScan:** 65% "authenticity". Flags: time zone mismatch (−10), language mismatch (−10), WebGL/audio/canvas
  tampering (−5 each). The language flag is real: the `Accept-Language` header drops `hi`.

Takeaway: fingerprint noise is *detectable*, even when every value is internally consistent. Whether a given
bot manager *punishes* noise is something only testing against real targets will tell.

## Log

### 2026-09-26: Gmail signup stops at device/phone verification (observed)

**Task:** create a Gmail account (run `~/.veil/runs/20260926-001742-58a213/`, 12 steps, direct connection,
exit 223.181.17.128 IN, a Windows 11 / RTX-class persona).

**What happened:**
1. Name, birthday, gender, address choice and password all went through normally. No CAPTCHA and no block page,
   so fingerprint and IP were *not* what stopped it.
2. After the password step, Google showed **"Verify some info before creating an account"**: *"Google needs to
   verify some info about your device or phone number before you can continue … by preventing abuse from
   computer programs or bots."* It offered a **QR code to scan with a phone**.
3. Per the user, scanning the QR leads to a page asking for **SMS verification** on the phone.
4. The agent can't do either, so it stopped and correctly reported that no account was created.

**Also observed:** in an earlier run (`20260926-001529-983c46`) the LLM itself refused to *invent* a birthday
and gender for the account and asked the user instead. The model's own guardrails are a separate limit from
the site's defences.

**Interpretation:** this is a layer-6 gate. Google lets the form be filled but ties account creation to a phone.
A phone number is scarce, costs money, belongs to someone, and can be rate-limited per number. That's exactly
what makes mass account creation expensive, and why it holds even against a perfect fingerprint.

**Implication for veil:** tasks that create accounts on major platforms can't be fully autonomous. What veil
*can* do is get to the gate quickly and hand over to the human. That's the "take over mid-task" feature.

## Open questions

- Tier 1 in practice: which bot managers let a Fortress persona through on a residential IP, and which don't?
  (Needs the phone proxy, then a list of target sites.)
- Does the detectable canvas/audio noise actually cost us on real sites, or only on detector pages?
- How much does the missing behavioural layer (instant clicks and typing) matter on tier-2 sites?
- **Out of scope, by decision:** getting around phone/SMS/QR/ID verification (virtual-number services, SMS
  farms, spoofed attestation). Those checks exist to tie an account to an accountable person, and defeating
  them for account creation amounts to fake-account fraud and breaks the platforms' terms. veil's answer to
  layer 6 is human handoff, not bypass. Research here stays at the level of *documenting that and why* these
  gates stop automation.
