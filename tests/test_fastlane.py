"""Fast-lane captures through the service: IRCTC chart JSON, NTES run list, and the diagnostic bundle."""
import json
import unittest
from datetime import timedelta

from test_api import call
from test_assist_service import NOW, live_app

from railvision.core.timeutil import iso, now_ist
from railvision.parsers import irctc

HALTS = ["RNC", "MURI", "BKSC", "GAYA", "LTT"]


def comp(error=None):
    return {"cdd": None if error else [{"coachName": "S3", "classCode": "SL", "vacantBerths": 0}, {"coachName": "B1", "classCode": "3A", "vacantBerths": 0}],
            "trainNo": "18609", "trainName": "RNC LTT EXP", "from": "RNC", "to": "LTT", "trainStartDate": "2026-10-14", "remote": "RNC",
            "chartOneDate": "2026-10-14 10:50:28", "chartTwoDate": None, "error": error,
            "chartStatusResponseDto": None if error else {"chartOneFlag": 3, "chartTwoFlag": 0}}


def coach(name, berths):
    """berths: {berthNo: [(from, to, occupied), ...]}"""
    return {"coachName": name, "error": None, "bdd": [
        {"berthCode": "L", "berthNo": no, "cabinCoupeNameNo": "1", "from": "RNC", "to": "LTT", "enable": True,
         "bsd": [{"splitNo": i + 1, "from": f, "to": t, "quota": "GN", "occupancy": occ} for i, (f, t, occ) in enumerate(sp)]}
        for no, sp in berths.items()]}


def cap(*bodies):
    return {"json": [{"url": f"https://www.irctc.co.in/online-charts/api/{n}", "req": rq, "status": 200, "body": json.dumps(b)} for n, rq, b in bodies]}


class ChartJsonTests(unittest.TestCase):
    def test_vacant_only_when_occupancy_is_false_and_stretches_merge(self):
        c = cap(("trainComposition", {}, comp()),
                ("coachComposition", {"coach": "S3", "cls": "SL"}, coach("S3", {
                    1: [("RNC", "MURI", False), ("MURI", "BKSC", False), ("BKSC", "LTT", True)],     # one stretch RNC→BKSC
                    2: [("RNC", "GAYA", True), ("GAYA", "LTT", False)],
                    3: [("RNC", "LTT", True)],
                    4: [("RNC", "MURI", None), ("MURI", "LTT", "false")]})))                           # unknown values are never vacant
        v = irctc.parse_vacancy(c, "MURI", "SL")
        self.assertEqual([(r["berth"], r["from"], r["to"]) for r in v["vacant"]], [("1", "RNC", "BKSC"), ("2", "GAYA", "LTT")])
        self.assertEqual(v["from_boarding"], 1)                 # berth 1 covers MURI; berth 2 frees up only from GAYA
        self.assertEqual((v["chart_prepared"], v["chart_no"], v["method"]), (True, 1, "irctc-json"))
        self.assertEqual(irctc.parse_vacancy(c, "GAYA", "SL")["from_boarding"], 1)
        self.assertEqual(irctc.parse_vacancy(c, "RNC", "3A")["total_vacant"], 0)

    def test_chart_not_prepared_and_missing_class(self):
        v = irctc.parse_vacancy(cap(("trainComposition", {}, comp("Chart not prepared"))), "RNC", "SL")
        self.assertIs(v["chart_prepared"], False)
        self.assertEqual(v["total_vacant"], 0)
        v = irctc.parse_vacancy(cap(("trainComposition", {}, comp())), "RNC", "2A")
        self.assertIn("no 2A coach", " ".join(v["issues"]))


class ServiceTests(unittest.TestCase):
    def test_ingest_chart_json_and_diagnostic(self):
        app, tok, _ = live_app()
        today = now_ist().date().isoformat()
        body = {"kind": "berth", "recipe": "irctc_berth", "status": "ok", "vars": {"train": "18609", "date": today, "station": "RNC", "class": "SL"},
                "text": "Reservation Chart", "tables": [], "url": "https://www.irctc.co.in/online-charts/", "title": "t", "fast": "irctc-charts",
                "steps": ["fast lane: chart data for 1 coach(es)"], "started_at": iso(now_ist()), "finished_at": iso(NOW - timedelta(minutes=1)),
                **cap(("trainComposition", {"trainNo": "18609"}, comp()),
                      ("coachComposition", {"coach": "S3", "cls": "SL"}, coach("S3", {7: [("RNC", "LTT", False)]})))}
        st, _, res = call(app, "POST", "/api/assist/captures", body, tok)
        self.assertEqual(st, 201, res)
        self.assertIn("1 vacant", res["headline"])
        st, _, d = call(app, "GET", "/api/assist/diagnostic", token=tok)
        self.assertEqual(st, 200)
        c0 = d["captures"][0]
        self.assertEqual(c0["raw"]["fast"], "irctc-charts")
        self.assertEqual(len(c0["raw"]["json"]), 2)
        self.assertEqual(c0["parse"]["vacancy"]["total_vacant"], 1)
        # the same check as an on-demand job: the detail carries the full list and readable context
        st, _, jb = call(app, "POST", "/api/assist/jobs", {"kind": "berth", "vars": {"train": "18609", "date": today, "station": "rnc", "date_is": "boarding"}}, tok)
        self.assertEqual((st, json.loads(jb["vars"])["station"]), (201, "RNC"))
        st, _, j2 = call(app, "POST", "/api/assist/jobs", {"kind": "berth", "vars": {"train": "12951", "date": today, "station": "alwar", "class": "all"}}, tok)
        self.assertEqual(json.loads(j2["vars"]), {"train": "12951", "date": today, "station": "AWR"})
        call(app, "POST", "/api/assist/captures", {**body, "job_id": jb["id"], "vars": json.loads(jb["vars"])}, tok)
        st, _, det = call(app, "GET", f"/api/assist/jobs/{jb['id']}", token=tok)
        self.assertEqual(st, 200, det)
        self.assertEqual(det["status"], "done")
        row = det["capture"]["vacancy"]["vacant"][0]
        self.assertEqual((row["coach"], row["berth"], row["from"], row["to"]), ("S3", "7", "RNC", "LTT"))
        self.assertTrue(det["capture"]["steps"])
        for bad in ({"train": "12"}, {"train": "18609", "date": "x"}, {"train": "18609", "date": today}, {"train": "18609", "date": today, "station": "Nowhere Town"}):
            self.assertEqual(call(app, "POST", "/api/assist/jobs", {"kind": "berth", "vars": bad}, tok)[0], 422, bad)
        # PNR checks never go into the bundle
        call(app, "POST", "/api/assist/captures", {"kind": "pnr", "recipe": "pnr_status", "status": "ok", "vars": {"pnr": "4512345678"},
                                                   "text": "PNR 4512345678 Booking Status CNF", "tables": [], "finished_at": iso(NOW)}, tok)
        _, _, d = call(app, "GET", "/api/assist/diagnostic", token=tok)
        self.assertTrue(all(c["kind"] != "pnr" for c in d["captures"]))
        self.assertNotIn("4512345678", json.dumps(d))


if __name__ == "__main__":
    unittest.main()
