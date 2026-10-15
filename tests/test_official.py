"""Official zonal railway websites: press-release lists, detail pages and PDF notices."""
import base64
import os
import sqlite3
import sys
import unittest
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).parent / "mock_sites"))

from mock_server import ZONE_DETAIL, zone_list  # noqa: E402
from pdfmini import make_pdf  # noqa: E402
from test_api import call  # noqa: E402
from test_personal import Clock, demo_app  # noqa: E402

from railvision.api.app import App  # noqa: E402
from railvision.core.timeutil import IST  # noqa: E402
from railvision.news.official import ZONE_PAGES, html_to_text, pdf_to_text  # noqa: E402
from railvision.news.service import NewsService  # noqa: E402
from railvision.providers.registry import ProviderRegistry  # noqa: E402
from railvision.services.store import Store  # noqa: E402

BASE = "https://nwr.mock.example/view_section.jsp?lang=0&id=0,4,268"
PDF = make_pdf(["Due to a traffic block between Rewari and Alwar,", "Train No. 12951 will remain cancelled on 20.10.2026"])


class ZoneNet:
    """Mock zonal portal (same CMS shape as xx.indianrailways.gov.in)."""

    def __init__(self, items=1, robots=None):
        self.items, self.robots, self.calls = items, robots, []

    def __call__(self, url, headers):
        self.calls.append(url)
        u = urlsplit(url)
        if u.path == "/robots.txt":
            return (200, {}, self.robots.encode()) if self.robots else (404, {}, b"")
        if u.path == "/view_section.jsp":
            return 200, {"content-type": "text/html"}, zone_list(self.items).replace('href="/zone/', 'href="/').encode()
        if u.path == "/view_detail.jsp":
            return 200, {"content-type": "text/html"}, ZONE_DETAIL[u.query.split("dcd=")[1]].encode()
        if u.path.endswith(".pdf"):
            return 200, {"content-type": "application/pdf"}, PDF
        return 404, {}, b""


class OfficialPagesTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock(datetime(2026, 10, 15, 9, 0, tzinfo=IST))
        self.net = ZoneNet()
        self.app, self.uid, self.tok = demo_app(self.clock, self.net)
        self.news = self.app.news
        self.app.journeys.add(self.uid, {"train": "12951", "start_date": "2026-10-20", "board": "NDLS", "dest": "JP"})

    def src(self):
        s = self.news.add_source({"kind": "web", "url": BASE, "name": "NWR press (mock)",
                                  "link_filter": r"view_detail\.jsp|\.pdf(\?|$)|/uploads/"})
        return s["id"]

    def test_zone_pages_built_in(self):
        z = [s for s in self.news.sources() if s["id"].startswith("zone_")]
        self.assertEqual(len(z), len(ZONE_PAGES))
        self.assertEqual(sum(1 for s in z if s["verified"]), 7)
        self.assertTrue(all(s["link_filter"] for s in z))

    def test_menus_ignored_details_read_and_pdf_alerts(self):
        sid = self.src()
        self.news.fetch_now(sid)
        self.assertEqual(self.news.items(self.uid), [], "first visit = baseline")
        self.net.items = 3
        r = self.news.fetch_now(sid)
        self.assertEqual(r["new"], 2, "only announcement links, never the menu")
        items = {i["title"][:12]: i for i in self.news.items(self.uid)}
        pdf_item = items["Cancellation"]
        self.assertEqual(pdf_item["detail_status"], "ok")
        self.assertIn("12951 will remain cancelled", pdf_item["text"])
        self.assertEqual(pdf_item["trains"], ["12951"], "train number found inside the PDF, not in the title")
        self.assertEqual(pdf_item["score"], 1.0)
        sp = items["Extension of"]
        self.assertTrue(sp["text"].startswith("Extension of special trains"), "detail text starts at the title, menus dropped")
        self.assertNotIn("Tenders and Recruitment", sp["text"])
        alerts = [n for n in self.app.store.list_notifications(self.uid) if n["type"] == "ANNOUNCEMENT"]
        self.assertEqual(len(alerts), 1)
        self.assertIn("traffic block", alerts[0]["body"].lower())

    def test_robots_disallow_suggests_assistant(self):
        self.net.robots = "User-agent: *\nDisallow: /"
        sid = self.src()
        self.news.fetch_now(sid)
        s = next(x for x in self.news.sources() if x["id"] == sid)
        self.assertEqual(s["last_status"], "robots_disallow")
        self.assertIn("browser assistant", s["last_detail"])
        self.assertFalse(any("view_section" in u for u in self.net.calls))

    def test_auto_follow_respects_your_choice(self):
        self.news.update_source("zone_nwr", {"enabled": False})       # you switched it off
        self.news.tick()
        on = {s["id"]: s["enabled"] for s in self.news.sources()}
        self.assertTrue(on["zone_nr"], "NDLS is in Northern Railway")
        self.assertFalse(on["zone_nwr"], "not re-enabled after you turned it off")

    def test_text_helpers(self):
        t = html_to_text(ZONE_DETAIL["103"], "Extension of special trains")
        self.assertTrue(t.startswith("Extension of special trains"))
        self.assertIn("04815/04816", t)
        self.assertEqual(pdf_to_text(b"not a pdf")[0], "")
        self.assertIn("12951", pdf_to_text(PDF)[0])

    def test_migration_from_v220_database(self):
        db = sqlite3.connect(":memory:")
        db.executescript("""CREATE TABLE news_sources (id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL, url TEXT, handle TEXT,
          interval_min INTEGER NOT NULL, enabled INTEGER NOT NULL, official INTEGER NOT NULL, rail_filter INTEGER NOT NULL DEFAULT 0,
          verified TEXT, notes TEXT, last_fetch_at TEXT, next_due_at TEXT, last_status TEXT, last_detail TEXT, etag TEXT,
          last_modified TEXT, x_user_id TEXT, since_id TEXT, fail_count INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
          CREATE TABLE news_items (id INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT UNIQUE NOT NULL, source_id TEXT NOT NULL, title TEXT,
          text TEXT, url TEXT, author TEXT, published_at TEXT, fetched_at TEXT NOT NULL, trains TEXT, stations TEXT, categories TEXT,
          zones TEXT, baseline INTEGER NOT NULL DEFAULT 0, official INTEGER NOT NULL DEFAULT 1);""")
        st = Store()
        st.db = db
        st.db.row_factory = sqlite3.Row
        from railvision.services.store import SCHEMA
        st.db.executescript(SCHEMA)
        NewsService(st, None, transport=ZoneNet())
        cols = {r[1] for r in db.execute("PRAGMA table_info(news_sources)")}
        self.assertTrue({"link_filter", "fetch_via", "zone", "user_set"} <= cols)


class AssistantReadsOfficialPagesTests(unittest.TestCase):
    """Server side of 'read with my browser assistant' (the browser part is in test_assist_e2e)."""

    def setUp(self):
        os.environ["RAILVISION_MODE"] = "live"
        self.clock = Clock(datetime(2026, 10, 15, 9, 0, tzinfo=IST))
        self.app = App(store=Store(), registry=ProviderRegistry("live", []), logger=lambda *_: None, assist=True, clock=self.clock,
                       news_transport=ZoneNet())
        os.environ["RAILVISION_MODE"] = "demo"
        self.uid, self.tok = self.app.store.create_user("me")
        self.app.journeys.add(self.uid, {"train": "12951", "start_date": "2026-10-20", "board": "NDLS", "dest": "JP"})
        st, _, s = call(self.app, "POST", "/api/news/sources", {"kind": "web", "url": BASE, "fetch_via": "assistant"}, self.tok)
        self.assertEqual(st, 201, s)
        self.sid = s["id"]

    def capture(self, links=None, **kw):
        return {"kind": "page", "recipe": "web_page", "status": "ok", "vars": kw.pop("vars"), "links": links or [], "text": "", **kw}

    def test_list_then_details_via_assistant(self):
        work = call(self.app, "GET", "/api/assist/work", token=self.tok)[2]
        page = [j for j in work["jobs"] if j["kind"] == "page"]
        self.assertEqual(page[0]["vars"], {"url": BASE, "source_id": self.sid})
        self.assertEqual(self.app.news.tick(only=self.sid)["fetched"], 0, "the server never fetches an assistant-only page itself")
        links = [{"t": "Contact Us", "h": "https://nwr.mock.example/contact"},
                 {"t": "Commissioning of new foot over bridge at Bandikui station", "h": "https://nwr.mock.example/view_detail.jsp?dcd=101"}]
        call(self.app, "POST", "/api/assist/captures", self.capture(links, vars={"url": BASE, "source_id": self.sid}), self.tok)
        links.append({"t": "Cancellation / partial cancellation of trains due to traffic block", "h": "https://nwr.mock.example/uploads/n102.pdf"})
        self.app.news.store.x("UPDATE news_sources SET next_due_at=NULL WHERE id=?", (self.sid,))
        st, _, r = call(self.app, "POST", "/api/assist/captures", self.capture(links, vars={"url": BASE, "source_id": self.sid}), self.tok)
        self.assertEqual(r["headline"], "1 new announcements")
        jobs = call(self.app, "GET", "/api/assist/work", token=self.tok)[2]["jobs"]
        det = [j for j in jobs if j["kind"] == "page" and j["vars"].get("item_id")]
        self.assertEqual(len(det), 1)
        self.assertTrue(det[0]["vars"]["url"].endswith(".pdf"))
        cap = self.capture(vars=det[0]["vars"], job_id=det[0]["job_id"], pdf_b64=base64.b64encode(PDF).decode())
        call(self.app, "POST", "/api/assist/captures", cap, self.tok)
        it = [i for i in self.app.news.items(self.uid) if i["title"].startswith("Cancellation")][0]
        self.assertEqual((it["trains"], it["score"]), (["12951"], 1.0))
        self.assertTrue(any(n["type"] == "ANNOUNCEMENT" for n in self.app.store.list_notifications(self.uid)))

    def test_fetch_via_validation(self):
        st, _, b = call(self.app, "POST", "/api/news/sources", {"kind": "rss", "url": "https://x.example/feed", "fetch_via": "assistant"}, self.tok)
        self.assertEqual(st, 422)
        st, _, b = call(self.app, "PATCH", f"/api/news/sources/{self.sid}", {"fetch_via": "direct"}, self.tok)
        self.assertEqual(b["fetch_via"], "direct")


if __name__ == "__main__":
    unittest.main()
