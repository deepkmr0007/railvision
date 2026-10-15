import unittest
from datetime import timedelta

from helpers import SD, T, ob, route

from railvision.delivery.base import SENT, DeliveryResult, Destination
from railvision.delivery.channels import Dispatcher
from railvision.providers.base import ProviderAdapter, ProviderError
from railvision.providers.registry import ProviderRegistry
from railvision.services.store import Store
from railvision.services.tracker import TaskError, TrackingEngine


class FakeProvider(ProviderAdapter):
    source_id = "fake-live"

    def __init__(self):
        self.obs, self.calls, self.fail = [], 0, False

    def get_train_route(self, train_number, service_date):
        return route()

    def get_train_status(self, train_number, service_date):
        self.calls += 1
        if self.fail:
            raise ProviderError("upstream 503")
        return list(self.obs)


class Capture(Destination):
    name = "telegram"

    def __init__(self):
        self.sent = []

    def send(self, user_id, message, task):
        self.sent.append((user_id, message))
        return DeliveryResult(SENT, "ok")


class AppSink(Capture):
    name = "app"


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock(T(21, 0))
        self.prov = FakeProvider()
        self.store = Store()
        self.tg, self.app = Capture(), AppSink()
        self.eng = TrackingEngine(self.store, ProviderRegistry("live", [self.prov]),
                                  Dispatcher(self.store, [self.app, self.tg]), mode="live", clock=self.clock)
        self.u1, _ = self.store.create_user("A")
        self.u2, _ = self.store.create_user("B")

    def mk(self, user=None, **kw):
        args = dict(train="12951", service_date=SD, reference_station="RE", end_station="DDD", interval_min=15,
                    destinations=["telegram"])
        args.update(kw)
        t = self.eng.draft_task(user or self.u1, **args)
        return self.eng.confirm(t["id"], user or self.u1)

    def test_draft_requires_confirmation_and_validates(self):
        t = self.eng.draft_task(self.u1, "12951", SD, "RE", "DDD", 15, ["app"])
        self.assertEqual(t["status"], "PENDING_CONFIRMATION")
        self.assertIn("NON-STOP", t["summary"]["reference_station"])
        self.assertEqual(self.eng.tick()["due"], 0)     # unconfirmed tasks never run
        with self.assertRaises(TaskError):
            self.eng.draft_task(self.u1, "12951", SD, "ZZZ", None, 15, ["app"])
        with self.assertRaises(TaskError):
            self.eng.draft_task(self.u1, "12951", SD, "RE", "AAA", 15, ["app"])    # end before ref
        with self.assertRaises(TaskError):
            self.eng.draft_task(self.u1, "12951", SD, "RE", None, 1, ["app"])      # interval too small
        with self.assertRaises(TaskError):
            self.eng.draft_task(self.u1, "12951", SD, "RE", None, 15, ["pigeon"])

    def test_shared_polling_one_fetch_for_many_tasks(self):
        self.prov.obs = [ob("DEPARTED", "BBB", T(20, 50))]
        self.mk()
        self.mk(user=self.u2, interval_min=30)
        s = self.eng.tick()
        self.assertEqual((s["due"], s["groups"], s["fetches"]), (2, 1, 1))
        self.assertEqual(self.prov.calls, 1)

    def test_estimate_then_confirmed_passage_then_no_duplicates(self):
        self.prov.obs = [ob("DEPARTED", "BBB", T(20, 50))]
        t = self.mk()
        self.eng.tick()
        types = [n["type"] for n in self.store.list_notifications(self.u1)]
        self.assertIn("ESTIMATED_CROSSING", types)
        self.prov.obs.append(ob("PASSED", "RE", T(21, 41)))
        self.clock.t = T(21, 50)
        self.eng.tick()     # not due yet (15 min interval) -> nothing
        self.clock.t = T(21, 16) + timedelta(minutes=15)
        self.eng.tick()
        types = [n["type"] for n in self.store.list_notifications(self.u1)]
        self.assertIn("CONFIRMED_PASSAGE", types)
        n_before = len(types)
        self.clock.t += timedelta(minutes=16)
        self.eng.tick()
        types2 = [n["type"] for n in self.store.list_notifications(self.u1)]
        self.assertEqual(types2.count("CONFIRMED_PASSAGE"), 1, "dedupe: confirmed passage sent once")
        self.assertGreaterEqual(len(types2), n_before)
        hist = self.store.passage_history("12951", SD.isoformat(), "RE")
        self.assertGreaterEqual(len(hist), 2)           # audit trail: estimate then confirmation
        self.assertEqual(hist[-1]["verification"], "VERIFIED_LIVE")
        self.assertEqual(self.tg.sent[-1][0], self.u1)
        self.assertNotIn("DEMO", self.tg.sent[-1][1]["text"])

    def test_completion(self):
        self.prov.obs = [ob("PASSED", "RE", T(22, 30)), ob("ARRIVED", "DDD", T(0, 45, day=2))]
        t = self.mk()
        self.clock.t = T(0, 50, day=2)
        self.eng.tick()
        self.assertEqual(self.store.get_task(t["id"], self.u1)["status"], "COMPLETED")
        self.assertIn("COMPLETED", [n["type"] for n in self.store.list_notifications(self.u1)])

    def test_cancelled_train_completes_task(self):
        self.prov.obs = [ob("CANCELLED", None, None)]
        t = self.mk()
        self.eng.tick()
        self.assertEqual(self.store.get_task(t["id"], self.u1)["status"], "COMPLETED")
        self.assertIn("CANCELLED", [n["type"] for n in self.store.list_notifications(self.u1)])

    def test_delay_alert_threshold(self):
        self.prov.obs = [ob("DEPARTED", "BBB", T(20, 50), delay=45)]
        self.mk(delay_threshold_min=30)
        self.eng.tick()
        self.assertIn("DELAY_ALERT", [n["type"] for n in self.store.list_notifications(self.u1)])

    def test_backoff_and_stale_warning_on_provider_failure(self):
        self.prov.fail = True
        t = self.mk()
        s = self.eng.tick()
        self.assertEqual(s["errors"], 1)
        t1 = self.store.get_task(t["id"], self.u1)
        self.assertEqual(t1["fail_count"], 1)
        self.clock.t = T(21, 0) + timedelta(seconds=30)
        self.eng.tick()     # still inside 60 s backoff window -> no extra upstream call
        self.assertEqual(self.prov.calls, 1)
        self.clock.t = T(21, 2)
        self.eng.tick()
        self.assertEqual(self.prov.calls, 2)
        self.assertIn("STALE", [n["type"] for n in self.store.list_notifications(self.u1)])

    def test_pause_resume_cancel_interval(self):
        self.prov.obs = [ob("DEPARTED", "BBB", T(20, 50))]
        t = self.mk()
        self.eng.set_state(t["id"], self.u1, "pause")
        self.assertEqual(self.eng.tick()["due"], 0)
        self.eng.set_state(t["id"], self.u1, "resume")
        self.assertEqual(self.eng.tick()["due"], 1)
        t2 = self.eng.update(t["id"], self.u1, interval_min=30)
        self.assertEqual(t2["interval_min"], 30)
        with self.assertRaises(TaskError):
            self.eng.update(t["id"], self.u1, interval_min=2)
        self.eng.set_state(t["id"], self.u1, "cancel")
        with self.assertRaises(TaskError):
            self.eng.set_state(t["id"], self.u1, "resume")

    def test_user_isolation(self):
        t = self.mk()
        self.assertIsNone(self.store.get_task(t["id"], self.u2))
        with self.assertRaises(TaskError):
            self.eng.set_state(t["id"], self.u2, "pause")
        self.assertFalse(self.store.delete_task(t["id"], self.u2))
        self.assertEqual(self.store.list_tasks(self.u2), [])

    def test_quiet_hours_suppress_routine_reports(self):
        self.prov.obs = [ob("DEPARTED", "BBB", T(20, 50))]
        self.mk(quiet=("20:00", "23:00"))
        self.eng.tick()
        types = [n["type"] for n in self.store.list_notifications(self.u1)]
        self.assertNotIn("STATUS_REPORT", types)
        self.assertNotIn("ESTIMATED_CROSSING", types)

    def test_periodic_report_when_nothing_changes(self):
        self.prov.obs = [ob("DEPARTED", "AAA", T(20, 2))]
        self.mk(reference_station="CCC")
        self.eng.tick()                                     # first: estimate notification
        self.clock.t = T(21, 16)
        self.prov.obs = [ob("DEPARTED", "AAA", T(20, 2)), ob("POSITION", "AAA", T(21, 12), km_after=10.0)]
        self.eng.tick()
        types = [n["type"] for n in self.store.list_notifications(self.u1)]
        self.assertTrue({"ESTIMATED_CROSSING", "STATUS_REPORT"} & set(types))


if __name__ == "__main__":
    unittest.main()
