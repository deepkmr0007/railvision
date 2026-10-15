# HTTP API (all JSON, times ISO-8601 with +05:30)

Auth: `Authorization: Bearer <token>` (SSE: `?token=`). Public endpoints are rate-limited per IP.

| Method | Path | Notes |
|---|---|---|
| GET | /api/health | mode, version, time |
| GET | /api/sources | capability matrix, source health, delivery channel availability |
| GET | /api/stations?q= | ambiguity-aware station search (EN/HI) |
| GET | /api/trains/search?q= | train search (provider-dependent) |
| GET | /api/trains/{no}/status?date=&station= | running status (+ crossing verdict if station given) |
| GET | /api/trains/{no}/route?date= | scheduled stops with halt/non-stop, km, day-of-run |
| GET | /api/trains/{no}/crossing?station=&date= | crossing verdict; also appended to audit trail |
| GET | /api/trains/{no}/passage-history?station= | 🔒 audit trail of verdict changes |
| GET | /api/stations/{code}/board?hours= | station board |
| GET | /api/between?from=&to=&date= | trains between stations |
| GET | /api/vacancy?train=&from=&class=&date= | post-chart vacancy + summary + disclaimer |
| POST | /api/session | demo/signup-enabled only: returns a token once |
| GET/POST | /api/tasks | 🔒 list / draft (needs confirm unless `"confirm": true`) |
| GET/PATCH/DELETE | /api/tasks/{id} | 🔒 owner only |
| POST | /api/tasks/{id}/confirm\|pause\|resume\|cancel | 🔒 |
| POST | /api/chat `{text, session_id}` | 🔒 assistant |
| GET | /api/notifications?after= | 🔒 |
| GET | /api/events?token= | 🔒 Server-Sent Events stream |
| POST | /api/whatsapp/link-code | 🔒 one-time code (15 min) |
| GET | /api/channel-queue · POST /api/channel-queue/{id}/published | 🔒 WhatsApp Channel admin copy queue |
| GET | /api/reports/summary | 🔒 tasks, alerts by type, engine metrics |
| GET/POST | /webhooks/whatsapp | Meta verify handshake / signed inbound (X-Hub-Signature-256) |
| POST | /webhooks/telegram | requires X-Telegram-Bot-Api-Secret-Token |

Errors: 400 bad input · 401 auth/signature · 404 not found or **MANUAL-LINK FALLBACK** (`official_url`) · 413 body too large · 422 task rule · 429 rate limit · 503 source unavailable/not authorized.
