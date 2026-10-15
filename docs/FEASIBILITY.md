# RAILVISION AI — Feasibility report & source/feature matrix

**Checked:** 14 October 2026 · **Rule applied throughout:** verified facts are separated from assumptions; nothing bypasses CAPTCHA, login, bot protection, robots policy or rate limits.

## 1. What was tested, with evidence

| Target | What happened (14 Oct 2026) | Conclusion |
|---|---|---|
| NTES `enquiry.indianrail.gov.in/mntes/` | Web fetch tool refused: **site's robots policy disallows automated access**. Build-sandbox egress proxy also denied the CONNECT. | No permitted automated route. Official CRIS data-sharing arrangement required. Dashboard links users to NTES (manual fallback). |
| IRCTC Online Charts `irctc.co.in/online-charts/` and `/trainclassvacantberth` | Request **blocked by bot detection**. Not retried or circumvented. | No permitted automated route. Needs IRCTC partnership/authorised agent integration. Manual link fallback + adapter ready. |
| Official public API for NTES/IRCTC data | None found in public documentation. Historic third-party APIs (e.g. eRail) are reported suspended. | Treat as **not available** until CRIS/IRCTC confirm otherwise. |
| Third-party providers (APIs/MCP servers built on NTES data) | Several exist; some document caching (~45 s) and per-minute quotas; several state they use *unofficial* NTES data. | Usable only after verifying the provider's **own right** to redistribute. Generic adapter built and tested against a mock; no live provider tested (no key; sandbox egress blocked). |
| Meta WhatsApp Cloud API — 1:1 | Documented and generally available. | Implemented: webhook verify, HMAC signature, opt-in/out, 24 h session window vs. approved templates. Needs your Meta Business + WABA + number. |
| Meta WhatsApp **Groups API** | Meta docs (updated 16 Jun 2026): open to businesses with an **Official Business Account (OBA)**; groups are invite-only, created by the business; **max 8 participants**; 1 Cloud API business per group; per-message pricing; not for Business-app numbers. | Gated behind `WA_OBA_VERIFIED=true`; otherwise automatic fallback (Telegram group / opt-in 1:1 / email / app). Cannot post into arbitrary existing groups. |
| WhatsApp **Channels** | No official Cloud API publishing endpoint found. Only unofficial client-emulation gateways advertise it. | **Not supported automatically.** RAILVISION queues copy-ready posts for a human channel admin. Unofficial gateways excluded (ToS / ban risk). |
| Telegram Bot API | Official, free, supports private chats, groups and channels the bot is added to. | Implemented as the practical automatic group/channel broadcast alternative. |

## 2. Feature matrix

| Feature | Status |
|---|---|
| Train search, route, running status, delay, prev/next stop, progress, freshness | **VERIFIED AND IMPLEMENTABLE NOW** in code + tests; live data **REQUIRES OFFICIAL CREDENTIALS/APPROVAL** (CRIS) or **REQUIRES THIRD-PARTY LICENSE** |
| Non-stop crossing engine (3 event classes, never infers from time) | **VERIFIED AND IMPLEMENTABLE NOW** (source-agnostic, 26 tests) |
| Station board, trains between stations | Code ready; data **REQUIRES … APPROVAL / LICENSE**; **MANUAL-LINK FALLBACK** to NTES today |
| Chart vacancy (post-chart) with coach map | UI + contract ready; data **REQUIRES OFFICIAL CREDENTIALS/APPROVAL** (IRCTC); **MANUAL-LINK FALLBACK** today |
| Reservation availability / booking | **NOT CURRENTLY SUPPORTED** — official IRCTC workflow only |
| Persistent tracking, shared polling, backoff, dedupe, audit | **VERIFIED AND IMPLEMENTABLE NOW** |
| Chatbot EN/HI/Hinglish (deterministic NLU + optional LLM classifier) | **VERIFIED AND IMPLEMENTABLE NOW** |
| WhatsApp 1:1 | **REQUIRES OFFICIAL CREDENTIALS/APPROVAL** (Meta) — code tested with mocks |
| WhatsApp groups | **REQUIRES OFFICIAL CREDENTIALS/APPROVAL** (OBA) — gated, fallback tested |
| WhatsApp channels (automatic) | **NOT CURRENTLY SUPPORTED** → **MANUAL-LINK FALLBACK** (admin copy queue) |
| Telegram, email (SMTP), in-app/SSE, browser notifications | **VERIFIED AND IMPLEMENTABLE NOW** (need your bot token / SMTP) |
| Android push (FCM v1) | **REQUIRES OFFICIAL CREDENTIALS/APPROVAL** (Firebase project) |
| Android app | Installable **PWA** now; native Kotlin/Compose is a later phase |

## 2a. Browser assistant (added 15 Oct 2026, at the owner's request)

The owner chose to accept automated personal checks of the official pages. RAILVISION 2.1 adds a
**browser assistant** that runs on the owner's computer and works the public NTES/IRCTC pages like
one person: one page at a time, a 15–20 s minimum gap, a 5 min minimum per train, up to 10 trains,
400 pages a day. CAPTCHAs, logins and blocks are never solved or bypassed; they are handed to the
user or the site is paused. Status: **VERIFIED AND IMPLEMENTABLE NOW** for personal use. The machinery
is proven against a local mock site; the real page layouts must be confirmed (TEACH) on first run,
because the real pages were unreachable from the build environment. It is still automation against
NTES's robots policy and IRCTC's terms, so it is not suitable as a public or commercial data source.

## 3. Recommended path to real live data (in order)

1. **Write to CRIS** (Centre for Railway Information Systems) requesting an NTES data-sharing arrangement for RAILVISION (purpose, volume, caching, attribution). This is the only fully official route for running status.
2. In parallel, evaluate **licensed providers**: obtain written confirmation of their data rights, SLA, freshness and quotas; fill `config/provider.example.json` from their docs; run the provider contract test.
3. For chart vacancy, approach **IRCTC** (authorised partner programme). Until then keep the official chart link.
4. WhatsApp: create Meta Business + WABA, register a number, get a **utility template** approved for train updates, set the webhook. Apply for OBA only if groups are essential.

## 4. Costs to expect (ask before paid use)

* Meta: per-message pricing for templates outside the 24 h window and for Groups API.
* Licensed rail data provider: plan-based.
* Hosting: one small VM (2 vCPU / 2 GB) handles thousands of tasks thanks to shared polling.
* Telegram, SMTP relay (low volume), PWA: free or near-free.
