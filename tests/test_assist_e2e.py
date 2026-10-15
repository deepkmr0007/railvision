"""End-to-end: real RAILVISION server  ⇄  real agent  ⇄  real Chromium  ⇄  LOCAL MOCK site.

Proves the machinery (sequential loop over 10 trains, pacing, recipes, CAPTCHA/block handling,
teach + replay, parsing, alerts). It does NOT prove anything about the real NTES/IRCTC pages,
which were unreachable from the build environment.
"""
import os
import sys
import threading
import time
import unittest
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "mock_sites"))

try:
    from playwright.sync_api import sync_playwright
    HAVE_PW = True
except ImportError:
    HAVE_PW = False

from mock_server import IST, TRAINS, make_server  # noqa: E402

from railvision.api.app import App  # noqa: E402
from railvision.api.server import make_handler  # noqa: E402
from railvision.assist.agent import Agent, Client, Pacer  # noqa: E402
from railvision.assist.teach import teach  # noqa: E402
from railvision.providers.registry import ProviderRegistry  # noqa: E402
from railvision.services.store import Store  # noqa: E402


@unittest.skipUnless(HAVE_PW and os.environ.get("RV_SKIP_E2E") != "1", "Playwright not installed")
class AssistEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mock = make_server(0)
        cls.mbase = f"http://127.0.0.1:{cls.mock.server_address[1]}"
        os.environ["RAILVISION_MODE"] = "live"
        cls.app = App(store=Store(), registry=ProviderRegistry("live", []), logger=lambda *_: None, assist=True)
        os.environ["RAILVISION_MODE"] = "demo"
        cls.app.assist.pacing.update(min_gap_s=0.2, jitter_s=0.1)     # tests only; real agents keep a 15 s floor
        cls.uid, cls.tok = cls.app.store.create_user("e2e")
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.app))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        cls.client = Client(cls.base, cls.tok)
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()
        cls.today = datetime.now(IST).date().isoformat()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.srv.shutdown()
        cls.mock.shutdown()

    def set_recipe(self, name, path, mode="auto", steps=None):
        from railvision.assist.recipes import DEFAULT_RECIPES
        d = {**DEFAULT_RECIPES[name].to_dict(), "start_url": self.mbase + path, "mode": mode, "steps": steps or []}
        self.client.call("PUT", f"/api/assist/recipes/{name}", d)

    def agent(self, logs):
        ctx = self.browser.new_context()
        return Agent(self.client, ctx, headed=False, log=logs.append, pacer=Pacer(min_gap_s=0.2, jitter_s=0.1, floor_s=0.2))

    def test_1_ten_trains_one_by_one_with_berth_and_crossing(self):
        self.set_recipe("ntes_status", "/ntes-a/")
        self.set_recipe("irctc_berth", "/irctc/")
        for lk in ("cancelled", "rescheduled", "diverted"):
            self.set_recipe(f"ntes_{lk}", "/ntes-a/")
        trains = sorted(TRAINS)[:10]
        for i, t in enumerate(trains):
            body = {"train": t, "start_date": self.today, "interval_min": 5,
                    "ref_station": "RE" if i == 0 else None, "end_station": "DLI" if i == 0 else None,
                    "berth_checks": [{"station": "AWR", "class": "3A"}] if i == 1 else []}
            self.client.call("POST", "/api/assist/watch", body)
        with self.assertRaises(Exception):
            self.client.call("POST", "/api/assist/watch", {"train": "12999", "start_date": self.today, "interval_min": 5})
        logs = []
        ag = self.agent(logs)
        t0 = time.time()
        n = ag.round()
        took = time.time() - t0
        self.assertEqual(n, 14)            # 3 NTES exception lists + 10 status pages + 1 berth chart
        starts = [ln for ln in logs if ln.startswith("→ status")]
        self.assertEqual([s.split()[2] for s in starts], trains, "trains are checked one at a time, in order")
        watch = self.client.call("GET", "/api/assist/watch")["watch"]
        self.assertTrue(all(w["last_status"] == "ok" for w in watch), [(w["train"], w["last_status"], w["last_reason"]) for w in watch])
        caps = self.client.call("GET", "/api/assist/captures?limit=50")["items"]
        st_caps = [c for c in caps if c["kind"] == "status"]
        self.assertEqual(len(st_caps), 10)
        self.assertTrue(all(c["confidence"] >= 0.8 for c in st_caps), [c["confidence"] for c in st_caps])
        berth = next(c for c in caps if c["kind"] == "berth")
        truth = len(self.mock.data.berths(trains[1], self.today, "AWR", "3A"))
        self.assertEqual(berth["parse"]["vacancy"]["total_vacant"], truth)
        # official-page data drives the normal APIs
        s = self.client.call("GET", f"/api/trains/{trains[0]}/status?date={self.today}&station=RE")
        self.assertIn(s["crossing"]["event_class"], ("CONFIRMED_PASSAGE", "ESTIMATED_CROSSING", "NOT_VERIFIED"))
        self.assertFalse(s["status"]["is_demo"])
        notes = self.client.call("GET", "/api/notifications")["items"]
        self.assertTrue({"STATUS_REPORT", "VACANCY_CHANGE"} <= {x["type"] for x in notes}, {x["type"] for x in notes})
        self.assertEqual(self.client.call("GET", "/api/assist/work")["watch"], [], "nothing due again before the interval")
        # exception lists: watched train 12956 is on the mock "cancelled" list -> announcement + alert
        lists_caps = [c for c in caps if c["kind"] == "list"]
        self.assertEqual({c["parse"]["list"] for c in lists_caps}, {"cancelled", "rescheduled", "diverted"})
        news = self.client.call("GET", "/api/news?relevant=1")["items"]
        self.assertTrue(any("12956" in n["title"] and n["score"] == 1.0 for n in news), [n["title"] for n in news])
        self.assertTrue(any(x["type"] == "ANNOUNCEMENT" and "12956" in x["body"] for x in notes + self.client.call("GET", "/api/notifications")["items"]))
        print(f"\n   e2e: 11 pages in {took:.1f}s, mock hits {self.mock.stats['hits']}")

    def test_2_table_layout_and_on_demand_jobs(self):
        self.set_recipe("ntes_status", "/ntes-b/")
        self.set_recipe("ntes_search", "/ntes-a/")
        r = self.client.call("POST", "/api/chat", {"text": "where is 12977"})     # not on the watch list
        self.assertIn("asked your browser assistant", r["reply"])
        self.client.call("POST", "/api/chat", {"text": "search train mock express"})
        logs = []
        self.assertEqual(self.agent(logs).round(), 2)
        res = [n for n in self.client.call("GET", "/api/notifications")["items"] if n["type"] == "ASSIST_RESULT"]
        bodies = " ".join(n["body"] for n in res)
        self.assertIn("RUNNING STATUS", bodies)
        self.assertIn("MOCK EXPRESS", bodies)
        r = self.client.call("POST", "/api/chat", {"text": "where is 12977"})
        self.assertIn("Last reported", r["reply"])

    def test_3_captcha_pauses_site_and_never_solves(self):
        self.set_recipe("ntes_status", "/ntes-b/?mode=captcha")
        self.client.call("POST", "/api/assist/jobs", {"kind": "status", "vars": {"train": "12907", "date": self.today}})
        self.client.call("POST", "/api/assist/jobs", {"kind": "status", "vars": {"train": "12914", "date": self.today}})
        logs = []
        ag = self.agent(logs)
        ag.round()
        jobs = self.client.call("GET", "/api/assist/jobs")["items"]
        first = next(j for j in jobs if '"12907"' in j["vars"])
        self.assertEqual(first["status"], "failed")
        self.assertTrue(any("skipped" in ln and "paused" in ln for ln in logs), logs)
        self.assertEqual(self.client.call("GET", "/api/assist/status")["agent"]["state"], "paused")
        failed = [n for n in self.client.call("GET", "/api/notifications")["items"] if n["type"] == "ASSIST_RESULT"]
        self.assertTrue(any("Could not read" in n["body"] and "CAPTCHA" in n["body"] for n in failed), failed)
        self.assertFalse(any("captcha" in ln.lower() and "solved" in ln.lower() for ln in logs))

    def test_5_pnr_you_type_the_captcha(self):
        self.client.call("PUT", "/api/assist/recipes/pnr_status",
                         {"kind": "pnr", "start_url": self.mbase + "/pnr/?autosolve=1", "mode": "auto", "result_texts": ["{pnr}"]})
        j = self.client.call("POST", "/api/journeys", {"train": "12951", "start_date": self.today, "board": "NDLS", "dest": "MMCT",
                                                       "cls": "3A", "pnr": "4512345678"})
        self.client.call("POST", f"/api/journeys/{j['id']}/check-pnr")
        logs = []
        ag = self.agent(logs)
        ag.headed = True                       # handover allowed: the mock page's script plays the person typing the CAPTCHA
        ag.round()
        self.assertTrue(any("Your turn" in ln or "CAPTCHA" in ln for ln in logs), logs)
        j2 = self.client.call("GET", f"/api/journeys/{j['id']}")
        p = j2["last_pnr"]
        self.assertEqual([x["current"]["status"] for x in p["passengers"]], ["RAC", "CNF"])
        self.assertIs(p["chart_prepared"], False)
        res = [n for n in self.client.call("GET", "/api/notifications")["items"] if n["type"] == "ASSIST_RESULT" and "PNR" in n["body"]]
        self.assertTrue(res and "RAC/4" in res[0]["body"])
        # headless agents must never get past a CAPTCHA
        self.client.call("POST", f"/api/journeys/{j['id']}/check-pnr")
        ag2 = self.agent([])
        ag2.round()
        failed = [x for x in self.client.call("GET", "/api/assist/jobs")["items"] if x["kind"] == "pnr"][0]
        self.assertEqual(failed["status"], "failed")

    def test_6_official_zonal_pages_read_by_your_browser(self):
        self.client.call("POST", "/api/journeys", {"train": "12951", "start_date": self.today, "board": "NDLS", "dest": "JP"})
        src = self.client.call("POST", "/api/news/sources", {"kind": "web", "url": self.mbase + "/zone/view_section.jsp?lang=0&id=0,4,268",
                                                             "name": "Zonal press releases (mock)", "fetch_via": "assistant",
                                                             "link_filter": r"view_detail\.jsp|\.pdf(\?|$)|/uploads/"})
        logs = []
        ag = self.agent(logs)
        ag.round()                                              # first visit: baseline (1 old item)
        self.mock.zone_state["items"] = 3
        self.app.news.store.x("UPDATE news_sources SET next_due_at=NULL WHERE id=?", (src["id"],))
        ag.round()                                              # list again: 2 new -> detail jobs queued
        ag.round()                                              # detail page + PDF, read in the browser
        uid = self.app.store.user_for_token(self.tok)
        items = {i["title"][:12]: i for i in self.app.news.items(uid) if i["source_id"] == src["id"]}
        self.assertEqual(set(items), {"Cancellation", "Extension of"}, "menus and footer links are never items")
        pdf_it = items["Cancellation"]
        self.assertEqual(pdf_it["detail_status"], "ok", pdf_it.get("detail_note"))
        self.assertIn("12951 will remain cancelled", pdf_it["text"])
        self.assertEqual(pdf_it["score"], 1.0)
        self.assertIn("04815/04816", items["Extension of"]["text"])
        self.assertTrue(any("read PDF" in ln for ln in logs) or any("notice102.pdf" in ln for ln in logs), logs)
        notes = self.client.call("GET", "/api/notifications")["items"]
        self.assertTrue(any(n["type"] == "ANNOUNCEMENT" and "12951" in n["body"] + n["title"] for n in notes))

    def test_4_teach_then_replay_for_other_trains(self):
        page = self.browser.new_page()
        today = datetime.now(IST).date()
        done = {"v": False}

        def human(pg, raw):
            if done["v"]:
                return True
            done["v"] = True
            pg.click("text=Spot Your Train")
            pg.type("#trn", "12951", delay=30)
            pg.wait_for_timeout(300)
            pg.click("li")
            pg.click(f"text={today.strftime('%d-%b-%Y')}")
            pg.click("text=Get Status")
            pg.wait_for_timeout(1000)
            return False
        rc = teach(page, "ntes_status", "status", self.mbase + "/ntes-a/", {"train": "12951", "date": today}, auto_done=human, log=lambda s: None)
        page.close()
        self.assertEqual(rc.mode, "recorded")
        self.assertIn("{train}", [s.value for s in rc.steps] + [s.text for s in rc.steps])
        self.client.call("PUT", "/api/assist/recipes/ntes_status", rc.to_dict())
        self.client.call("POST", "/api/assist/jobs", {"kind": "status", "vars": {"train": "12935", "date": self.today}})
        logs = []
        self.agent(logs).round()
        cap = self.client.call("GET", "/api/assist/captures?limit=1")["items"][0]
        self.assertEqual((cap["status"], cap["vars"]["train"], cap["recipe"]), ("ok", "12935", "ntes_status"))
        self.assertGreaterEqual(cap["confidence"], 0.8)
        self.assertTrue(self.client.call("GET", "/api/assist/recipes")["ntes_status"]["verified"])


if __name__ == "__main__":
    unittest.main()
