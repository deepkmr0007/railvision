import json
import unittest
from datetime import date

from railvision.core.models import ObsKind
from railvision.parsers import irctc, ntes

SD = date(2026, 10, 15)


def cap(text="", tables=None, js=None, finished="2026-10-15T10:45:00+05:30"):
    return {"text": text, "tables": tables or [], "json": [{"url": "x", "status": 200, "body": json.dumps(js)}] if js else [],
            "finished_at": finished}


TABLE = [["Station", "Arr (Sch/Act)", "Dep (Sch/Act)", "Delay", "PF", "Km", "Status"],
         ["JAIPUR JN (JP)", "SRC", "08:00 / 08:04", "4 min", "1", "0 km", "Departed"],
         ["BANDIKUI JN (BKI)", "09:15 / 09:22", "09:17 / 09:24", "7 min", "2", "91 km", "Departed"],
         ["REWARI (RE)", "10:15 / 10:24", "10:15 / 10:24", "9", "-", "225 km", "Departed (No halt)"],
         ["GURUGRAM (GGN)", "11:00 / 11:10", "11:02 / --", "10 min", "3", "277 km", "Arrived"],
         ["DELHI JN (DLI)", "11:40 / 11:52", "DSTN", "", "", "305 km", "Yet to arrive"]]
HEADER = "12951 - TEST RAJDHANI\nLast updated on 15-Oct-2026 10:41\nArrived at GURUGRAM (GGN) at 11:10\n"


class NtesParserTests(unittest.TestCase):
    def test_table_layout(self):
        p = ntes.parse_status(cap(HEADER + "Cancelled Trains | Diverted Trains", [TABLE], finished="2026-10-15T11:15:00+05:30"), "12951")
        self.assertEqual([s.code for s in p.stations], ["JP", "BKI", "RE", "GGN", "DLI"])
        self.assertEqual(p.train_name, "TEST RAJDHANI")
        self.assertEqual(p.last_updated.strftime("%H:%M"), "10:41")
        self.assertFalse(p.cancelled, "menu text 'Cancelled Trains' must not cancel the train")
        self.assertFalse(p.diverted)
        self.assertGreaterEqual(p.confidence, 0.8)
        s = p.stations[1]
        self.assertEqual((s.sch_arr, s.act_arr, s.sch_dep, s.act_dep, s.delay_min, s.platform, s.km), ("09:15", "09:22", "09:17", "09:24", 7, "2", 91.0))
        self.assertFalse(p.stations[2].halt)
        r, obs, _ = ntes.to_route_and_obs(p, "12951", SD, cap(finished="2026-10-15T11:15:00+05:30"), "src")
        kinds = [(o.kind, o.station_code) for o in obs]
        self.assertIn((ObsKind.PASSED, "RE"), kinds)
        self.assertIn((ObsKind.ARRIVED, "GGN"), kinds)
        self.assertNotIn((ObsKind.DEPARTED, "GGN"), kinds)
        self.assertFalse(any(c == "DLI" for _, c in kinds), "a station 'yet to arrive' must never get an event")
        self.assertTrue(r.complete)

    def test_expected_times_are_not_actuals(self):
        txt = ("12951 TEST EXP\nLast Updated: 15-Oct-2026 10:41\nDeparted from ALWAR (AWR) at 10:00\n"
               "ALWAR (AWR)\nSch Arr: 09:50 Act Arr: 09:55\nSch Dep: 09:53 Act Dep: 10:00\nDeparted\n"
               "GURUGRAM (GGN)\nSch Arr: 11:00 Exp Arr: 11:12\nSch Dep: 11:02 Exp Dep: 11:14\nNot Arrived\n"
               "DELHI JN (DLI)\nSch Arr: 11:40 Exp Arr: 11:52\n")
        p = ntes.parse_status(cap(txt), "12951")
        self.assertEqual(len(p.stations), 3)
        self.assertTrue(p.stations[1].expected_only)
        _, obs, _ = ntes.to_route_and_obs(p, "12951", SD, cap(), "src")
        self.assertEqual({o.station_code for o in obs}, {"AWR"})

    def test_page_json_preferred(self):
        js = {"data": {"stations": [
            {"stnCode": "JP", "stnName": "JAIPUR", "schArrTime": "SRC", "schDepTime": "08:00", "actDepTime": "08:03", "hasDeparted": True, "distance": 0, "isStoppage": True},
            {"stnCode": "RE", "stnName": "REWARI", "schArrTime": "10:15", "schDepTime": "10:15", "actArrTime": "10:20", "actDepTime": "10:20", "hasDeparted": True, "distance": 225, "isStoppage": False},
            {"stnCode": "DLI", "stnName": "DELHI", "schArrTime": "11:40", "schDepTime": "DSTN", "actArrTime": "11:50", "hasArrived": False, "distance": 305, "isStoppage": True}]}}
        p = ntes.parse_status(cap("12951 X\nLast Updated: 15-Oct-2026 10:41", js=js), "12951")
        self.assertEqual(len(p.stations), 3)
        _, obs, _ = ntes.to_route_and_obs(p, "12951", SD, cap(), "src")
        self.assertEqual([(o.kind, o.station_code) for o in obs], [(ObsKind.DEPARTED, "JP"), (ObsKind.PASSED, "RE")])

    def test_low_confidence_creates_no_events(self):
        p = ntes.parse_status(cap("NEW DELHI (NDLS)\n10:00\nsomething"), "12951")
        self.assertLess(p.confidence, 0.6)
        _, obs, issues = ntes.to_route_and_obs(p, "12951", SD, cap(), "src")
        self.assertEqual(obs, [])
        self.assertTrue(any("confidence" in i for i in issues))

    def test_wrong_train_on_page(self):
        p = ntes.parse_status(cap(HEADER.replace("12951", "12952"), [TABLE]), "12951")
        self.assertLessEqual(p.confidence, 0.3)

    def test_cancel_sentence(self):
        p = ntes.parse_status(cap("12951 TEST\nThis train is cancelled for the date 15-Oct-2026", [TABLE]), "12951")
        self.assertTrue(p.cancelled)
        _, obs, _ = ntes.to_route_and_obs(p, "12951", SD, cap(), "src")
        self.assertEqual(obs[0].kind, ObsKind.CANCELLED)

    def test_midnight_rollover_and_day_of_run(self):
        t = [["Station", "Sch Arr", "Act Arr", "Sch Dep", "Act Dep", "Status"],
             ["A STN (AAA)", "", "", "23:30", "23:35", "Departed"],
             ["B STN (BBB)", "00:40", "00:52", "00:45", "00:55", "Departed"],
             ["C STN (CCC)", "02:10", "", "", "", "Not arrived"]]
        p = ntes.parse_status(cap("Last Updated: 16-Oct-2026 01:00", [t], finished="2026-10-16T01:02:00+05:30"), None)
        r, obs, _ = ntes.to_route_and_obs(p, "12345", SD, cap(finished="2026-10-16T01:02:00+05:30"), "src")
        self.assertEqual([s.day for s in r.stops], [1, 2, 2])
        dep = next(o for o in obs if o.station_code == "BBB" and o.kind == ObsKind.DEPARTED)
        self.assertEqual(dep.event_time.isoformat(), "2026-10-16T00:55:00+05:30")

    def test_km_position_and_future_actual_rejected(self):
        p = ntes.parse_status(cap(HEADER.replace("Arrived at GURUGRAM (GGN) at 11:10", "12.5 km after REWARI (RE)"), [TABLE],
                                  finished="2026-10-15T10:30:00+05:30"), "12951")
        self.assertEqual(p.position, {"kind": "POSITION", "code": "RE", "km_after": 12.5})
        _, obs, _ = ntes.to_route_and_obs(p, "12951", SD, cap(finished="2026-10-15T10:30:00+05:30"), "src")
        self.assertFalse(any(o.station_code == "GGN" for o in obs), "an 'actual' later than the capture time is not actual")
        self.assertTrue(any(o.kind == ObsKind.POSITION for o in obs))

    def test_delay_formats(self):
        for s, v in (("Delay: 00:26 Hrs", 26), ("Late by 1 hr 5 min", 65), ("Right Time", 0), ("2 min early", -2),
                     ("Before 3 min", -3)):
            self.assertEqual(ntes.delay_minutes(s), v, s)
        for s, v in (("6", 6), ("6 min", 6), ("01:05", 65), ("RT", 0), ("-2", -2)):
            self.assertEqual(ntes.delay_cell(s), v, s)

    def test_partial_route_marked_incomplete(self):
        t = [r for r in TABLE if "No halt" not in r[6]]
        p = ntes.parse_status(cap(HEADER, [t], finished="2026-10-15T11:15:00+05:30"), "12951")
        r, _, _ = ntes.to_route_and_obs(p, "12951", SD, cap(), "src")
        self.assertFalse(r.complete)


class IrctcParserTests(unittest.TestCase):
    def test_table(self):
        t = [["Coach", "Berth No", "Berth Type", "From", "To"], ["B1", "12", "UB", "AWR", "DLI"], ["B2", "7", "SU", "JP", "AWR"],
             ["B2", "33", "LB", "AWR", "GGN"]]
        v = irctc.parse_vacancy(cap("First Chart prepared · 3A 3 vacant", [t]), "AWR", "3A")
        self.assertEqual((v["total_vacant"], v["from_boarding"], v["chart_no"], v["classes"]), (3, 2, 1, {"3A": 3}))
        self.assertEqual(v["by_coach"], {"B1": 1, "B2": 2})

    def test_text_and_not_prepared(self):
        v = irctc.parse_vacancy(cap("S3 45 SL NDLS - JP\nS4 2 LB NDLS to AWR"), "NDLS", "SL")
        self.assertEqual(v["total_vacant"], 2)
        v = irctc.parse_vacancy(cap("Chart not prepared yet for this train"), "NDLS", "SL")
        self.assertIs(v["chart_prepared"], False)
        self.assertEqual(v["total_vacant"], 0)

    def test_search(self):
        r = irctc.parse_search(cap("12951 - MUMBAI RAJDHANI\n12952 - NEW DELHI RAJDHANI\n12002 BHOPAL SHATABDI"), "rajdhani")
        self.assertEqual([x["number"] for x in r["results"]], ["12951", "12952"])


if __name__ == "__main__":
    unittest.main()
