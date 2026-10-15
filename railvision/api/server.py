"""Zero-dependency HTTP server (Python stdlib) + background scheduler.

    python -m railvision.api.server --port 8000            # demo mode by default

Put it behind a TLS-terminating reverse proxy (Caddy/nginx) in any non-local deployment.
For multi-worker production use the ASGI wrapper (api/fastapi_app.py) + separate worker.
"""
from __future__ import annotations

import argparse
import json
import queue
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from ..services.tracker import HousekeepingThread, SchedulerThread
from .app import App, Request, parse_query


def make_handler(app: App):
    class H(BaseHTTPRequestHandler):
        server_version = "RAILVISION"
        sys_version = ""

        def log_message(self, fmt, *args):  # no query strings (tokens) in logs
            app.log(f"[http] {self.command} {urlsplit(self.path).path} -> {args[1] if len(args) > 1 else ''}")

        def _req(self) -> Request:
            u = urlsplit(self.path)
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(min(n, 7_000_000)) if n else b""
            return Request(self.command, u.path, parse_query(u.query),
                           {k.lower(): v for k, v in self.headers.items()}, body, self.client_address[0])

        def _send(self, st, hd, body):
            self.send_response(st)
            for k, v in hd.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _sse(self, req: Request):
            try:
                uid = app._auth(req)
            except Exception:
                return self._send(401, {"Content-Type": "application/json"}, b'{"error":"auth"}')
            q = app.bus.subscribe(uid)
            self.send_response(200)
            for k, v in {"Content-Type": "text/event-stream", "Cache-Control": "no-cache",
                         "X-Accel-Buffering": "no", **app.cors_headers(req)}.items():
                self.send_header(k, v)
            self.end_headers()
            try:
                self.wfile.write(b": connected\n\n")
                self.wfile.flush()
                while True:
                    try:
                        ev = q.get(timeout=20)
                        self.wfile.write(f"event: {ev.get('type', 'message')}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n".encode())
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                app.bus.unsubscribe(uid, q)

        def _any(self):
            req = self._req()
            if req.path == "/api/events" and req.method == "GET":
                return self._sse(req)
            self._send(*app.handle(req))

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _any

    return H


def main():
    ap = argparse.ArgumentParser(description="RAILVISION AI server")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--tick", type=float, default=30.0, help="scheduler tick seconds")
    ap.add_argument("--no-scheduler", action="store_true")
    a = ap.parse_args()
    app = App()
    if not a.no_scheduler:
        SchedulerThread(app.tracker, a.tick).start()
        HousekeepingThread(app).start()
    srv = ThreadingHTTPServer((a.host, a.port), make_handler(app))
    srv.daemon_threads = True
    print(f"RAILVISION AI {app.mode.upper()} mode on http://{a.host}:{a.port}  (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
