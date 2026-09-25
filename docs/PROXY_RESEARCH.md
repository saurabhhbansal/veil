# Proxy sourcing: what's cheapest that actually works

*Researched 2026-09-25. Prices come from third-party comparisons (webscraping.ai, checked against
provider pricing pages on 2026-07-28; Proxidize and AIMultiple, Sept 2026). They change often and
promos come and go, so confirm on the provider's own page before paying.*

## Why the proxy matters as much as the browser

Fortress fixes the **fingerprint**. It can't fix the **IP**. Bot managers score the IP's ASN
(autonomous system: which network the address belongs to) and reputation before any page
script runs, and Fortress's own docs say roughly 90% of remaining blocks are the IP, not the
browser. From most to least trusted:

| IP type | Trust | Typical cost | Notes |
|---|---|---|---|
| Mobile (4G/5G carrier) | Highest | ~$2–9/GB, or your own SIM | Carrier NAT puts thousands of real users behind one IP, so sites can't ban it without collateral damage |
| Residential (home ISP) | High | $0.50–7/GB | The standard choice for agents |
| ISP / static residential | High, stable | $1–3 per IP per month | One fixed IP; good for a long-lived logged-in identity |
| Datacenter | Low | Cents per GB | Fine for easy sites, instantly flagged by Cloudflare/Akamai/DataDome |
| Tor / free lists | Near zero | Free | Exit lists are public and pre-blocked; free lists are dead, slow, and can intercept your traffic (MITM) |

This VPS's own IP (Oracle Cloud, datacenter ASN) falls in the "datacenter" row. Your home connection is residential.

## Recommendation for this project

### 1. Free: your own phone as a mobile proxy (start here)

A phone on Jio/Airtel/Vi gives you a carrier IP, the most trusted kind, for the cost of the data you use.

- Install a proxy app on an Android phone (e.g. **Every Proxy**, which serves HTTP/SOCKS on the LAN), connect the computer to the same Wi‑Fi, and set
  `VEIL_PROXY_FILE` to a file containing `PHONE_LAN_IP:PORT`.
  (Prefer HTTP: Chromium can't authenticate SOCKS proxies at all.)
- **Rotation:** toggling airplane mode gets a new carrier IP. Over ADB this should work without root on Android 10+ (`adb shell cmd connectivity airplane-mode enable`, then `disable`). Test it on your phone; veil doesn't automate it yet.
- Limits: one IP at a time, Indian geo only, and it's your personal SIM. Good for development and demos, not for parallel sessions.

### 2. Cheapest paid, low volume: DataImpulse, $1/GB

- **$1.00/GB pay-as-you-go, $5 minimum, traffic never expires.** Below ~50 GB/month nothing is cheaper.
- Residential, 195 countries, targeting by country/state/city/ASN, rotating or sticky sessions.
- Sticky-session syntax (confirmed in their docs, docs.dataimpulse.com/proxies/parameters/session-id):
  `http://LOGIN__cr.us;sessid.123:PASSWORD@gw.dataimpulse.com:823`
  In veil: `VEIL_PROXY_GATEWAY=http://LOGIN__cr.us;sessid.{session}:PASSWORD@gw.dataimpulse.com:823`

### 3. If volume goes past ~50 GB/month: Evomi, about $0.50/GB

- $49.99/month for 100 GB. Cheaper per GB, but a monthly commitment.

### Reference: other providers (entry price → best published rate)

| Provider | Entry | $/GB at entry | Best published $/GB | Expiry |
|---|---|---|---|---|
| DataImpulse | $5 | $1.00 | $0.80 (1 TB) | never |
| Evomi | $49.99/mo | $0.50 | $0.49 | not stated |
| Rayobyte | $3.50 | $3.50 | $0.50 (5 TB+) | PAYG never |
| Webshare | $3.50/mo | $3.50 | $1.40 (3 TB) | not stated |
| Decodo (ex‑Smartproxy) | $11.25/mo | $3.75 | $2.00 | not stated |
| Bright Data | PAYG | $4.00 (promo) | $2.50 | not stated |
| IPRoyal | $7 | $7.00 | $4.90 | never |
| Oxylabs | $30/mo | $6.00 | $2.50 | not stated |
| SOAX | $200/mo | $3.00 | $1.50 | 60 days |

Mobile-specific pools (e.g. ProxyEmpire) run about $4.50/GB at small volume, falling to $2/GB at large volume.

## Budgeting bandwidth

Per-GB billing counts every byte, including failed and blocked page loads. A browser agent loads real
pages with images, scripts and fonts, so assume roughly **2–10 MB per page view**, or about **100–500 page views per GB**. On DataImpulse, that's about $1 for a few hundred agent steps.

Ways to cut this (not built yet):
- Block images, media and fonts on pages where the agent only needs text. Careful: some bot managers notice a browser that never loads images.
- Stop Chromium's background calls to Google services (seen in testing: `accounts.google.com`, `android.clients.google.com`), which go through the paid proxy too.

## An ethical note on residential IPs

Residential pools are real people's home connections. Some networks get them through consented
bandwidth-sharing apps; others through SDKs bundled into free apps, which users rarely understand.
Before paying, check whether the provider publishes how it sources IPs and gets consent; I haven't
verified this for the providers above. Your own phone avoids the question entirely.

## What veil does with any of these

- **Gateway** (`VEIL_PROXY_GATEWAY` with `{session}`): each session gets a new sticky exit IP, and a
  named `--identity` reuses its session token so it keeps the same IP.
- **List** (`VEIL_PROXY_FILE`): round-robin, random or least-used selection. A proxy that gets blocked goes on an exponential cooldown.
- In both modes, the exit IP is geolocated and the persona's timezone and `Accept-Language` are set to match.
- `veil proxies` checks each proxy's exit IP, country and latency before you spend money on a run.
