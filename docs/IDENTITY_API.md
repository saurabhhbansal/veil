# Identity API: veil asks your company server who to sign up as

By default veil reads people from `profiles.csv`, a file you fill in by hand. With the identity API turned on,
veil asks **your own HTTP server** for a person every time a sign-up or log-in needs one. It gets their email,
phone, name, date of birth, address and country, and optionally a password. You keep employee details on your
company machine. veil keeps only what it needs for the run.

This document is the contract. If your server implements the endpoints below, veil works with it unchanged.
The client side lives in `src/veil/identities.py`, and `tests/test_identities.py` contains a fake server that
follows this contract exactly.

**There's a ready-made server:** [`../veil-identity-server`](../../veil-identity-server/README.md), a separate
project next to veil. It implements everything here, stores employees in SQLite, and catches SMS codes (Twilio,
Android forwarder apps, any gateway) and email codes and links (IMAP polling, inbound-mail webhooks). Copy that
folder to the company machine and follow its README.

---

## 1. Turning it on

Add these lines to veil's `.env`. Only the first one is required.

```env
VEIL_IDENTITY_API=http://10.0.0.5:8900        # base URL of your server; setting it switches veil to API mode
VEIL_IDENTITY_API_TOKEN=change-me             # optional: sent as "Authorization: Bearer change-me"
VEIL_IDENTITY_API_TIMEOUT=15                  # optional: seconds per request (default 15)
VEIL_IDENTITY_API_SEND_PASSWORDS=0            # optional: 1 = include the password veil used when reporting results
```

- With `VEIL_IDENTITY_API` set, veil does not read `profiles.csv` for account runs. The Accounts panel shows
  the server instead, and profiles can't be edited from the app.
- With it unset, veil falls back to `profiles.csv`, exactly as before.
- If the URL is set but nothing is listening, every sign-up fails with
  `identity server unreachable at http://... (ConnectError: ...)`. That is expected until you build the server.

Check from the command line:

```bash
veil accounts profiles          # API mode: prints the server URL and whoever GET /v1/identities lists
veil signup example.com         # asks the server for a person, then runs the sign-up
```

---

## 2. The flow

```
 You (chat / Accounts panel / CLI)
   │  "sign up on shop.com"
   ▼
 veil ──(1) POST /v1/identities/assign ───────────────▶ Your identity server
   │        {site, purpose, country, exit_countries,          │ picks an employee, can create
   │         exclude}                                         │ a mailbox / number for them
   │ ◀──────────────────────── identity JSON ─────────────────┘
   │
   │  (2) picks a proxy exit in identity.country, launches a stealth browser
   │      with a sticky device for (site, identity.id), and the agent fills the form.
   │      The LLM only ever sees placeholders (x_email, x_phone…), never the values.
   │
   ├──(3) GET /v1/identities/{id}/codes?channel=sms ────▶ (optional) the OTP the site sent
   │        polled every 5 s for up to 90 s                  to that person's phone or mailbox
   │
   ├──(3b) GET /v1/identities/{id}/links ──────────────▶ (optional) the "confirm your email" link;
   │        polled the same way; the agent opens it          the run ends signed_up, not verify_email
   │
   └──(4) POST /v1/identities/{id}/events ─────────────▶ (optional) how it ended: signed_up,
            {site, action, status, note, ...}                verify_email, needs_human, failed…
```

Retries and later log-ins use **the same person**. veil remembers `(site, identity id)` in its local
`signups.csv` and calls `GET /v1/identities/{id}` to fetch that person again. It does not ask for a new one.

---

## 3. The identity object

Every endpoint that returns a person returns this JSON object. Only `id` and `email` are required. veil
leaves out any field you don't send, and the agent never makes up a value for a missing field: if a form
needs a field the identity doesn't have, the run stops with `needs_human`.

```json
{
  "id": "emp-104",
  "email": "asha.rao@yourcompany.com",
  "username": "asha_rao",
  "first_name": "Asha",
  "last_name": "Rao",
  "dob": "1994-03-07",
  "gender": "female",
  "phone": "+4915112345678",
  "address": "Hauptstr. 5",
  "city": "Berlin",
  "state": "BE",
  "postcode": "10115",
  "country": "DE",
  "password": "optional — omit and veil generates a strong random one per site"
}
```

| Field | Required | Format / notes |
|---|---|---|
| `id` | yes | Your stable id for this person. veil uses it in URLs (percent-encoded) and as the key for retries, log-ins and the sticky browser identity (`acct-<site>-<id>`). Never reuse an id for a different person. |
| `email` | yes | Must contain `@`. This is the address the site will mail. |
| `dob` | no | **`YYYY-MM-DD`**, nothing else. veil derives day, month, month name, year and `DD/MM/YYYY` from it. Any other format is rejected. |
| `phone` | no | Send **E.164** (`+<country code><number>`). The agent types the field exactly as you give it. |
| `country` | no | ISO-3166 alpha-2 (`DE`, `IN`, `US`) or an English country name. **It decides the proxy exit country**: veil picks a proxy in that country when it has one. |
| `password` | no | If present, veil uses it for this site. If absent, veil generates a 16-character random password and keeps it in `signups.csv` (see `send_passwords` in §4.4). |
| `username`, `first_name`, `last_name`, `gender`, `address`, `city`, `state`, `postcode` | no | Free text, typed as given. |

veil converts every value to a string and ignores extra fields, so you can include your own data such as
`employee_no` without breaking anything.

---

## 4. Endpoints

Base URL: whatever you set in `VEIL_IDENTITY_API`. All paths are under `/v1`.

Every request carries these headers:

```
Accept: application/json
User-Agent: veil/<version>
Authorization: Bearer <VEIL_IDENTITY_API_TOKEN>      (only when a token is set)
Content-Type: application/json                       (POSTs)
```

### Errors (all endpoints)

For any status of 400 or above, send a JSON body like `{"error": "human readable reason"}`. A
`{"detail": "..."}` body also works, so FastAPI's default error format is fine. veil shows the reason in
the chat as `identity server: <status> <reason>`, and the run doesn't start (the app returns HTTP 502).

| You return | veil does |
|---|---|
| `2xx` + identity JSON | proceeds |
| `4xx` / `5xx` | stops this job and shows your `error` text |
| not JSON / timeout / connection refused | stops this job and says why |

---

### 4.1 `POST /v1/identities/assign` (required)

veil calls this when it needs a **new** person for a site: a new sign-up, or a log-in where no profile was
named and veil has no record for the site.

**Request**

```json
{
  "site": "shop.example.com",
  "url": "https://shop.example.com",
  "purpose": "signup",
  "country": "DE",
  "exit_countries": ["DE", "IN", "US"],
  "exclude": ["emp-101"]
}
```

| Field | Meaning |
|---|---|
| `site` | Registrable host with no `www.` prefix. Use it to avoid handing out someone who already has an account there (veil also checks its own log). |
| `url` | Same site as a URL. |
| `purpose` | `"signup"` or `"login"`. For `login`, return someone who **already has** an account on `site`. |
| `country` | The preferred country (`VEIL_PROXY_COUNTRY`), or `null`. You may honour it, for example by creating a mailbox or number in that region, or ignore it. |
| `exit_countries` | The countries veil has tested proxies in, or `null` (meaning any country: a gateway proxy, or unknown). Returning a person whose `country` is in this list means the run exits from the right country. Otherwise the run still goes ahead from another exit and shows a notice. |
| `exclude` | Ids that other jobs in the **same request** already use on this site. "Create 3 accounts on shop.com" makes three calls, and each later call excludes the earlier ids. Never return an id from this list. |

**Response**: `200` with the identity object (§3).

**No one available**: `409 {"error": "no free employee for shop.example.com"}`, or `404`.

What your server can do here is up to you:
- pick an existing employee who isn't yet on `site`;
- **create** an address on the fly (`signup+shop@yourcompany.com`, or a new mailbox in the requested country) and
  a phone number from your SMS provider, then return them;
- record that `id` is now in use for `site`, so you don't hand it out again.

---

### 4.2 `GET /v1/identities/{id}` (required)

This returns the same person again. veil calls it on retries, on log-ins to an account it already knows, and
when you name a profile in chat ("sign up on x.com as emp-104").

**Response**: `200` with the identity object. **Unknown id**: `404 {"error": "no such identity"}`.

For a log-in to a site where veil already has a record, veil uses its own logged password even if this
call fails. It then has only the email and password, so `username` and `phone` are missing.

---

### 4.3 `GET /v1/identities` (optional)

This lists people so the chat planner can recognise names ("log in to reddit as emp-104"). It returns only
id and email. veil never asks for anything else here, and only ids and emails reach the LLM.

```json
{ "identities": [ { "id": "emp-104", "email": "asha.rao@yourcompany.com" }, { "id": "emp-105", "email": "..." } ] }
```

If you don't implement it, return `404` or anything else. veil treats a failure as an empty list and
assigns people on demand.

---

### 4.4 `POST /v1/identities/{id}/events` (optional)

veil sends this after every account run ends. It is best effort: veil sends it on a background thread, a
failure only goes to veil's log, and your response body is ignored. Reply `204`.

```json
{
  "site": "shop.example.com",
  "action": "signup",
  "status": "signed_up",
  "note": "Welcome page shown",
  "email": "asha.rao@yourcompany.com",
  "browser_identity": "acct-shop.example.com-emp-104",
  "at": "2026-10-09T14:03:11+0200",
  "password": "only when VEIL_IDENTITY_API_SEND_PASSWORDS=1"
}
```

`action` is `signup` or `login`. `status` is one of:

| status | meaning |
|---|---|
| `signed_up` | account created and confirmed working |
| `verify_email` | form sent; the site emailed a confirmation link (click it from the mailbox) |
| `logged_in` | veil is logged in to the account |
| `needs_human` | stopped at something only a person can do (CAPTCHA, a code that never arrived, a field with no value) |
| `already_registered` | the site says this email already has an account |
| `failed` | anything else; `note` says why |

Use these events to mark the employee as "has account on site", or to raise a ticket on `needs_human`.

**Passwords.** When your server doesn't supply one, veil generates it and stores it only in
`<data_dir>/accounts/signups.csv` on the veil PC. Set `VEIL_IDENTITY_API_SEND_PASSWORDS=1` if your server
should also receive it, for example so you can hand credentials to the employee. Use HTTPS when you do.

---

### 4.5 `GET /v1/identities/{id}/codes?channel=sms|email&site=...&since=...` (optional)

This is how the agent gets one-time codes without a human. When a site says "we sent a code to your phone or
email", the agent calls its `get_verification_code` action, and veil polls this endpoint every 5 seconds for up
to 90 seconds.

| Query | Meaning |
|---|---|
| `channel` | `sms` (the identity's `phone`) or `email` (its `email`) |
| `site` | the site that sent it, so you can filter messages by sender |
| `since` | Unix seconds. Return only codes received **after** this time; it is set to one minute before the run started. |

**Code available**: `200 {"code": "482913"}`. Return just the code, not the whole message.
**Not yet**: `404` or `204`. veil keeps polling until 90 seconds have passed.

If you don't implement this endpoint, every poll gets a 404. The agent then falls back to
`request_human_help`, and the person watching the app types the code.

How you get codes is up to you: an SMS provider's webhook storing inbound messages, an IMAP poll of the
mailbox, and so on. Pull the digits out with a regex such as `\b\d{4,8}\b`, and match on the sender and the
time.

The code is shown to the agent once so it can type it. It is kept out of the agent's long-term memory.

---

### 4.6 `GET /v1/identities/{id}/links?site=...&since=...` (optional)

This returns the confirmation link from a "verify your email" message. When a site says it has emailed a link,
the agent calls `get_verification_link`. veil polls this endpoint the same way as codes (every 5 seconds, for up
to 90 seconds). The agent then opens the link in the same tab, so the run ends `signed_up` instead of
`verify_email`.

| Query | Meaning |
|---|---|
| `site` | the site that sent it |
| `since` | Unix seconds. Return only links received after this time. |

**Link available**: `200 {"link": "https://shop.example.com/confirm?token=..."}`.
**Not yet**: `404` or `204`.

Return the link the person would click: the "Verify email" button, not unsubscribe or help links. Without this
endpoint, veil reports `verify_email` and someone clicks the link by hand.

---

## 5. The server

[`veil-identity-server`](../../veil-identity-server/README.md) (in `D:\Major Projecteil-identity-server`)
implements this whole contract:

- **Employees**: imported from CSV. A person is picked per site with no repeats, and assignments are reserved so
  two runs never get the same person.
- **SMS codes**: from Twilio (signature-checked), from any SMS gateway's webhook, or from an Android phone running
  an SMS-to-URL forwarder app.
- **Email codes and links**: by polling a shared catch-all mailbox or each person's own mailbox over IMAP
  (read-only), or by inbound-mail webhooks (SendGrid, Mailgun, Cloudflare, raw MIME).
- **Extraction**: the code (`G-123 456` becomes `123456`) and the confirmation link are pulled out of each
  message as it arrives. Messages naming the site come first.
- **Admin**: an admin API and the `ids` CLI.

You can also write your own server in any language. Anything that answers the endpoints above works.

---

## 6. What stays where

| Data | Lives on | Reaches the LLM? |
|---|---|---|
| Identity details (email, phone, name…) | your server; in veil's memory only during the run | **No.** The agent sees `x_email`, `x_phone`… and browser-use types the real value only on `*.site` |
| Generated password | veil's `signups.csv`; your server only with `SEND_PASSWORDS=1` | No |
| One-time code | your server, then the agent's current step | Yes, once, so it can type it |
| Ids and emails | `GET /v1/identities` | Yes, to the chat planner, so it can match "as emp-104" |
| Bearer token | veil's `.env` | No |

Notes for running it in a company:
- Use **HTTPS**, or keep the server on a LAN or VPN that only the veil PC can reach. The token is the only authentication.
- One `id` means one real person or mailbox. veil gives each `(site, id)` its own permanent device fingerprint,
  proxy session and cookies, so the same person always comes back from the same "computer".
- veil never creates a second account for an `(site, id)` that already has one. A later "sign up" there
  becomes a refusal with a hint to log in instead.

---

## 7. Where this is in the code

| File | Role |
|---|---|
| `src/veil/identities.py` | `IdentityAPI` (this contract), `LocalProfiles` (profiles.csv), `profile_source(settings)` picks one |
| `src/veil/accounts.py` | `plan_signup` / `plan_login` take a source: the log first (same person on retry), then `source.get` / `source.assign` |
| `src/veil/agent.py` | `get_verification_code` / `get_verification_link` actions, added when the source can fetch them |
| `src/veil/webui/server.py` | account runs call the source off the event loop, then report outcomes |
| `src/veil/config.py` | `VEIL_IDENTITY_API*` settings |
| `tests/test_identities.py` | a fake server implementing this document, and the tests that run against it |
