# Browser assistant — checking the official pages for you

The assistant runs **on your own computer**. It opens the official NTES and IRCTC pages in a normal
Chromium window, types the train number and start date like you would, reads the result, and sends
it to RAILVISION. It handles **one train at a time**, then waits before the next.

## Start

| Computer | Do this once | Then every time |
|---|---|---|
| Windows | Install Python 3.11+ from python.org (tick "Add to PATH") | double-click `start.bat` |
| Mac / Linux | Python 3.11+ | `./start.sh` |

The first start installs Playwright and Chromium (about 150 MB). A dashboard tab opens already
signed in, and a second browser window appears. That window is the assistant: leave it open.

Your private files live in `assist_data/`: the database, `token.txt` (your sign-in key), the
browser profile and recorded recipes. Don't share `token.txt`.

## Watch up to 10 trains

Dashboard → **Watch list** → *Add a train*:

* **Train number** and **start date**: the date the train left its origin. For an overnight train
  you boarded today, that is often *yesterday*; use the "Started yesterday" button.
* **Check every**: 5 to 60 minutes. All trains are checked one after another, never at the same time.
* **Watch station / Until** (optional): turns on crossing alerts. You get "passed Rewari" even
  when the train doesn't stop there, confirmed only when the page proves it.
* **Vacant berth check** (optional): boarding station, class and journey date for the IRCTC chart.
* **Send updates to**: app, WhatsApp, Telegram, email or browser.

Each train card shows the last check, what the page said, the read confidence, and a
**screenshot of the exact page** that was read.

Asking the assistant also works: *"where is 12951"*, *"12951 kahan hai"*,
*"search train rajdhani"*. If the train isn't on your watch list, a one-off check is queued.

## Teach it the real page (recommended on first use)

The pages could not be opened from the environment where this was built, so the first run uses the
**automatic form-filler**. It finds the train box, date and button by their labels, but this hasn't
yet been confirmed on the real NTES or IRCTC pages. If a check says **Needs teaching**, or the read
confidence is low, record the steps once:

```bash
python -m railvision.assist teach ntes_status --train 12951 --date 2026-10-15 --server http://127.0.0.1:8000 --token <token.txt>
python -m railvision.assist teach irctc_berth --train 12951 --date 2026-10-15 --station NDLS --cls 3A --server http://127.0.0.1:8000 --token <token.txt>
python -m railvision.assist teach ntes_search --query rajdhani --server http://127.0.0.1:8000 --token <token.txt>
```

Do the search in the window that opens, wait for the result, and press **Save recipe** in the
bottom-right corner. The train number and date you typed become slots, so the recipe works for
every train. If NTES has a "show intermediate stations" option, click it while teaching. That lets
the assistant see stations where the train doesn't stop.

To try one check and see what was read (a capture is saved in `assist_data/captures/` for tuning):

```bash
python -m railvision.assist run ntes_status --train 12951 --date 2026-10-15
```

## Limits it keeps on its own

* One page at a time: at least 15–20 s (plus a random 0–10 s) between page loads.
* At least 5 min between checks of the same train, at most 10 trains, at most 400 page loads per day.
* **CAPTCHA, login or "access denied"**: never solved or bypassed. With the visible window, it
  stops and asks you (alert + WhatsApp). In headless mode it pauses that site for 30 minutes,
  doubling up to 4 hours.
* No stealth plugins, no faked browser identity, no proxy rotation.

## Read rules

* A station counts as reached **only** if the page says Departed/Arrived/Crossed, or it is before
  the station the page names as the current position. "Expected" times never become actual events.
* Menu text like "Cancelled Trains" never marks your train cancelled.
* Below 60 % read confidence, no live events are created. You see the page text and screenshot
  instead, and an alert tells you to check it.

## Please note

The NTES robots policy says no to automated access, and IRCTC's terms forbid automated tools.
This assistant behaves like one person checking a few trains, but it is still automation.
**Use it for your own trains, at low rates; don't run it as a public service or resell the data.**
For anything public or commercial, use an authorized source (see `FEASIBILITY.md`). Booking always
stays on IRCTC; RAILVISION never books, and never asks for your IRCTC password.

## Troubleshooting

| You see | Do this |
|---|---|
| Needs teaching | Run `teach` for that recipe (above). |
| CAPTCHA — needs you | Solve it in the assistant window yourself, or wait; the site is paused for a while. |
| Site refused | The site blocked the request. It's paused automatically; try again later and keep the interval at 15 min or more. |
| No result | Wrong start date is the usual cause. Try "Started yesterday". |
| Low read confidence | Open the capture and screenshot; send the file from `assist_data/captures/` to tune the parser. |
| Agent offline | The `start` window was closed or the computer slept. Run `start` again. |
