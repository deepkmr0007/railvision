"""The Android WebView driver, exercised in Chromium against the mock sites with the app's job loop."""
import os
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "mock_sites"))
sys.path.insert(0, str(Path(__file__).parent))

try:
    from playwright.sync_api import sync_playwright
    HAVE_PW = True
except ImportError:
    HAVE_PW = False

from mock_server import IST, make_server  # noqa: E402
from phone_harness import run_job  # noqa: E402

from railvision.assist.recipes import DEFAULT_RECIPES  # noqa: E402
from railvision.assist.teach import RECORDER_JS, build_recipe  # noqa: E402
from railvision.parsers import irctc, lists, ntes  # noqa: E402


@unittest.skipUnless(HAVE_PW and os.environ.get("RV_SKIP_E2E") != "1", "Playwright not installed")
class PhoneDriverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mock = make_server(0)
        cls.base = f"http://127.0.0.1:{cls.mock.server_address[1]}"
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(**({"channel": os.environ["RV_BROWSER_CHANNEL"]} if os.environ.get("RV_BROWSER_CHANNEL") else {}))
        cls.today = datetime.now(IST).date().isoformat()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.mock.shutdown()

    def recipe(self, name, path, **kw):
        return {**DEFAULT_RECIPES[name].to_dict(), "start_url": self.base + path, **kw}

    def page(self):
        return self.browser.new_page()

    def test_status_two_layouts(self):
        for path in ("/ntes-a/", "/ntes-b/"):
            with self.subTest(path=path):
                cap = run_job(self.page(), self.recipe("ntes_status", path), {"train": "12951", "date": self.today})
                self.assertEqual(cap["status"], "ok", cap)
                p = ntes.parse_status(cap, "12951")
                self.assertGreaterEqual(p.confidence, 0.8, (p.issues, cap["notes"]))
                self.assertEqual(len(p.stations), 8)

    def test_berth_with_class(self):
        cap = run_job(self.page(), self.recipe("irctc_berth", "/irctc/"), {"train": "12951", "date": self.today, "station": "AWR", "class": "3A"})
        self.assertEqual(cap["status"], "ok", cap)
        v = irctc.parse_vacancy(cap, "AWR", "3A")
        self.assertEqual(v["total_vacant"], len(self.mock.data.berths("12951", self.today, "AWR", "3A")))

    def test_exception_list_and_search(self):
        cap = run_job(self.page(), self.recipe("ntes_cancelled", "/ntes-a/"), {"list": "cancelled"})
        self.assertEqual(cap["status"], "ok", cap)
        self.assertEqual([i["train"] for i in lists.parse_exceptions(cap, "cancelled")["items"]], ["12956", "14311"])
        cap = run_job(self.page(), self.recipe("ntes_search", "/ntes-a/"), {"query": "MOCK EXPRESS"})
        self.assertTrue(irctc.parse_search(cap, "mock express")["results"])

    def test_captcha_blocked_without_person_and_handed_over_with(self):
        cap = run_job(self.page(), self.recipe("ntes_status", "/ntes-b/?mode=captcha"), {"train": "12951", "date": self.today})
        self.assertEqual(cap["status"], "captcha")
        asked = []
        cap = run_job(self.page(), self.recipe("pnr_status", "/pnr/?autosolve=1"), {"pnr": "4512345678"}, handover=True,
                      on_needs_you=asked.append)
        self.assertTrue(asked and "CAPTCHA" in asked[0])
        self.assertEqual(cap["status"], "ok", cap)
        pr = lists.parse_pnr(cap, "4512345678")
        self.assertEqual([p["current"]["status"] for p in pr["passengers"]], ["RAC", "CNF"])

    def test_fast_lane_running_status_picks_the_right_run(self):
        self.mock.fast["calls"].clear()
        rc = self.recipe("ntes_status", "/mntes/")
        cap = run_job(self.page(), rc, {"train": "12951", "date": self.today})
        self.assertEqual(cap["status"], "ok", cap)
        self.assertEqual(cap.get("fast"), "ntes-instances", cap["notes"])
        self.assertEqual(self.mock.fast["calls"], [("tr", "12951")])
        cap.update(vars={"train": "12951", "date": self.today}, finished_at=datetime.now(IST).isoformat())
        p = ntes.parse_status(cap, "12951")
        self.assertGreaterEqual(p.confidence, 0.7, p.issues)
        want = self.mock.data.run("12951", datetime.now(IST).date())
        self.assertEqual(p.position["code"], want["current"]["code"])
        route, obs, issues = ntes.to_route_and_obs(p, "12951", datetime.now(IST).date(), cap, "assist")
        self.assertTrue(route.open_ended and obs)
        self.assertEqual(obs[-1].station_code, want["current"]["code"])
        # a run that is not on the page gives nothing rather than another day's data
        cap["vars"]["date"] = "2020-01-01"
        p2 = ntes.parse_status(cap, "12951")
        self.assertFalse(p2.stations)
        self.assertLess(p2.confidence, 0.6)
        # tomorrow's run: listed, not started
        tm = (datetime.now(IST).date() + timedelta(days=1)).isoformat()
        cap["vars"]["date"] = tm
        self.assertTrue(ntes.parse_status(cap, "12951").not_started)

    def test_fast_lane_falls_back_to_the_form(self):
        self.mock.fast["no_token"] = True
        try:
            cap = run_job(self.page(), self.recipe("ntes_status", "/mntes/"), {"train": "12951", "date": self.today})
        finally:
            self.mock.fast.pop("no_token")
        self.assertEqual(cap["status"], "ok", cap)
        self.assertIsNone(cap.get("fast"))
        self.assertGreaterEqual(ntes.parse_status(cap, "12951").confidence, 0.8)

    def test_fast_lane_vacancy_chart(self):
        self.mock.fast["calls"].clear()
        v_ = {"train": "12951", "date": self.today, "station": "AWR", "class": "3A"}
        cap = run_job(self.page(), self.recipe("irctc_berth", "/online-charts/"), v_)
        self.assertEqual(cap["status"], "ok", cap)
        self.assertEqual([c[0] for c in self.mock.fast["calls"]], ["trainComposition"] + ["coachComposition"] * 3)
        v = irctc.parse_vacancy(cap, "AWR", "3A")
        want = self.mock.data.berths("12951", self.today, "AWR", "3A")
        self.assertEqual(v["method"], "irctc-json")
        self.assertEqual(v["total_vacant"], len(want))
        self.assertEqual(v["from_boarding"], sum(1 for w in want if w["from"] == "AWR"))
        self.assertTrue(v["chart_prepared"])
        self.assertEqual({(r["coach"], r["berth"], r["from"], r["to"]) for r in v["vacant"]},
                         {(w["coach"], str(w["berth"]), w["from"], w["to"]) for w in want})
        self.mock.fast["chart_not_prepared"] = True
        try:
            cap = run_job(self.page(), self.recipe("irctc_berth", "/online-charts/"), v_)
        finally:
            self.mock.fast.pop("chart_not_prepared")
        v = irctc.parse_vacancy(cap, "AWR", "3A")
        self.assertIs(v["chart_prepared"], False)
        self.assertIn("Chart not prepared", v["issues"][0])
        self.mock.fast["charts_refuse"] = True
        try:
            cap = run_job(self.page(), self.recipe("irctc_berth", "/online-charts/"), v_)
        finally:
            self.mock.fast.pop("charts_refuse")
        self.assertEqual(cap["status"], "blocked")

    def test_blocked_site(self):
        cap = run_job(self.page(), self.recipe("ntes_status", "/ntes-b/?mode=blocked"), {"train": "12951", "date": self.today})
        self.assertEqual(cap["status"], "blocked")

    def test_page_capture_links(self):
        self.mock.zone_state["items"] = 3
        cap = run_job(self.page(), DEFAULT_RECIPES["web_page"].to_dict(), {"url": self.base + "/zone/view_section.jsp?lang=0&id=0,4,268"})
        self.assertEqual(cap["status"], "ok")
        hrefs = [ln["h"] for ln in cap["links"]]
        self.assertTrue(any("view_detail.jsp" in h for h in hrefs) and any(h.endswith(".pdf") for h in hrefs))

    def test_recorded_recipe_taught_then_replayed_by_phone_driver(self):
        pg = self.page()
        raw = []
        pg.expose_binding("rvRecord", lambda src, st: raw.append(st))
        pg.expose_binding("rvDone", lambda src: None)
        pg.add_init_script(RECORDER_JS)
        pg.goto(self.base + "/ntes-a/")
        d = datetime.now(IST).date()
        pg.click("text=Spot Your Train")
        pg.type("#trn", "12951", delay=30)
        pg.wait_for_timeout(300)
        pg.click("li")
        pg.click(f"text={d.strftime('%d-%b-%Y')}")
        pg.click("text=Get Status")
        pg.wait_for_timeout(800)
        rc = build_recipe("ntes_status", "status", self.base + "/ntes-a/", raw, {"train": "12951", "date": d}).to_dict()
        cap = run_job(self.page(), rc, {"train": "12935", "date": self.today})
        self.assertEqual(cap["status"], "ok", cap)
        self.assertEqual(ntes.parse_status(cap, "12935").train_number, "12935")


if __name__ == "__main__":
    unittest.main()
