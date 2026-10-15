import base64
import os
import unittest
from datetime import timedelta

from test_api import call
from test_parsers import HEADER, TABLE

from railvision.api.app import App
from railvision.core.timeutil import iso, now_ist
from railvision.providers.registry import ProviderRegistry
from railvision.services.store import Store


NOW = now_ist().replace(hour=11, minute=20, second=0, microsecond=0)      # fixed scenario clock (today 11:20 IST)


def live_app():
    os.environ["RAILVISION_MODE"] = "live"
    try:
        app = App(store=Store(), registry=ProviderRegistry("live", []), logger=lambda *_: None, assist=True, clock=lambda: NOW)
    finally:
        os.environ["RAILVISION_MODE"] = "demo"
    uid, tok = app.store.create_user("A")
    uid2, tok2 = app.store.create_user("B")
    return app, tok, tok2


def status_capture(watch_id=None, job_id=None, train="12951", text=None, tables=None, status="ok", finished=None):
    d = now_ist().date().isoformat()
    return {"kind": "status", "recipe": "ntes_status", "watch_id": watch_id, "job_id": job_id, "status": status,
            "vars": {"train": train, "date": d}, "text": text if text is not None else HEADER.replace("15-Oct-2026", now_ist().strftime("%d-%b-%Y")),
            "tables": tables if tables is not None else [TABLE], "json": [], "url": "https://example/x", "title": "t",
            "screenshot_b64": base64.b64encode(b"\xff\xd8fakejpeg").decode(), "started_at": iso(now_ist()),
            "finished_at": finished or iso(NOW - timedelta(minutes=5))}


class AssistServiceTests(unittest.TestCase):
    def setUp(self):
        self.app, self.tok, self.tok2 = live_app()
        self.today = now_ist().date().isoformat()

    def add(self, train="12951", tok=None, **kw):
        return call(self.app, "POST", "/api/assist/watch", {"train": train, "start_date": self.today, "interval_min": 10, **kw}, tok or self.tok)

    def test_requires_live_mode(self):
        with self.assertRaises(ValueError):
            App(store=Store(), registry=ProviderRegistry("demo", []), logger=lambda *_: None, assist=True)

    def test_watch_validation_and_limit(self):
        self.assertEqual(self.add("1295")[0], 422)
        self.assertEqual(self.add(interval_min=2)[0], 422)
        self.assertEqual(call(self.app, "POST", "/api/assist/watch", {"train": "12951", "start_date": "2020-01-01"}, self.tok)[0], 422)
        self.assertEqual(self.add(berth_checks=[{"station": "NDLS", "class": "ZZ"}])[0], 422)
        for i in range(10):
            st, _, w = self.add(str(12900 + i), berth_checks=[{"station": "NDLS", "class": "3A"}] if i == 0 else [])
            self.assertEqual(st, 201, w)
        st, _, b = self.add("12999")
        self.assertEqual(st, 422)
        self.assertIn("full", b["error"])
        self.assertEqual(self.add("12900")[0], 422)                   # duplicate
        self.assertEqual(len(call(self.app, "GET", "/api/assist/watch", token=self.tok2)[2]["watch"]), 0)  # isolation

    def test_work_is_leased_and_sequential(self):
        self.add("12951", berth_checks=[{"station": "AWR", "class": "3A"}])
        self.add("12952")
        st, _, w = call(self.app, "GET", "/api/assist/work", token=self.tok)
        self.assertEqual([i["train"] for i in w["watch"]], ["12951", "12952"])
        self.assertEqual([j["kind"] for j in w["watch"][0]["jobs"]], ["status", "berth"])
        self.assertGreaterEqual(w["pacing"]["min_gap_s"], 15)
        self.assertIn("ntes_status", w["recipes"])
        self.assertEqual(call(self.app, "GET", "/api/assist/work", token=self.tok)[2]["watch"], [], "leased items are not handed out twice")
        self.assertTrue(call(self.app, "GET", "/api/assist/status", token=self.tok)[2]["agent"]["online"])

    def test_ingest_feeds_status_crossing_and_alerts(self):
        _, _, w = self.add("12951", ref_station="RE", end_station="DLI", notify="every", destinations=["app", "whatsapp"])
        call(self.app, "GET", "/api/assist/work", token=self.tok)
        st, _, r = call(self.app, "POST", "/api/assist/captures", status_capture(w["id"]), self.tok)
        self.assertEqual(st, 201, r)
        self.assertEqual(r["status"], "ok")
        self.assertGreaterEqual(r["confidence"], 0.6)
        self.assertTrue(r.get("task"))
        # the official-page data now flows through the normal endpoints
        st, _, s = call(self.app, "GET", "/api/trains/12951/status", query={"date": self.today, "station": "RE"})
        self.assertEqual(st, 200, s)
        self.assertFalse(s["status"]["is_demo"])
        self.assertIn("browser assistant", s["status"]["freshness"]["source"])
        self.assertEqual(s["crossing"]["event_class"], "CONFIRMED_PASSAGE")
        notes = call(self.app, "GET", "/api/notifications", token=self.tok)[2]["items"]
        types = {n["type"] for n in notes}
        self.assertIn("STATUS_REPORT", types)
        self.assertIn("CONFIRMED_PASSAGE", types)
        w2 = call(self.app, "GET", "/api/assist/watch", token=self.tok)[2]["watch"][0]
        self.assertEqual(w2["last_status"], "ok")
        self.assertGreater(w2["next_due_at"], iso(NOW + timedelta(minutes=9)))
        caps = call(self.app, "GET", "/api/assist/captures", token=self.tok)[2]["items"]
        self.assertTrue(caps[0]["has_shot"])
        st, hd, body = call(self.app, "GET", f"/api/assist/captures/{caps[0]['id']}/screenshot", token=self.tok)
        self.assertEqual((st, hd["Content-Type"]), (200, "image/jpeg"))
        self.assertEqual(call(self.app, "GET", f"/api/assist/captures/{caps[0]['id']}/screenshot", token=self.tok2)[0], 404)

    def test_on_change_only_reports_changes(self):
        _, _, w = self.add("12951")
        for _ in range(3):
            call(self.app, "POST", "/api/assist/captures", status_capture(w["id"]), self.tok)
        reps = [n for n in call(self.app, "GET", "/api/notifications", token=self.tok)[2]["items"] if n["type"] == "STATUS_REPORT"]
        self.assertEqual(len(reps), 1)

    def test_blocked_capture_backs_off_and_warns(self):
        _, _, w = self.add("12951")
        c = status_capture(w["id"], status="captcha", text="", tables=[])
        c["reason"] = "The site is showing a CAPTCHA"
        call(self.app, "POST", "/api/assist/captures", c, self.tok)
        w2 = call(self.app, "GET", "/api/assist/watch", token=self.tok)[2]["watch"][0]
        self.assertGreater(w2["next_due_at"], iso(NOW + timedelta(minutes=29)))
        self.assertIn("ASSIST_PROBLEM", {n["type"] for n in call(self.app, "GET", "/api/notifications", token=self.tok)[2]["items"]})

    def test_cannot_post_into_someone_elses_watch(self):
        _, _, w = self.add("12951")
        self.assertEqual(call(self.app, "POST", "/api/assist/captures", status_capture(w["id"]), self.tok2)[0], 404)

    def test_chat_queues_official_page_check_and_name_search(self):
        r = call(self.app, "POST", "/api/chat", {"text": "where is 12951"}, self.tok)[2]
        self.assertIn("browser assistant", r["reply"])
        job = r["data"]["job"]
        r = call(self.app, "POST", "/api/chat", {"text": "search train rajdhani"}, self.tok)[2]
        self.assertEqual(r["intent"], "search")
        jobs = call(self.app, "GET", "/api/assist/work", token=self.tok)[2]["jobs"]
        self.assertEqual({j["kind"] for j in jobs}, {"status", "search"})
        call(self.app, "POST", "/api/assist/captures", status_capture(job_id=job["id"]), self.tok)
        res = [n for n in call(self.app, "GET", "/api/notifications", token=self.tok)[2]["items"] if n["type"] == "ASSIST_RESULT"]
        self.assertTrue(res and "RUNNING STATUS" in res[0]["body"])
        # now the bot answers from the captured official page
        r = call(self.app, "POST", "/api/chat", {"text": "has 12951 crossed Rewari?"}, self.tok)[2]
        self.assertIn("CONFIRMED", r["reply"])

    def test_recipe_upload_and_https_rule(self):
        rc = {"kind": "status", "start_url": "http://evil.example", "mode": "recorded", "steps": []}
        self.assertEqual(call(self.app, "PUT", "/api/assist/recipes/ntes_status", {**rc, "name": "x"}, self.tok)[0], 422)
        rc["start_url"] = "https://enquiry.indianrail.gov.in/mntes/"
        rc["steps"] = [{"action": "fill", "selector": "#t", "value": "{train}"}]
        st, _, b = call(self.app, "PUT", "/api/assist/recipes/ntes_status", rc, self.tok)
        self.assertEqual(st, 200)
        self.assertEqual(call(self.app, "GET", "/api/assist/recipes", token=self.tok)[2]["ntes_status"]["mode"], "recorded")


class PacerTests(unittest.TestCase):
    def test_gap_daily_cap_and_backoff(self):
        from railvision.assist.agent import Pacer
        t = {"now": 1000.0}
        slept = []
        p = Pacer(min_gap_s=20, jitter_s=0, max_pages_per_day=3, sleep=lambda s: (slept.append(s), t.__setitem__("now", t["now"] + s)),
                  clock=lambda: t["now"])
        p.wait_turn()
        p.wait_turn()
        self.assertAlmostEqual(slept[-1], 20.0)                        # second page waits the full gap
        p.wait_turn()
        with self.assertRaises(RuntimeError):
            p.wait_turn()                                              # 4th page today refused
        url = "https://enquiry.indianrail.gov.in/mntes/"
        self.assertEqual(p.strike(url), 1800)
        self.assertGreater(p.host_paused(url), 1700)
        self.assertEqual(p.host_paused("https://www.irctc.co.in/x"), 0)
        self.assertEqual(p.strike(url), 3600)                          # doubles
        for _ in range(5):
            w = p.strike(url)
        self.assertEqual(w, 4 * 3600)                                  # capped at 4 h
        p.clear(url)
        self.assertEqual(p.strike(url), 1800)


if __name__ == "__main__":
    unittest.main()
