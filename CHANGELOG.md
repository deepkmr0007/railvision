# Changelog

## 2.1.0 — 15 Oct 2026
* **Browser assistant**: checks the official NTES running-status and IRCTC vacant-berth pages from your own computer,
  one train at a time, on your interval. Watch list of up to 10 trains, each with its start date.
* TEACH mode records your own search once; the recipe is reused for every train.
* Never solves CAPTCHAs, logins or bot checks: hands the visible window to you, or pauses that site (30 min → 4 h).
* NTES parser (tables / text / the page's own JSON) with read confidence; expected times are never treated as actual.
* IRCTC vacant-berth parser and train-name search.
* Chatbot: unknown train → official page check; "search train rajdhani" / "rajdhani train khojo" → name search.
* Dashboard: Watch list view (add trains, berth checks, check now, page screenshot + parsed result), token sign-in.
* One-command start: `start.bat` / `./start.sh` / `python -m railvision.launch`.
* 117 automated tests, including an end-to-end run against a local mock site.

## 2.0.0 — 14 Oct 2026
* Crossing engine, tracking, chatbot, WhatsApp/Telegram/email/push delivery, command-center dashboard, 90 tests.
