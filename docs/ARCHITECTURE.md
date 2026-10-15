# RAILVISION AI — Architecture

```
           ┌────────────── Providers (adapters, normalized contract) ───────────────┐
           │ DemoProvider (synthetic)   GenericHttpProvider (licensed/authorized)   │
           │ OfficialLinkProvider (manual fallback: NTES / IRCTC pages)            │
           └───────────────┬────────────────────────────────────────────────────────┘
                           │ LiveObservation / Route / VacancySnapshot (IST, source+timestamps)
   cache · rate limit · circuit breaker · backoff (services/resilience.py)
                           │
 ┌─────────────────────────▼─────────────────────────┐
 │ core/crossing.py  — 3-class crossing engine       │   core/status.py — running status
 │ (direct / downstream-derived / estimated / N.V.)  │   (reported vs timetable, freshness)
 └─────────────────────────┬─────────────────────────┘
                           │
 services/tracker.py — persistent tasks, SHARED POLLING per (train, date), dedupe keys,
                       passage audit trail, completion, quiet hours, delay alerts
                           │
 delivery/ — Dispatcher → app (SSE) · browser · WhatsApp 1:1 · WhatsApp group (OBA-gated)
             · WhatsApp channel (admin copy queue) · Telegram · email · Android push
                           │
 chatbot/ — NLU (EN/HI/Hinglish) → tools → grounded reply; optional LLM classifier (validated JSON)
                           │
 api/app.py — framework-agnostic routes, auth, rate limits, security headers, webhooks
   ├─ api/server.py      stdlib HTTP server + SSE (dev / single node)  ← tested
   └─ api/fastapi_app.py ASGI wrapper for production                   ← not executed in sandbox
 web/index.html — command-center dashboard (PWA), also builds as an offline demo replay
```

## Key decisions

* **Source-agnostic core.** All rail logic consumes normalized observations, so swapping in an authorized provider is configuration, not a rewrite.
* **Demo/live isolation is enforced in code**, not by convention: the registry refuses demo providers in live mode, the crossing engine raises on mixed data, the message composer refuses demo payloads in live mode, and every demo object carries `is_demo` + a visible banner.
* **Time:** every timestamp is timezone-aware IST; timetable times are resolved from the service date (origin date) + day-of-run, so midnight crossings are exact.
* **Shared polling:** one upstream fetch per (train, service date) per tick, regardless of subscriber count; provider cache adds a second layer.
* **Idempotent notifications:** every message has a deterministic dedupe key enforced by a UNIQUE constraint; restarts and retries never double-send.
* **Audit:** each change in a crossing verdict is appended (`passage_events`, `supersedes` link), so corrections stay visible.

## Core entities (tables)

Train/Station/Route/ScheduledStop (provider + `stations.py`), `observations` (LiveObservation, with trace id), `passage_events` (PassageEvent + Prediction), `tracking_tasks` (TrackingTask/Subscription), `notifications`, `delivery_attempts`, `consent_records`, `wa_links` (WhatsAppAccountLink), `telegram_links`, `link_codes`, `processed_messages` (replay guard), `chat_context`, `channel_queue`, `audit_events`. PostgreSQL DDL: `deploy/postgres_schema.sql`.

## Scaling notes

* SQLite + one worker is fine for a single node. For multiple workers move to PostgreSQL and claim due tasks with `SELECT … FOR UPDATE SKIP LOCKED`; move `TTLCache`/`KeyedRateLimiter` to Redis (interfaces are deliberately tiny).
* The SSE bus is per-process; with several API replicas, publish via Redis pub/sub.
