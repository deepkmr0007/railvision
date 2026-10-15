import unittest
from datetime import timedelta

from helpers import SD, T, ob, route

from railvision.core.crossing import MixedDataError, evaluate_crossing
from railvision.core.models import EventClass, ScheduledStop, Verification


class CrossingTests(unittest.TestCase):
    def ev(self, obs, station="RE", now=None):
        return evaluate_crossing(route(), obs, station, now or T(23, 0), SD)

    # --- three event classes -------------------------------------------------
    def test_scheduled_halt_direct(self):
        r = self.ev([ob("DEPARTED", "AAA", T(20, 5), delay=5), ob("ARRIVED", "BBB", T(21, 40), delay=10),
                     ob("DEPARTED", "BBB", T(21, 45), delay=10)], station="BBB")
        self.assertEqual(r.event_class, EventClass.SCHEDULED_HALT)
        self.assertEqual(r.verification, Verification.VERIFIED_LIVE)
        self.assertTrue(r.crossed)
        self.assertEqual(r.event_time, T(21, 45))

    def test_confirmed_passage_direct_report(self):
        r = self.ev([ob("PASSED", "RE", T(22, 31), delay=6)])
        self.assertEqual(r.event_class, EventClass.CONFIRMED_PASSAGE)
        self.assertEqual(r.verification, Verification.VERIFIED_LIVE)
        self.assertEqual(r.event_time, T(22, 31))
        self.assertTrue(r.crossed)

    def test_confirmed_passage_derived_from_downstream(self):
        r = self.ev([ob("DEPARTED", "BBB", T(21, 40)), ob("ARRIVED", "CCC", T(23, 30))], now=T(23, 40))
        self.assertEqual(r.event_class, EventClass.CONFIRMED_PASSAGE)
        self.assertEqual(r.verification, Verification.DERIVED)
        self.assertTrue(r.crossed)
        self.assertIsNone(r.event_time, "exact time must not be invented")
        self.assertEqual((r.est_earliest, r.est_latest), (T(21, 40), T(23, 30)))
        self.assertTrue(T(21, 40) < r.est_central < T(23, 30))

    def test_estimated_from_upstream(self):
        r = self.ev([ob("DEPARTED", "BBB", T(21, 45), delay=10)], now=T(21, 50))
        self.assertEqual(r.event_class, EventClass.ESTIMATED_CROSSING)
        self.assertEqual(r.verification, Verification.ESTIMATED)
        self.assertIsNone(r.crossed, "an estimate never asserts crossing")
        self.assertEqual(r.est_central, T(22, 35))           # 21:45 + scheduled 50 min run
        self.assertLess(r.est_earliest, r.est_central)
        self.assertGreater(r.est_latest - r.est_central, r.est_central - r.est_earliest)  # late-skewed
        self.assertIn("ESTIMATED", r.headline)

    # --- the golden rule ----------------------------------------------------
    def test_never_infer_crossing_from_scheduled_time_passing(self):
        r = self.ev([], now=T(23, 59))  # long after scheduled 22:25, no data
        self.assertEqual(r.event_class, EventClass.NOT_VERIFIED)
        self.assertIsNone(r.crossed)
        self.assertIn("Crossing not verified", r.headline)

    def test_past_estimate_still_not_verified(self):
        # fresh position 5 km before RE at 22:20; by 22:45 the whole estimate window is in the past
        r = self.ev([ob("DEPARTED", "BBB", T(21, 35)), ob("POSITION", "BBB", T(22, 20), km_after=55.0)], now=T(22, 45))
        self.assertIsNone(r.crossed)
        self.assertEqual(r.event_class, EventClass.ESTIMATED_CROSSING)
        self.assertIn("NOT yet verified", r.headline)

    def test_stale_data_not_used(self):
        r = self.ev([ob("DEPARTED", "BBB", T(21, 45))], now=T(21, 47) + timedelta(hours=3))
        self.assertEqual(r.event_class, EventClass.NOT_VERIFIED)
        self.assertEqual(r.verification, Verification.STALE)

    def test_moderately_stale_widens_estimate(self):
        fresh = self.ev([ob("DEPARTED", "BBB", T(21, 45))], now=T(21, 50))
        stale = self.ev([ob("DEPARTED", "BBB", T(21, 45))], now=T(22, 40))
        self.assertGreater(stale.est_latest - stale.est_earliest, fresh.est_latest - fresh.est_earliest)
        self.assertTrue(any("old" in w for w in stale.warnings))

    def test_not_started(self):
        r = self.ev([], now=T(19, 0))
        self.assertIn("has not started", r.headline)

    # --- disruptions -------------------------------------------------------
    def test_cancelled(self):
        r = self.ev([ob("CANCELLED", None, None)])
        self.assertEqual(r.event_class, EventClass.CANCELLED)
        self.assertFalse(r.crossed)

    def test_diversion_drops_station(self):
        r = self.ev([ob("DEPARTED", "BBB", T(21, 40)),
                     ob("DIVERTED", "BBB", T(21, 41), payload={"route": ["AAA", "PPA", "BBB", "XDV", "CCC", "DDD"]})])
        self.assertEqual(r.event_class, EventClass.NOT_ON_ROUTE)
        self.assertFalse(r.crossed)

    def test_diversion_estimate_on_untimed_section(self):
        # station still on route after diversion, but anchor is on diversion-only station with no timetable
        r = self.ev([ob("DIVERTED", "BBB", T(21, 41), payload={"route": ["AAA", "BBB", "XDV", "RE", "CCC", "DDD"]}),
                     ob("PASSED", "XDV", T(22, 0))], now=T(22, 5))
        self.assertIn(r.event_class, (EventClass.NOT_VERIFIED, EventClass.ESTIMATED_CROSSING))
        self.assertIsNone(r.crossed)

    def test_short_termination(self):
        r = self.ev([ob("SHORT_TERMINATED", "BBB", T(21, 40))])
        self.assertEqual(r.event_class, EventClass.NOT_ON_ROUTE)

    def test_station_not_on_route(self):
        r = self.ev([], station="ZZZ")
        self.assertEqual(r.event_class, EventClass.NOT_ON_ROUTE)

    # --- data quality -------------------------------------------------------
    def test_duplicates_removed(self):
        o = ob("PASSED", "RE", T(22, 31))
        r = self.ev([o, ob("PASSED", "RE", T(22, 31))])
        self.assertEqual(len(r.evidence), 1)

    def test_source_correction_latest_wins(self):
        a = ob("PASSED", "RE", T(22, 31), src_lag=2)
        b = ob("PASSED", "RE", T(22, 34), src_lag=8)
        r = self.ev([b, a])
        self.assertEqual(r.event_time, T(22, 34))
        self.assertTrue(any("corrected" in w for w in r.warnings))

    def test_out_of_order_observations_reduce_confidence(self):
        r = self.ev([ob("DEPARTED", "BBB", T(23, 10)), ob("ARRIVED", "CCC", T(22, 50))], now=T(23, 30))
        self.assertTrue(r.crossed)
        self.assertLess(r.confidence, 0.9)
        self.assertTrue(any("Out-of-order" in w for w in r.warnings))

    def test_off_route_observation_ignored_with_warning(self):
        r = self.ev([ob("DEPARTED", "BBB", T(21, 45)), ob("PASSED", "QQQ", T(22, 0))], now=T(22, 5))
        self.assertTrue(any("off the known route" in w for w in r.warnings))

    def test_mixed_demo_and_live_rejected(self):
        with self.assertRaises(MixedDataError):
            evaluate_crossing(route(), [ob("PASSED", "RE", T(22, 31), is_demo=True)], "RE", T(23, 0), SD)

    # --- time handling -------------------------------------------------------
    def test_midnight_estimate(self):
        r = self.ev([ob("DEPARTED", "CCC", T(23, 50), delay=25)], station="DDD", now=T(23, 52))
        self.assertEqual(r.est_central, T(1, 5, day=2))       # 23:50 + 75 min -> next calendar day
        self.assertEqual(r.est_central.date().isoformat(), "2026-10-15")

    def test_midnight_direct_arrival_day2(self):
        r = self.ev([ob("ARRIVED", "DDD", T(0, 55, day=2))], station="DDD", now=T(1, 0, day=2))
        self.assertTrue(r.crossed)
        self.assertIn("arrived at", r.headline)

    def test_arrived_anchor_includes_dwell(self):
        r = self.ev([ob("ARRIVED", "BBB", T(21, 30))], now=T(21, 31))
        self.assertEqual(r.est_central, T(22, 25))   # 21:30 + 5 dwell + 50 run

    def test_position_anchor_between_stations(self):
        r = self.ev([ob("DEPARTED", "BBB", T(21, 35)), ob("POSITION", "BBB", T(22, 0), km_after=30.0)], now=T(22, 2))
        self.assertEqual(r.event_class, EventClass.ESTIMATED_CROSSING)
        self.assertEqual(r.est_central, T(22, 25))   # halfway point at 22:00, remaining 25 min

    def test_reversed_direction_route(self):
        stops = [ScheduledStop(0, "DDD", "Delta", 0, 1, None, "06:00", True),
                 ScheduledStop(1, "CCC", "Charlie", 80, 1, "07:10", "07:15", True),
                 ScheduledStop(2, "RE", "Rewari", 140, 1, None, "08:05", False),
                 ScheduledStop(3, "AAA", "Alpha", 300, 1, "10:30", None, True)]
        r = evaluate_crossing(route(stops), [ob("DEPARTED", "CCC", T(7, 20))], "RE", T(7, 25), SD)
        self.assertEqual(r.est_central, T(8, 10))
        r2 = evaluate_crossing(route(stops), [ob("ARRIVED", "AAA", T(10, 40))], "RE", T(10, 45), SD)
        self.assertTrue(r2.crossed)

    def test_rescheduled_departure(self):
        r = self.ev([ob("RESCHEDULED", None, T(18, 0), payload={"new_departure": "2026-10-14T22:00:00+05:30"})],
                    now=T(20, 30))
        self.assertIn("has not started", r.headline)
        self.assertTrue(any("RESCHEDULED" in w for w in r.warnings))

    def test_result_serializes(self):
        d = self.ev([ob("PASSED", "RE", T(22, 31))]).to_dict()
        self.assertEqual(d["event_class"], "CONFIRMED_PASSAGE")
        self.assertTrue(d["event_time"].endswith("+05:30"))


if __name__ == "__main__":
    unittest.main()
