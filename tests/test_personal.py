"""Announcements, My Journeys reminders, calendar, punctuality, digest, helplines, new chat intents."""
import json
import os
import unittest
from datetime import date, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

from test_api import call

from railvision.api.app import App
from railvision.core.models import LiveObservation, ObsKind
from railvision.core.timeutil import IST
from railvision.news.fetch import FetchError, parse_feed, parse_web
from railvision.news.relevance import score, tag
from railvision.providers.demo import DemoProvider
from railvision.providers.registry import ProviderRegistry
from railvision.services.extras import punctuality
from railvision.services.store import Store

DAY = date(2026, 10, 15)


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>PIB</title>
<item><title>Railways announces festival special trains between New Delhi and Jaipur</title><link>https://pib.gov.in/PressReleaseIframePage.aspx?PRID=1</link></item>
<item><title>Ministry of AYUSH launches research portal</title><link>https://pib.gov.in/PressReleaseIframePage.aspx?PRID=2</link></item>
<item><title>12951 Mumbai Rajdhani cancelled on 16.10.2026 due to traffic block near Rewari</title><link>https://pib.gov.in/PressReleaseIframePage.aspx?PRID=3</link></item>
</channel></rss>"""
PAGE1 = """<html><body><nav><a href="/">Home</a></nav><ul><li><a href="/news/1.pdf">Commissioning of new platform at Alwar station</a></li>
<li><a href="/contact">Contact Us</a></li></ul></body></html>"""
PAGE2 = PAGE1.replace("</ul>", '<li><a href="/news/2.pdf">Diversion of 12951 via Alwar due to line block</a></li></ul>')


class FakeNet:
    def __init__(self):
        self.pages, self.calls = {}, []

    def __call__(self, url, headers):
        self.calls.append((url, headers))
        u = urlsplit(url)
        if u.path == "/robots.txt":
            r = self.pages.get(f"{u.scheme}://{u.netloc}/robots.txt")
            return (200, {}, r.encode()) if r else (404, {}, b"")
        if u.netloc == "api.x.com":
            return self.x(u)
        v = self.pages.get(url)
        if v is None:
            return 404, {}, b""
        if isinstance(v, tuple):
            if headers.get("If-None-Match") == v[1]:
                return 304, {}, b""
            return 200, {"etag": v[1]}, v[0].encode()
        return 200, {}, v.encode()

    def x(self, u):
        q = parse_qs(u.query)
        if "/users/by/username/" in u.path:
            return 200, {}, json.dumps({"data": {"id": "999", "username": "RailMinIndia"}}).encode()
        if q.get("since_id"):
            return 200, {}, json.dumps({"data": [{"id": "106", "text": "Train 12951 will run with an extra 3A coach from 16 Oct",
                                                  "created_at": "2026-10-15T05:00:00Z"}]}).encode()
        return 200, {}, json.dumps({"data": [{"id": "105", "text": "Fog: some trains in Northern Railway regulated", "created_at": "2026-10-15T04:00:00Z"},
                                             {"id": "104", "text": "Happy journey!", "created_at": "2026-10-15T03:00:00Z"}]}).encode()


def demo_app(clock, net=None):
    os.environ["RAILVISION_MODE"] = "demo"
    app = App(store=Store(), registry=ProviderRegistry("demo", [DemoProvider(clock=clock, anchor=datetime(2026, 10, 15, 6, 20, tzinfo=IST))]),
              clock=clock, logger=lambda *_: None, news_transport=net or FakeNet())
    uid, tok = app.store.create_user("me")
    return app, uid, tok


class RelevanceAndParsingTests(unittest.TestCase):
    def test_feed_and_web_parsing(self):
        items = parse_feed(RSS.encode())
        self.assertEqual(len(items), 3)
        self.assertTrue(items[0].url.endswith("PRID=1"))
        web = parse_web(PAGE2.encode(), "https://nwr.example.in/")
        self.assertEqual([w.url for w in web], ["https://nwr.example.in/news/1.pdf", "https://nwr.example.in/news/2.pdf"])
        atom = parse_feed(b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>a1</id><title>Rail block</title>'
                          b'<link href="https://x.in/a1"/><updated>2026-10-15T01:00:00Z</updated></entry></feed>')
        self.assertEqual((atom[0].title, atom[0].url), ("Rail block", "https://x.in/a1"))

    def test_entity_expansion_refused(self):
        with self.assertRaises(FetchError):
            parse_feed(b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa">]><rss><channel><item><title>&a;</title></item></channel></rss>')

    def test_tags_and_scores(self):
        t = tag("12951 Mumbai Rajdhani cancelled due to fog near Rewari, North Western Railway; PRID=2323619 ₹12000")
        self.assertEqual(t["trains"], ["12951"])
        self.assertEqual(t["stations"], ["RE"])
        self.assertIn("cancellation", t["categories"])
        self.assertEqual(score(t, {"trains": {"12951"}})[0], 1.0)
        self.assertEqual(score(t, {"trains": set(), "stations": {"RE"}})[0], 0.8)
        self.assertEqual(score(tag("New lift at Rewari station"), {"stations": {"RE"}})[0], 0.6)
        self.assertEqual(score(t, {"trains": set(), "stations": set(), "zones": {"North Western"}})[0], 0.4)
        self.assertEqual(score(tag("Mumbai suburban timetable"), {"stations": {"MMCT"}})[0], 0.0, "shared city names are not stations")


class NewsServiceTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock(datetime(2026, 10, 15, 9, 0, tzinfo=IST))
        self.net = FakeNet()
        self.app, self.uid, self.tok = demo_app(self.clock, self.net)
        self.news = self.app.news
        self.app.journeys.add(self.uid, {"train": "12951", "start_date": "2026-10-16", "board": "NDLS", "dest": "RE"})

    def test_pib_filtered_and_alerts_for_your_train(self):
        self.net.pages["https://pib.gov.in/RssMain.aspx?ModId=6&Lang=1&Regid=3"] = RSS
        s = self.news.tick()
        self.assertEqual((s["new"], s["errors"]), (2, 0), s)       # AYUSH item filtered out
        items = self.news.items(self.uid)
        self.assertEqual(len(items), 2)
        top = self.news.items(self.uid, relevant_only=True)
        self.assertEqual(top[0]["score"], 1.0)
        notes = self.app.store.list_notifications(self.uid)
        self.assertEqual([n["type"] for n in notes].count("ANNOUNCEMENT"), 1)
        self.assertEqual(self.news.tick()["fetched"], 0, "not due again before its interval")

    def test_web_page_baseline_then_new_links_and_etag(self):
        src = self.news.add_source({"kind": "web", "url": "https://nwr.example.in/notices", "name": "NWR notices"})
        self.net.pages["https://nwr.example.in/notices"] = (PAGE1, '"v1"')
        self.news.fetch_now(src["id"])
        self.assertEqual(self.news.items(self.uid), [], "existing links are a baseline, not news")
        self.news.fetch_now(src["id"])                             # 304 Not Modified
        self.assertIn('"v1"', [h.get("If-None-Match") for _, h in self.net.calls if "notices" in _])
        self.net.pages["https://nwr.example.in/notices"] = (PAGE2, '"v2"')
        r = self.news.fetch_now(src["id"])
        self.assertEqual(r["new"], 1)
        self.assertEqual(self.news.items(self.uid)[0]["trains"], ["12951"])

    def test_robots_txt_respected(self):
        self.net.pages["https://blocked.example.in/robots.txt"] = "User-agent: *\nDisallow: /"
        src = self.news.add_source({"kind": "web", "url": "https://blocked.example.in/news"})
        self.news.fetch_now(src["id"])
        s = next(x for x in self.news.sources() if x["id"] == src["id"])
        self.assertEqual(s["last_status"], "robots_disallow")
        self.assertFalse(any(u == "https://blocked.example.in/news" for u, _ in self.net.calls))

    def test_x_official_api_budget_and_since_id(self):
        self.news.x_token = "TEST"
        self.news.x_daily_posts = 7
        self.news.update_source("x_railminindia", {"enabled": True})
        self.news.fetch_now("x_railminindia")
        call = next(h for u, h in self.net.calls if "/tweets" in u)
        self.assertEqual(call["Authorization"], "Bearer TEST")
        use = self.news.x_usage_today()
        self.assertEqual((use["posts"], use["users"]), (2, 1))
        self.assertAlmostEqual(use["cost_usd"], 0.02)
        self.news.fetch_now("x_railminindia")
        self.assertTrue(any("since_id=105" in u for u, _ in self.net.calls))
        titles = [i["title"] for i in self.news.items(self.uid)]
        self.assertTrue(any("extra 3A coach" in t for t in titles))
        self.news.fetch_now("x_railminindia")                     # 3 read + up to 5 more > 7 -> budget stops the call
        s = next(x for x in self.news.sources() if x["id"] == "x_railminindia")
        self.assertEqual(s["last_status"], "budget")

    def test_x_without_token_is_reported(self):
        self.news.x_token = ""
        self.news.update_source("x_irctc", {"enabled": True})
        self.news.fetch_now("x_irctc")
        s = next(x for x in self.news.sources() if x["id"] == "x_irctc")
        self.assertEqual(s["last_status"], "not_configured")

    def test_source_validation(self):
        with self.assertRaises(ValueError):
            self.news.add_source({"kind": "web", "url": "ftp://x"})
        with self.assertRaises(ValueError):
            self.news.add_source({"kind": "x", "handle": "bad handle!"})
        with self.assertRaises(ValueError):
            self.news.add_source({"kind": "x", "handle": "NWRailways", "interval_min": 5})


class JourneyTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock(datetime(2026, 10, 15, 5, 0, tzinfo=IST))
        self.app, self.uid, self.tok = demo_app(self.clock)
        self.j = self.app.journeys.add(self.uid, {"train": "90001", "start_date": "2026-10-15", "board": "AWR", "dest": "DLI",
                                                  "cls": "3A", "buffer_min": 45, "alarm_min": 30, "pnr": "4512345678"})

    def kinds(self):          # oldest first
        return [n["payload"].get("kind") for n in reversed(self.app.store.list_notifications(self.uid, 200)) if n["type"] == "JOURNEY_REMINDER"]

    def at(self, h, m):
        self.clock.t = datetime(2026, 10, 15, h, m, tzinfo=IST)
        return self.app.journeys.tick()

    def test_reminder_timeline(self):
        self.at(5, 0)
        self.assertIn("day_before", self.kinds())
        self.assertIn("chart", self.kinds(), "chart ~4 h before the 06:20 origin departure, PNR not confirmed")
        ev = self.app.journeys.get(self.uid, self.j["id"])["last_eval"]
        self.assertEqual(ev["sched_dep"][11:16], "08:53")             # 90001 leaves JP 06:45, AWR +128 min
        self.assertEqual(ev["dep_basis"], "scheduled")
        self.assertNotIn("leave", self.kinds())
        # leave home: departure (08:28 + live delay) - 45 min travel - 15 min margin, announced 10 min early
        for h, m in [(7, 20), (7, 30), (7, 40), (7, 50), (8, 0)]:
            self.at(h, m)
        self.assertIn("leave", self.kinds())
        msg = next(n for n in self.app.store.list_notifications(self.uid) if n["payload"].get("kind") == "leave")
        self.assertIn("Leave by", msg["body"])
        self.assertIn("expected (live delay)", msg["body"])
        # destination alarm then arrival
        for h in range(10, 13):
            for m in range(0, 60, 10):
                self.at(h, m)
        for h, m in []:
            self.at(h, m)
        k = self.kinds()
        self.assertIn("dest_alarm", k)
        self.assertIn("arrived", k)
        self.assertLess(k.index("dest_alarm"), k.index("arrived"))
        self.assertEqual(self.app.journeys.get(self.uid, self.j["id"])["status"], "done")
        self.assertEqual(len(k), len(set(k)), "every reminder is sent once")

    def test_cancelled_train(self):
        j = self.app.journeys.add(self.uid, {"train": "90002", "start_date": "2026-10-15", "board": "AWR", "dest": "DLI"})
        self.at(6, 0)
        self.assertEqual(self.app.journeys.get(self.uid, j["id"])["status"], "cancelled")
        self.assertIn("cancelled", self.kinds())

    def test_station_not_on_route(self):
        j = self.app.journeys.add(self.uid, {"train": "90001", "start_date": "2026-10-15", "board": "NDLS", "dest": "DLI"})
        self.at(5, 0)
        self.assertIn("not on this train", self.app.journeys.get(self.uid, j["id"])["last_eval"]["note"])

    def test_validation_and_isolation(self):
        for bad in ({"train": "123"}, {"start_date": "2020-01-01"}, {"board": "DLI"}, {"pnr": "12"}, {"cls": "XX"}):
            with self.assertRaises(ValueError):
                self.app.journeys.add(self.uid, {**{"train": "90001", "start_date": "2026-10-15", "board": "AWR", "dest": "DLI"}, **bad})
        other, tok2 = self.app.store.create_user("other")
        self.assertIsNone(self.app.journeys.get(other, self.j["id"]))
        self.assertEqual(call(self.app, "GET", f"/api/journeys/{self.j['id']}", token=tok2)[0], 404)

    def test_pnr_result_updates_trip_and_alerts_on_change(self):
        pr = {"pnr": "4512345678", "passengers": [{"n": 1, "booking": {"raw": "WL/12"}, "current": {"status": "RAC", "raw": "RAC/4"}}]}
        self.app.journeys.apply_pnr(self.uid, pr)
        pr2 = {"pnr": "4512345678", "all_confirmed": True,
               "passengers": [{"n": 1, "booking": {"raw": "WL/12"}, "current": {"status": "CNF", "coach": "B3", "number": "45",
                                                                              "berth_type": "LB", "raw": "CNF/B3/45/LB"}}]}
        self.app.journeys.apply_pnr(self.uid, pr2)
        j = self.app.journeys.get(self.uid, self.j["id"])
        self.assertEqual((j["coach"], j["berth"]), ("B3", "45 LB"))
        self.assertTrue(any(k.startswith("pnr_change") for k in self.kinds()))
        with self.assertRaises(ValueError):
            self.app.journeys.check_pnr(self.uid, self.j["id"])            # demo mode: no browser assistant

    def test_calendar_export(self):
        self.at(5, 0)
        st, hd, body = call(self.app, "GET", f"/api/journeys/{self.j['id']}.ics", query={"token": self.tok})
        self.assertEqual(st, 200)
        self.assertEqual(hd["Content-Type"], "text/calendar; charset=utf-8")
        ics = body.decode() if isinstance(body, bytes) else body
        for needle in ("BEGIN:VCALENDAR", "BEGIN:VEVENT", "SUMMARY:🚆 90001 AWR→DLI", "DTSTART:20261015T032300Z",
                       "TRIGGER:-PT60M", "PNR 4512345678", "END:VCALENDAR"):
            self.assertIn(needle, ics)
        self.assertEqual(call(self.app, "GET", f"/api/journeys/{self.j['id']}.ics")[0], 401)


class StatsDigestHelpChatTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock(datetime(2026, 10, 15, 6, 58, tzinfo=IST))
        self.app, self.uid, self.tok = demo_app(self.clock)

    def test_punctuality(self):
        obs = []
        for k, d in enumerate([3, 12, 40, 0, 7]):
            sd = DAY - timedelta(days=k + 1)
            t = datetime.combine(sd, datetime.min.time(), tzinfo=IST) + timedelta(hours=10)
            for kind, dl in ((ObsKind.ARRIVED, d), (ObsKind.DEPARTED, d + 2)):
                obs.append(LiveObservation(train_number="12951", service_date=sd, kind=kind, station_code="RE", event_time=t,
                                           source_id="s", source_time=t, retrieved_at=t, delay_min=dl))
        self.app.store.save_observations(obs)
        p = punctuality(self.app.store, "12951", "RE")
        s = p["stations"]["RE"]
        self.assertEqual((s["runs"], s["median"], s["max"], s["on_time_pct"]), (5, 7, 40, 40))
        st, _, b = call(self.app, "GET", "/api/stats/punctuality", token=self.tok, query={"train": "12951", "station": "RE"})
        self.assertEqual(st, 200)

    def test_digest_once_a_day_only_when_relevant(self):
        self.assertEqual(self.app.digest.tick(), 0, "nothing personal -> no digest")
        self.app.journeys.add(self.uid, {"train": "90001", "start_date": "2026-10-15", "board": "AWR", "dest": "DLI"})
        self.app.journeys.tick()
        self.assertEqual(self.app.digest.tick(), 0, "before 07:00")
        self.clock.t = datetime(2026, 10, 15, 7, 1, tzinfo=IST)
        self.assertEqual(self.app.digest.tick(), 1)
        self.assertEqual(self.app.digest.tick(), 0, "once a day")
        body = [n for n in self.app.store.list_notifications(self.uid) if n["type"] == "DIGEST"][0]["body"]
        self.assertIn("90001 AWR→DLI", body)
        self.assertIn("139", body)

    def test_prefs_api(self):
        st, _, p = call(self.app, "PATCH", "/api/prefs", {"digest_time": "06:30", "zones": ["north western"], "keywords": ["fog"],
                                                          "digest_destinations": ["whatsapp", "nope"]}, self.tok)
        self.assertEqual((p["digest_time"], p["zones"], p["digest_destinations"]), ("06:30", ["North Western"], ["app", "whatsapp"]))

    def test_help_and_chat_intents(self):
        st, _, h = call(self.app, "GET", "/api/help")
        self.assertEqual([x["number"] for x in h["helplines"]], ["139", "182", "112"])
        r = call(self.app, "POST", "/api/chat", {"text": "rpf helpline number"}, self.tok)[2]
        self.assertIn("182", r["reply"])
        self.app.journeys.add(self.uid, {"train": "90001", "start_date": "2026-10-15", "board": "AWR", "dest": "DLI"})
        self.app.journeys.tick()
        r = call(self.app, "POST", "/api/chat", {"text": "my trips"}, self.tok)[2]
        self.assertIn("90001 AWR→DLI", r["reply"])
        r = call(self.app, "POST", "/api/chat", {"text": "PNR 4512345678"}, self.tok)[2]
        self.assertIn("browser assistant", r["reply"])
        r = call(self.app, "POST", "/api/chat", {"text": "any announcements?"}, self.tok)[2]
        self.assertEqual(r["intent"], "news")


if __name__ == "__main__":
    unittest.main()
