# Personal features (v2.2)

All of this runs on your own computer with `start.bat` / `./start.sh`.

## My Journeys

Add a trip: train, **start date** (the day it leaves its origin), from, to, class, and optionally
your PNR, your travel time to the station, and how early to wake you before your stop.

| Reminder | When |
|---|---|
| Heads-up | The day before (or the same morning): departure time, seat, platform if known |
| Chart time | About 4 h before the train leaves its origin, only while your berth isn't confirmed |
| Leave home | Expected departure − your travel time − 15 min. Uses the live delay. A second message comes if it moves 15+ min |
| Wake-up | When the expected arrival at your stop is within your chosen minutes (live position when available) |
| Arrived | When the source reports the train at your stop; the trip is then marked done |

Each time is labelled **scheduled**, **expected (live delay)**, **estimated from live position**
or **reported**. Two days before the trip, the train is added to the Watch list automatically.
Each trip shows a small **delay history** for your boarding station, built from the runs RAILVISION
has seen. **Add to calendar** gives an `.ics` file with a leave-home alarm.

## PNR status

*My Journeys → Check PNR*, or send `PNR 4512345678` to the assistant or WhatsApp bot. The browser
assistant opens the official PNR page and fills in the number. **You type the CAPTCHA** in that
window; it never solves one. The result (booking status → current status, chart status) comes
back to the dashboard and WhatsApp. If your berth becomes confirmed, the coach and berth are saved
to the trip, and any status change triggers an alert.

## Announcements

| Source | How it's read | Default |
|---|---|---|
| PIB press releases | Official RSS, filtered to railway items | On (English), Hindi optional |
| NTES cancelled / rescheduled / diverted lists | Browser assistant, every 2 h while you watch a train or have a trip | On |
| Official web pages you add (zonal railways, divisions) | New links on the page; first visit is a baseline; robots.txt respected | Add your own |
| X: @RailMinIndia, @IRCTCofficial, @RailwaySeva, + any handle you add | **Official X API only**, with your key | Off until you add a key |

Each item is tagged with train numbers, stations (English and Hindi names), city, zone and topic
(cancellation, diversion, fog, special trains, …), then scored for you:

* **1.0** mentions your train · **0.8** disruption at your station · **0.7** disruption in your
  station's city · **0.6** your station · **0.5** your station's city · **0.4** disruption in your zone.
* Alerts go to your phone at **0.8+** by default (Settings → *Alert me for*). The *For you* feed
  shows everything from 0.4.

### Official railway websites (v2.3)

All 16 zonal railways have a **press-release page** built in (*Announcements → Official railway
websites*). RAILVISION checks the page every 2 hours, ignores menus and footers, and then **opens
each new notice**, a web page or a PDF, so train numbers inside the notice count too.

* Zones of your trips, watched trains and saved stations switch on by themselves (Rewari or Jaipur →
  North Western; New Delhi → Northern …). If you switch one off, it stays off.
* **Direct** reading follows each site's robots.txt. On 15 Oct 2026 the zonal sites' robots.txt
  refused automated readers, so expect "robots_disallow" there. Press **Via assistant** on that zone
  and your own browser window reads the page and the PDFs like you would: up to 5 notices per check,
  inside the assistant's normal pacing. This is personal use, the same as the NTES checks.
* PDF notices need `pypdf` (installed by the start scripts). Scanned PDFs have no text, so you see
  the title and link only.
* Addresses: 7 zones were confirmed by web search (NR, WR, NER, SWR, SCR, SER, CR). The other 9 use
  the same portal pattern but are unconfirmed. If one shows HTTP 404, open the zone's site, go to
  *Press Releases*, and add that address as a web page.

### X (Twitter) setup

1. Create a developer app at the X developer console and buy credits (X charges per use: about
   $0.005 per post read and $0.010 per account lookup, checked October 2026; see docs.x.com for
   current prices).
2. Put the app's bearer token in `.env`: `X_BEARER_TOKEN=...` and optionally
   `X_DAILY_POST_BUDGET=60` (≈ $0.30/day). RAILVISION stops for the day at the budget.
3. In Announcements, switch the X accounts on. Only new posts are fetched each time; replies and
   reposts are skipped.

RAILVISION never scrapes x.com or logs in to it.

## Daily digest

One message a day (default 07:00), and only when something concerns you: today's and tomorrow's
trips with expected times and PNR status, your watch list, and announcements from the last 24 h
that concern your trains. Choose the time and where it goes in Settings.

## Help

The Help tab, and "helpline" in chat, give **139** (Rail Madad: enquiry, complaints, medical,
security), **182** (RPF security) and **112** (national emergency), plus official links.
