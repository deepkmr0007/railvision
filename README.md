# RAILVISION AI — Indian Railways Live Intelligence Command Center

Running status · **non-stop station crossing** · station boards · chart vacancy · persistent tracking with alerts · English/Hindi/Hinglish assistant · WhatsApp / Telegram / email / app delivery · premium command-center dashboard (installable PWA).

> **Honest status:** the whole platform runs and is tested end-to-end on clearly labelled **synthetic demo data**. Real live railway data needs an authorized source (CRIS/NTES arrangement or a licensed provider) — see `docs/FEASIBILITY.md`. Nothing here scrapes NTES/IRCTC or bypasses their protections.

## Run it (Python 3.11+, zero dependencies)

```bash
python -m railvision.api.server --port 8000      # demo mode, opens on http://127.0.0.1:8000
python -m unittest discover -s tests              # 90 tests
python tools/export_demo_snapshot.py              # rebuild the offline replay dashboard (dist/)
```

Try in the dashboard's Assistant: `track 90001 from Rewari to Delhi every 15 min` → `yes`. Demo trains: **90001** (normal, non-stop at Rewari), **90002** (cancelled), **90003** (diverted, drops Rewari), **90004** (reverse direction).

## Go live

1. `cp .env.example .env`, set `RAILVISION_MODE=live`, a strong `RAILVISION_SECRET`, `RAILVISION_APP_URL`.
2. Fill `config/provider.example.json` from your **authorized** provider's documentation; set `RAILVISION_PROVIDER_CONFIG` and the key env var.
3. WhatsApp: set `WA_PHONE_NUMBER_ID`, `WA_ACCESS_TOKEN`, `WA_APP_SECRET`, `WA_VERIFY_TOKEN`, `WA_GRAPH_VERSION` (current version from Meta docs) and an approved `WA_TEMPLATE_NAME`; point the Meta webhook at `https://<host>/webhooks/whatsapp`.
4. Telegram (optional group broadcast): `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`; `setWebhook` to `https://<host>/webhooks/telegram` with that secret.
5. Production: `pip install -r requirements.txt`, `docker compose -f deploy/docker-compose.yml up` (API + worker + Postgres + Caddy TLS).

## Layout

```
railvision/core        crossing engine, status builder, models, IST time
railvision/providers   demo, generic licensed HTTP adapter, official-link fallback, source matrix
railvision/services    store, tracker (shared polling), messages, resilience, WhatsApp bot
railvision/delivery    WhatsApp (1:1 / OBA groups / channel queue), Telegram, email, FCM, in-app SSE
railvision/chatbot     EN/HI/Hinglish NLU, chat engine, optional LLM classifier
railvision/api         framework-agnostic app, stdlib server, FastAPI wrapper
web/                   dashboard + PWA manifest + service worker
docs/                  FEASIBILITY.md · ARCHITECTURE.md · PRIVACY.md · API.md
PROJECT_STATUS.md      completed / tested / blockers / costs / next steps
```
