import json
import unittest
from datetime import datetime, timedelta

from helpers import SD, T

from railvision.core.models import ObsKind
from railvision.core.status import build_status
from railvision.core.timeutil import IST
from railvision.providers.base import NotAuthorized, NotSupported, ProviderError
from railvision.providers.demo import DemoProvider
from railvision.providers.generic_http import GenericHttpProvider, dig
from railvision.providers.registry import SOURCE_MATRIX, ProviderRegistry

CFG = {
    "source_id": "mock-licensed", "base_url": "https://mock.example", "auth": {"type": "header", "name": "X-Key", "env": "MOCK_KEY"},
    "rate_limit_per_min": 100, "max_retries": 2, "_sleep": lambda s: None,
    "endpoints": {"train_route": {"path": "/route/{train_number}?d={date}", "ttl": 600},
                  "train_status": {"path": "/live/{train_number}?d={date}", "ttl": 30}},
    "map": {
        "route": {"list": "data.route", "code": "stn", "name": "nm", "km": "km", "day": "day", "arr": "a", "dep": "d",
                  "train_name": "data.name"},
        "status": {"list": "data.rows", "code": "stn", "day": "day", "delay": "late", "halt": "stop",
                   "actual_arr": "aa", "actual_dep": "ad", "platform": "pf", "updated_at": "data.updated",
                   "position_station": "data.pos.after", "position_km_after": "data.pos.km", "cancelled": "data.cancelled"},
    },
}
ROUTE = {"data": {"name": "MOCK EXP", "route": [
    {"stn": "AAA", "nm": "Alpha", "km": 0, "day": 1, "a": "--", "d": "20:00"},
    {"stn": "RE", "nm": "Rewari", "km": 60, "day": 1, "a": "20:50", "d": "20:50"},
    {"stn": "BBB", "nm": "Bravo", "km": 120, "day": 1, "a": "21:40", "d": "--"}]}}
LIVE = {"data": {"updated": "2026-10-14T20:58:00+05:30", "cancelled": False, "pos": {"after": "RE", "km": 4.5}, "rows": [
    {"stn": "AAA", "day": 1, "late": 3, "stop": True, "aa": None, "ad": "20:03", "pf": "2"},
    {"stn": "RE", "day": 1, "late": 5, "stop": False, "aa": "20:55", "ad": "20:55"},
    {"stn": "BBB", "day": 1, "late": None, "stop": True, "aa": None, "ad": None}]}}


class MockTransport:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def __call__(self, url, headers, timeout):
        self.calls.append((url, headers))
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(r, Exception):
            raise r
        return r


def ok(obj):
    return 200, json.dumps(obj).encode()


class GenericHttpTests(unittest.TestCase):
    def setUp(self):
        import os
        os.environ["MOCK_KEY"] = "secret-key"
        self.clock = lambda: T(21, 0)

    def test_route_and_status_mapping(self):
        tr = MockTransport([ok(ROUTE), ok(LIVE)])
        p = GenericHttpProvider(CFG, transport=tr, clock=self.clock)
        r = p.get_train_route("12345", SD)
        self.assertEqual([s.code for s in r.stops], ["AAA", "RE", "BBB"])
        self.assertFalse(r.stops[1].halt)   # arr == dep -> run-through
        self.assertIsNone(r.stops[0].arr)
        obs = p.get_train_status("12345", SD)
        kinds = [(o.kind, o.station_code) for o in obs]
        self.assertIn((ObsKind.DEPARTED, "AAA"), kinds)
        self.assertIn((ObsKind.PASSED, "RE"), kinds)
        self.assertIn((ObsKind.POSITION, "RE"), kinds)
        self.assertEqual(obs[0].payload.get("platform"), "2")
        self.assertEqual(obs[0].source_time, datetime(2026, 10, 14, 20, 58, tzinfo=IST))
        self.assertEqual(tr.calls[0][1]["X-Key"], "secret-key")
        st = build_status(r, obs, SD, T(21, 0))
        self.assertEqual(st["state"], "RUNNING")
        self.assertFalse(st["is_demo"])

    def test_cache_shares_responses(self):
        tr = MockTransport([ok(LIVE)])
        p = GenericHttpProvider(CFG, transport=tr, clock=self.clock)
        p.get_train_status("12345", SD)
        p.get_train_status("12345", SD)
        self.assertEqual(len(tr.calls), 1)
        self.assertEqual(p.stats["cache_hits"], 1)

    def test_retry_then_success(self):
        tr = MockTransport([(503, b""), ok(LIVE)])
        p = GenericHttpProvider(CFG, transport=tr, clock=self.clock)
        self.assertTrue(p.get_train_status("12345", SD))
        self.assertEqual(len(tr.calls), 2)

    def test_auth_errors_and_missing_key(self):
        p = GenericHttpProvider(CFG, transport=MockTransport([(401, b"")]), clock=self.clock)
        with self.assertRaises(NotAuthorized):
            p.get_train_status("12345", SD)
        import os
        del os.environ["MOCK_KEY"]
        with self.assertRaises(NotAuthorized):
            GenericHttpProvider(CFG, transport=MockTransport([ok(LIVE)])).get_train_status("12345", SD)

    def test_circuit_breaker_opens(self):
        tr = MockTransport([(500, b"")])
        p = GenericHttpProvider({**CFG, "breaker_threshold": 2, "max_retries": 0}, transport=tr, clock=self.clock)
        for _ in range(2):
            with self.assertRaises(ProviderError):
                p.get_train_status("12345", SD)
        n = len(tr.calls)
        with self.assertRaises(ProviderError) as cm:
            p.get_train_status("12345", SD)
        self.assertIn("circuit OPEN", str(cm.exception))
        self.assertEqual(len(tr.calls), n)          # no upstream call while open
        self.assertEqual(p.get_source_health()["status"], "DOWN")

    def test_malformed_and_network_errors(self):
        p = GenericHttpProvider({**CFG, "max_retries": 0}, transport=MockTransport([(200, b"<html>")]), clock=self.clock)
        with self.assertRaises(ProviderError):
            p.get_train_status("12345", SD)
        p2 = GenericHttpProvider({**CFG, "max_retries": 0}, transport=MockTransport([ProviderError("timeout")]), clock=self.clock)
        with self.assertRaises(ProviderError):
            p2.get_train_status("12345", SD)

    def test_cancelled_flag(self):
        live = json.loads(json.dumps(LIVE))
        live["data"]["cancelled"] = True
        p = GenericHttpProvider(CFG, transport=MockTransport([ok(live)]), clock=self.clock)
        self.assertEqual(p.get_train_status("12345", SD)[0].kind, ObsKind.CANCELLED)

    def test_https_required_and_unsupported_endpoint(self):
        with self.assertRaises(ValueError):
            GenericHttpProvider({**CFG, "base_url": "http://insecure"})
        p = GenericHttpProvider(CFG, transport=MockTransport([ok(LIVE)]))
        with self.assertRaises(NotSupported):
            p.get_chart_vacancy("12345", SD, "AAA", "3A")

    def test_dig(self):
        self.assertEqual(dig({"a": [{"b": 5}]}, "a[0].b"), 5)
        self.assertIsNone(dig({"a": []}, "a[3].b"))


class DemoAndRegistryTests(unittest.TestCase):
    def test_registry_refuses_mixing(self):
        with self.assertRaises(ValueError):
            ProviderRegistry("live", [DemoProvider()])
        import os
        os.environ["MOCK_KEY"] = "k"
        with self.assertRaises(ValueError):
            ProviderRegistry("demo", [GenericHttpProvider(CFG)])

    def test_registry_falls_back_to_official_link(self):
        reg = ProviderRegistry("live", [])
        with self.assertRaises(NotSupported) as cm:
            reg.call("get_chart_vacancy", "12951", SD, "NDLS", "3A")
        self.assertIn("irctc.co.in", cm.exception.official_url)

    def test_demo_everything_labelled(self):
        now = T(9, 40)
        p = DemoProvider(clock=lambda: now)
        sd = p.default_service_date("90001")
        r = p.get_train_route("90001", sd)
        self.assertTrue(r.is_demo and "SYNTHETIC" in r.train_name)
        self.assertTrue(all(o.is_demo for o in p.get_train_status("90001", sd)))
        v = p.get_chart_vacancy("90001", sd, "JP", "3A")
        self.assertTrue(v.is_demo)
        self.assertTrue(any("SYNTHETIC" in n for n in v.notes))
        with self.assertRaises(NotSupported):
            p.get_train_route("12951", sd)   # real numbers are never faked

    def test_demo_observations_never_from_future(self):
        now = T(9, 40)
        p = DemoProvider(clock=lambda: now)
        for n in ("90001", "90003", "90004"):
            for o in p.get_train_status(n, p.default_service_date(n)):
                self.assertLessEqual(o.source_time, now)

    def test_source_matrix_complete(self):
        need = {"source", "status", "fields", "coverage", "update_frequency", "auth", "terms", "cost_limits",
                "approval", "tests"}
        for row in SOURCE_MATRIX:
            self.assertTrue(need <= set(row), row["source"])


if __name__ == "__main__":
    unittest.main()
