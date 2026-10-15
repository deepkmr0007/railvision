# RAILVISION AI — Indian Railways Live Intelligence Command Center

Running status · **non-stop station crossing** · station boards · chart vacancy · persistent tracking with alerts · English/Hindi/Hinglish assistant · WhatsApp / Telegram / email / app delivery · premium command-center dashboard (installable PWA).

## New in 2.4: Android app (APK)

**Install on your phone:** open **https://github.com/deepkmr0007/railvision/releases/latest** on the phone and tap
**RailVision.apk** (Android 8.0+). The whole engine runs inside the app; its built-in browser assistant checks your
trains on the official pages one at a time, and alerts arrive as phone notifications. No computer needed.
Guide: [`docs/ANDROID.md`](docs/ANDROID.md). Every push rebuilds the APK with GitHub Actions and publishes it as a Release.

## New in 2.3: official railway websites

Press releases from all 16 zonal railway websites, including the notice itself (web page or PDF), matched to your trains
and stations. Zones of your trips switch on automatically; sites that refuse automated readers can be read by your own
browser assistant instead. See [`docs/PERSONAL.md`](docs/PERSONAL.md#official-railway-websites-v23).

## New in 2.2: built for your own trips

* **My Journeys**: heads-up, chart-time, leave-home (follows the live delay) and wake-up-before-your-stop
  reminders, PNR status (you type the CAPTCHA), delay history, calendar export.
* **Announcements**: PIB press releases, official railway pages you choose, NTES cancelled/diverted/rescheduled
  lists, and X posts from @RailMinIndia and others (official X API with a spending cap). Anything about
  your train or station is flagged and sent to your phone.
* **Daily digest**, **Help** (139 · 182 · 112), and chat commands: `PNR 4512345678`, `my trips`, `news for 12951`, `helpline`.

Guide: [`docs/PERSONAL.md`](docs/PERSONAL.md)

## New in 2.1: the browser assistant (real official pages)

Run **`start.bat`** (Windows) or **`./start.sh`** (Mac/Linux). The dashboard opens signed in, and a
browser window checks your **watch list of up to 10 trains** on the official NTES running-status page,
one train at a time, every few minutes. It can also check **IRCTC vacant berths** for a train and start
date you choose, and search trains by name. Results feed the crossing engine, alerts and WhatsApp.
Full guide: [`docs/BROWSER_ASSISTANT.md`](docs/BROWSER_ASSISTANT.md).

> **Status:** tested end-to-end against a local mock site. The real NTES and IRCTC pages
> could not be reached from the build environment, so on first use teach the assistant the real pages
> once (one command, ~1 minute). It never solves CAPTCHAs or bypasses bot checks. Automating these pages
> goes against NTES's robots policy and IRCTC's terms: personal, low-rate use only.

## Demo mode (Python 3.11+, zero dependencies)

```bash
python -m railvision.api.server --port 8000      # demo mode, opens on http://127.0.0.1:8000
python -m unittest discover -s tests              # 153 tests (browser tests need: pip install playwright pypdf)
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
railvision/assist      browser assistant: runner, auto form-filler, TEACH recorder, polite agent loop, CLI
railvision/parsers     NTES running-status, exception lists, PNR, IRCTC vacant-berth and train-search parsers
railvision/news        announcements: RSS, official web pages, X official API, relevance scoring
railvision/api         framework-agnostic app, stdlib server, FastAPI wrapper
web/                   dashboard + PWA manifest + service worker
docs/                  PERSONAL.md · BROWSER_ASSISTANT.md · FEASIBILITY.md · ARCHITECTURE.md · PRIVACY.md · API.md
tests/mock_sites       local mock railway pages used by the end-to-end tests
PROJECT_STATUS.md      completed / tested / blockers / costs / next steps
```

## Push this repository to GitHub

```bash
git remote add origin https://github.com/deepkmr0007/railvision.git
git push -u origin main --tags
```
