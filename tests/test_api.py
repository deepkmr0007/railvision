import hashlib
import hmac
import json
import os
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

from helpers import T

from railvision.api.app import App, Request
from railvision.api.server import make_handler
from railvision.providers.demo import DemoProvider
from railvision.providers.registry import ProviderRegistry
from railvision.services.store import Store


def make_app(**env):
    for k in ("WA_APP_SECRET", "WA_VERIFY_TOKEN", "TELEGRAM_WEBHOOK_SECRET", "RAILVISION_CORS_ORIGINS", "RAILVISION_RATE_PER_MIN"):
        os.environ.pop(k, None)
    os.environ.update(env)
    os.environ["RAILVISION_MODE"] = "demo"
    now = T(9, 40)
    return App(store=Store(), registry=ProviderRegistry("demo", [DemoProvider(clock=lambda: now)]),
               clock=lambda: now, logger=lambda *_: None)


def call(app, method, path, body=None, token=None, headers=None, query=None, ip="1.1.1.1"):
    h = {k.lower(): v for k, v in (headers or {}).items()}
    if token:
        h["authorization"] = f"Bearer {token}"
    raw = json.dumps(body).encode() if isinstance(body, (dict, list)) else (body or b"")
    st, hd, out = app.handle(Request(method, path, {k: [v] for k, v in (query or {}).items()}, h, raw, ip))
    try:
        return st, hd, json.loads(out)
    except ValueError:
        return st, hd, out


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.app = make_app(WA_APP_SECRET="sec", WA_VERIFY_TOKEN="vt", RAILVISION_CORS_ORIGINS="https://ok.example")
        st, _, b = call(self.app, "POST", "/api/session", {"display_name": "A"})
        self.assertEqual(st, 201)
        self.tok = b["token"]
        self.tok2 = call(self.app, "POST", "/api/session", {"display_name": "B"})[2]["token"]

    def test_public_endpoints(self):
        st, hd, b = call(self.app, "GET", "/api/health")
        self.assertEqual((st, b["mode"]), (200, "demo"))
        self.assertEqual(hd["X-Content-Type-Options"], "nosniff")
        self.assertIn("frame-ancestors 'none'", hd["Content-Security-Policy"])
        st, _, b = call(self.app, "GET", "/api/sources")
        self.assertGreaterEqual(len(b["matrix"]), 6)
        st, _, b = call(self.app, "GET", "/api/trains/90001/status", query={"station": "RE"})
        self.assertEqual(st, 200)
        self.assertTrue(b["status"]["is_demo"])
        self.assertIn(b["crossing"]["event_class"], ("ESTIMATED_CROSSING", "CONFIRMED_PASSAGE", "NOT_VERIFIED"))
        st, _, b = call(self.app, "GET", "/api/trains/90001/route")
        self.assertEqual(b["stops"][0]["code"], "JP")
        self.assertIsNotNone(b["stops"][0]["lat"])
        st, _, b = call(self.app, "GET", "/api/vacancy", query={"train": "90001", "from": "JP", "class": "3A"})
        self.assertIn("summary", b)
        self.assertIn("not reservation availability", b["disclaimer"])
        st, _, b = call(self.app, "GET", "/api/between", query={"from": "JP", "to": "DLI"})
        self.assertTrue(b["rows"])
        st, _, b = call(self.app, "GET", "/api/stations", query={"q": "delhi"})
        self.assertGreater(len(b["results"]), 3)
        st, _, b = call(self.app, "GET", "/api/stations/AWR/board", query={"hours": "12"})
        self.assertEqual(st, 200)

    def test_unknown_train_gives_manual_link_not_fake(self):
        st, _, b = call(self.app, "GET", "/api/trains/12951/status")
        self.assertEqual(st, 404)
        self.assertIn("official_url", b)

    def test_bad_input(self):
        self.assertEqual(call(self.app, "GET", "/api/trains/90001/status", query={"date": "14-10"})[0], 400)
        self.assertEqual(call(self.app, "GET", "/api/trains/90001/crossing")[0], 400)
        self.assertEqual(call(self.app, "GET", "/nope/../../etc/passwd")[0], 404)
        self.assertEqual(call(self.app, "POST", "/api/chat", b"{bad", token=self.tok)[0], 400)
        self.assertEqual(call(self.app, "POST", "/api/session", b"x" * 300_000)[0], 413)

    def test_auth_and_isolation(self):
        self.assertEqual(call(self.app, "GET", "/api/tasks")[0], 401)
        self.assertEqual(call(self.app, "GET", "/api/tasks", token="rv_forged")[0], 401)
        st, _, t = call(self.app, "POST", "/api/tasks", {"train_number": "90001", "reference_station": "RE",
                                                         "end_station": "DLI", "interval_min": 15, "destinations": ["app"]}, self.tok)
        self.assertEqual((st, t["status"]), (201, "PENDING_CONFIRMATION"))
        tid = t["id"]
        self.assertEqual(call(self.app, "GET", f"/api/tasks/{tid}", token=self.tok2)[0], 404)
        self.assertEqual(call(self.app, "POST", f"/api/tasks/{tid}/confirm", token=self.tok2)[0], 422)
        self.assertEqual(call(self.app, "DELETE", f"/api/tasks/{tid}", token=self.tok2)[0], 404)
        self.assertEqual(call(self.app, "POST", f"/api/tasks/{tid}/confirm", token=self.tok)[2]["status"], "ACTIVE")
        st, _, s = call(self.app, "POST", "/api/admin/tick", token=self.tok)
        self.assertEqual(s["due"], 1)
        self.assertEqual(call(self.app, "PATCH", f"/api/tasks/{tid}", {"interval_min": 30}, self.tok)[2]["interval_min"], 30)
        self.assertEqual(call(self.app, "PATCH", f"/api/tasks/{tid}", {"interval_min": 1}, self.tok)[0], 422)
        n = call(self.app, "GET", "/api/notifications", token=self.tok)[2]["items"]
        self.assertTrue(n and all(x["is_demo"] for x in n))
        self.assertEqual(call(self.app, "GET", "/api/notifications", token=self.tok2)[2]["items"], [])
        self.assertEqual(call(self.app, "DELETE", f"/api/tasks/{tid}", token=self.tok)[0], 200)

    def test_chat_endpoint(self):
        st, _, b = call(self.app, "POST", "/api/chat", {"text": "where is 90001"}, self.tok)
        self.assertEqual(st, 200)
        self.assertIn("DEMO", b["reply"])

    def test_whatsapp_webhook_security(self):
        st, _, b = call(self.app, "GET", "/webhooks/whatsapp", query={"hub.mode": "subscribe", "hub.verify_token": "vt",
                                                                      "hub.challenge": "777"})
        self.assertEqual((st, b), (200, 777))
        self.assertEqual(call(self.app, "GET", "/webhooks/whatsapp", query={"hub.mode": "subscribe",
                                                                           "hub.verify_token": "x", "hub.challenge": "1"})[0], 403)
        body = json.dumps({"object": "whatsapp_business_account", "entry": []}).encode()
        self.assertEqual(call(self.app, "POST", "/webhooks/whatsapp", body, headers={"X-Hub-Signature-256": "sha256=bad"})[0], 401)
        sig = "sha256=" + hmac.new(b"sec", body, hashlib.sha256).hexdigest()
        self.assertEqual(call(self.app, "POST", "/webhooks/whatsapp", body, headers={"X-Hub-Signature-256": sig})[0], 200)
        self.assertEqual(call(self.app, "POST", "/webhooks/telegram", {"update_id": 1})[0], 401)

    def test_cors(self):
        _, hd, _ = call(self.app, "GET", "/api/health", headers={"Origin": "https://ok.example"})
        self.assertEqual(hd.get("Access-Control-Allow-Origin"), "https://ok.example")
        _, hd, _ = call(self.app, "GET", "/api/health", headers={"Origin": "https://evil.example"})
        self.assertNotIn("Access-Control-Allow-Origin", hd)

    def test_rate_limit(self):
        app = make_app(RAILVISION_RATE_PER_MIN="5")
        codes = [call(app, "GET", "/api/health", ip="9.9.9.9")[0] for _ in range(7)]
        self.assertIn(429, codes)
        self.assertEqual(call(app, "GET", "/api/health", ip="8.8.8.8")[0], 200)

    def test_signup_disabled_in_live(self):
        os.environ["RAILVISION_MODE"] = "live"
        app = App(store=Store(), registry=ProviderRegistry("live", []), logger=lambda *_: None)
        self.assertEqual(call(app, "POST", "/api/session", {})[0], 403)
        st, _, b = call(app, "GET", "/api/trains/12951/status")
        self.assertEqual(st, 404)
        self.assertIn("enquiry.indianrail.gov.in", b["official_url"])
        os.environ["RAILVISION_MODE"] = "demo"


class RealServerTests(unittest.TestCase):
    """Boots the actual stdlib HTTP server on an ephemeral port and talks to it over TCP."""

    def test_server_end_to_end(self):
        app = make_app()
        srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        try:
            with urllib.request.urlopen(base + "/api/health", timeout=5) as r:
                self.assertEqual(json.loads(r.read())["ok"], True)
            with urllib.request.urlopen(base + "/", timeout=5) as r:
                html = r.read().decode()
                self.assertIn("RAILVISION", html)
            req = urllib.request.Request(base + "/api/session", data=b'{"display_name":"x"}', method="POST",
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as r:
                tok = json.loads(r.read())["token"]
            got = []

            def listen():
                with urllib.request.urlopen(base + f"/api/events?token={tok}", timeout=5) as r:
                    for line in r:
                        got.append(line.decode())
                        if line.startswith(b"data:"):
                            break
            lt = threading.Thread(target=listen, daemon=True)
            lt.start()
            time.sleep(0.3)
            uid = app.store.user_for_token(tok)
            app.bus.publish(uid, {"type": "notification", "title": "hello"})
            lt.join(3)
            self.assertTrue(any("hello" in g for g in got), got)
        finally:
            srv.shutdown()


if __name__ == "__main__":
    unittest.main()
