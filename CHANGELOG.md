# Changelog

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
