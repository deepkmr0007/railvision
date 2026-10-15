# RAILVISION AI — Project status

**Date:** 15 Oct 2026 · **Build:** v2.1.0 · **Test evidence:** `python -m unittest discover -s tests` → **117 tests, all passing**
(crossing 26 · WhatsApp/messaging 16 · providers 14 · chatbot 13 · parsers 13 · tracker 11 · API + real TCP server/SSE 10 ·
assist service/API 9 · pacer 1 · browser end-to-end 4). Dashboard v2.1 rendered headless at 1280 px (light) and 390 px (dark)
across all 10 views: no script errors, no horizontal overflow.

## v2.1 — browser assistant (official pages, personal use)

| Area | What works | Evidence |
|---|---|---|
| Sequential loop | Up to 10 trains, each with a start date, checked one after another on your interval; berth checks after each train's status | e2e test 1: 10 status pages + 1 berth chart, in order, every read ≥80 % confidence |
| Getting to the result | Automatic form-filler (menu link, train box, autocomplete, start-date chip / dropdown / date box, submit) worked unchanged on two different mock layouts; TEACH records your own clicks with {train}/{date} slots and replays them for other trains | e2e tests 1, 2, 4 |
| Human checks | CAPTCHA, login and "access denied" detected; never solved; window handed to you, or the site paused 30 min → 4 h; you get an alert | e2e test 3, pacer test |
| Reading | NTES parser for tables, labelled text and the page's own JSON; expected times never become actual; menu text never cancels a train; below 60 % confidence no live events are created | 13 parser tests |
| Vacant berths | IRCTC chart parser (JSON / table / text), "chart not prepared" detection; counts matched the mock exactly | parser tests, e2e test 1 |
| Name search | Chat "search train rajdhani" / "rajdhani train khojo" → official-site search → alert with the list | e2e test 2 |
| Server | Watch list (≤10, owner-only), leased work queue, capture + screenshot storage, on-demand jobs, alerts on change, crossing tasks for a watch station | 9 service/API tests |
| One command | `start.bat` / `./start.sh` / `python -m railvision.launch`: live mode, private token, signed-in dashboard, assistant window | launcher smoke test (server part) |

**Not verified yet:** the real NTES and IRCTC page layouts. Both sites were unreachable from the build
environment, so the first real run must confirm them (automatic mode or TEACH, about 1 minute each).
The parser may also need tuning on real captures. Send the files from `assist_data/captures/` if
read confidence stays below 80 %.

## Completed and tested

| Area | What works | Evidence |
|---|---|---|
| Crossing engine | SCHEDULED HALT / CONFIRMED PASSAGE (direct or proven by a later station, time bracketed) / ESTIMATED (late-skewed range) / NOT VERIFIED / NOT ON ROUTE / CANCELLED. Midnight, day-of-run, diversion, short termination, reschedule, reversed direction, duplicates, source corrections, out-of-order, stale data, mixed demo/live guard. Never infers crossing from the clock. | test_crossing.py |
| Running status | Reported position vs timetable kept separate; prev/next halt, delay, progress, platform only if supplied, freshness, alerts | test_providers, test_api |
| Providers | Normalized adapter contract; synthetic demo; config-driven licensed HTTP adapter with cache, rate limit, retries, circuit breaker, https-only; official manual-link fallback; registry refuses demo/live mixing | test_providers.py |
| Tracking | Draft → confirm, pause/resume/cancel/delete, interval change, delay threshold, quiet hours, completion, shared polling (1 fetch for N tasks), exponential backoff, stale warnings, DB-enforced notification dedupe, passage audit trail, per-user isolation | test_tracker.py |
| Assistant | EN / Hindi / Hinglish intents for every example in the brief; follow-up questions; route-aware station disambiguation ("Delhi" → DLI on this train); confirmation summary before persistent tasks; Hindi replies; optional LLM classifier with schema validation | test_chatbot.py |
| WhatsApp | Webhook verify, HMAC-SHA256 signature, replay/duplicate guard, opt-in, STOP/START/DELETE MY DATA, account LINK codes, 24 h session text vs approved template, masked phone logging, failure reporting; Groups gated on OBA with fallbacks; Channels → admin copy queue | test_whatsapp.py |
| Other delivery | Telegram (webhook secret, per-chat rate limit), SMTP email, FCM v1 push, in-app + SSE browser notifications; every attempt recorded | test_whatsapp, test_api |
| API & security | Bearer auth, owner-only tasks, per-IP / per-user / webhook rate limits, 256 KB body cap, CSP + security headers, CORS allowlist, no internal error leakage, signup disabled in live mode | test_api.py |
| Dashboard | Overview, Live train (km-true line diagram, timetable, approximate map), Crossing monitor (time-window axis, confidence, evidence, audit trail, all-station ledger), Station board, Chart vacancy (coach/bay map, segment detail, three-concept explainer), Tracking & alerts (task form, feed with per-channel delivery results), Assistant, Data sources, Settings (theme, low-bandwidth, notifications, WhatsApp link). PWA installable. Offline demo replay built from real engine output. | headless render check |

## Not tested here (by necessity)

* **Any live railway source** — NTES disallows automated access (robots), IRCTC blocked by bot protection; no licensed provider key; sandbox egress blocked.
* **Live WhatsApp / Telegram / SMTP / FCM sends** — no credentials; payloads and flows tested with mocks.
* **FastAPI/uvicorn entrypoint** — PyPI unreachable in the build sandbox; it is a thin wrapper over the tested `App.handle()`.
* Load tests and mobile background behaviour on a real device.

## Blockers (owner action needed)

1. Authorized running-status source: CRIS/NTES arrangement **or** licensed provider with proven redistribution rights.
2. IRCTC authorization for chart vacancy (until then: official link).
3. Meta Business + WABA + phone number + approved utility template; OBA only if WhatsApp groups are essential.
4. Domain + TLS host; Firebase project for Android push (optional).

## Costs (approval required before any paid use)

Meta per-message charges (templates outside 24 h, Groups API) · licensed data provider plan · ~1 small VM · Telegram/PWA free.

## Next steps

0. On your computer: run `start`, add 2–3 trains, and if a check says *Needs teaching*, run the TEACH command once for
   `ntes_status` (and `irctc_berth` if you use berth checks). Share one capture file if confidence is low.

1. Pick and contract a data source → fill `config/provider.example.json` → add a recorded-response contract test → switch `RAILVISION_MODE=live`.
2. Meta setup → get the 8-parameter update template approved → end-to-end test on a real phone (1:1 only).
3. Deploy with `deploy/docker-compose.yml`; run the worker; verify SSE through Caddy.
4. PostgreSQL store adapter (schema ready in `deploy/postgres_schema.sql`), Redis cache/limits for multi-worker scale.
5. Native Android (Kotlin + Compose) client on the same API, FCM for background alerts.
6. Load the full authorized station master (codes, Hindi names, coordinates) to replace the seed list.
