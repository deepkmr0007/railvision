# Changelog

## 2.6.0 — 12 Nov 2026
* **Vacant berths screen (Chart vacancy tab).** In the app this tab used to show only the demo train. It is now a real
  tool: enter train number, journey date, boarding station (code or name) and class (or all classes) and get **every
  vacant berth on the IRCTC chart: coach, berth/seat number, berth type, and the stations it is vacant from → to**, with
  filters (class, coach, berth type, only usable from your station), counts per class, chart number/time, and Share.
* **Three ways to read the chart**, tried in order on `www.irctc.co.in/online-charts`:
  1. the quick data request;
  2. filling in the page's form automatically and reading the data the page then loads;
  3. if neither works, the app opens the IRCTC page and asks you to fill in the form and press *Get Train Chart*;
     the moment the page loads the chart, the app reads every coach by itself.
* **Failures are spelled out** on the screen (for example "HTTP 403 on the quick request"), with the steps the assistant
  took, *Try again*, *Open the IRCTC page* and *Share diagnostic file*.
* Station names are accepted for the boarding station where the app knows them; otherwise type the station code.
* 161 tests.

## 2.5.0 — 11 Nov 2026
* **Fast lane.** With the official page open in the assistant's browser, the app now asks the site for the data the same
  way the page's own buttons do (same origin, same cookies, read-only), instead of typing into the form every time.
  * **Vacant berths (IRCTC charts):** reads the chart's own data — every coach of the class, every berth, each stretch of
    the journey — and counts a berth as vacant only where the chart says so. The old reader expected a table that the
    real page never shows. "Usable from your station" is counted only when the chart proves the stretch covers it.
  * **Running status (NTES):** one quick request per train; the answer lists every current run, and only the run with
    your start date is used. If that run is not listed, nothing is taken from another day.
  * Falls back to the form automatically; a refusal (HTTP 401/403/429), CAPTCHA or login is still reported, never bypassed.
* **Faster rounds.** Checks on the same site run back to back on the page that is already open: about 6–9 s apart instead
  of 20–30 s (first load of a site still waits 20 s). Watch interval can now be 2 or 3 minutes; default is 5.
* **Share diagnostic file** (Settings → Phone app, or *Download diagnostic file* on desktop): what the assistant saw on
  its last checks, so a page it cannot read can be fixed from evidence. PNR checks are never included.
* Partial routes (only the stations reached so far) are marked open-ended and can never trigger "journey completed".
* One page driver (`railvision/assist/rv_driver.js`) now serves both the phone app and the desktop assistant.
* CI uses the runner's Chrome on Ubuntu 24.04. 159 tests.

## 2.4.0 — 15 Oct 2026
* **Android app (APK)**, built by GitHub Actions and published as a GitHub Release on every push.
  * The Python engine runs inside the app (Chaquopy, Python 3.12), on 127.0.0.1 only; data stays in app storage.
  * Built-in browser assistant: the app's own WebView with `rv_driver.js`, the same recipes, pacing (≥20 s, 400 pages/day),
    and CAPTCHA/login hand-over (a notification opens the assistant browser so you can finish the step).
  * Android notifications for all alerts; a check every 15 min with the app closed; optional *Keep running* service;
    restored after a reboot or an update.
  * Dashboard: *Settings → Phone app* (assistant on/off, keep running, battery, notifications, open assistant browser).
* `railvision/mobile.py` entry point; `RAILVISION_WEB_DIR` override.
* 7 new tests drive the phone driver in Chromium with the app's job loop against all mock sites (153 total).

## 2.3.0 — 15 Oct 2026
* **Official railway websites**: press-release pages of all 16 zones built in; menus filtered out; each new notice opened
  and read (HTML or PDF via pypdf) so train numbers inside notices trigger alerts; zones of your trips/stations auto-follow.
* Pages whose robots.txt refuses automated readers can be read by your browser assistant instead (per source switch).
* Browser assistant: new `web_page` recipe (pages and PDFs), links captured; detail jobs queued after each list check.
* Database migration for existing installs; station → zone map; Announcements tab shows the notice text.
* 146 automated tests.

## 2.2.0 — 15 Oct 2026
* **My Journeys**: trips with heads-up, chart-time, leave-home (live delay), wake-up and arrived reminders; auto watch list;
  PNR status via the browser assistant (CAPTCHA typed by you) with change alerts; `.ics` calendar export.
* **Announcements hub**: PIB RSS (railway-filtered), official web pages (robots.txt, ETag, baseline), NTES
  cancelled/rescheduled/diverted lists, X via the official API with a daily budget; tagging by train, station, city,
  zone and topic; personal relevance score and alerts.
* Delay history per train and station; once-a-day digest that only sends when something concerns you; helplines.
* Chat: `PNR …`, `my trips`, `announcements` / `news for 12951`, `helpline`.
* Dashboard: My Journeys, Announcements, Help tabs; digest and alert settings.
* Fixes: stored times follow the app clock; train-number-only notices are no longer dropped by the railway filter.
* 137 automated tests.

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
