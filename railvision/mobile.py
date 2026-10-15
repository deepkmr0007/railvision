"""Entry point for the Android app (Chaquopy). The Kotlin side calls these from a background thread.

    start(data_dir, web_dir) -> '{"port": 8765, "token": "...", "version": "..."}'
    run_once()               -> '{"tracker": {...}, "housekeeping": {...}}'   (WorkManager's 15-minute job)
    status()                 -> '{"running": true, ...}'

Everything stays on the phone: the database, the token and the settings live in the app's private
storage (data_dir). The server listens on 127.0.0.1 only, so other devices can't reach it.
"""
from __future__ import annotations

import json
import os
import secrets
import socket
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

_lock = threading.Lock()
_state: dict = {}


def _free_port(preferred: int) -> int:
    for p in [preferred] + list(range(preferred + 1, preferred + 20)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    return 0


def _secret(path: Path) -> str:
    if not path.exists():
        path.write_text(secrets.token_hex(32))
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return path.read_text().strip()


def start(data_dir: str, web_dir: str, port: int = 8765) -> str:
    with _lock:
        if _state.get("server"):
            return json.dumps(_public())
        data = Path(data_dir)
        data.mkdir(parents=True, exist_ok=True)
        env_file = data / "settings.env"             # optional keys (X bearer token, WhatsApp...) set from the app
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())
        try:
            import certifi                            # HTTPS for PIB / zonal sites from Python on Android
            os.environ.setdefault("SSL_CERT_FILE", certifi.where())
        except ImportError:
            pass
        port = _free_port(port)
        os.environ.setdefault("RAILVISION_MODE", "live")
        os.environ.setdefault("RAILVISION_ASSIST", "true")
        os.environ["RAILVISION_DB"] = str(data / "railvision.db")
        os.environ["RAILVISION_SECRET"] = _secret(data / "secret.txt")
        os.environ["RAILVISION_WEB_DIR"] = str(web_dir)
        os.environ["RAILVISION_APP_URL"] = f"http://127.0.0.1:{port}"
        os.environ.setdefault("RAILVISION_PLATFORM", "android")

        from . import __version__
        from .api import app as app_mod
        from .api.server import make_handler
        from .services.tracker import HousekeepingThread, SchedulerThread

        app_mod.WEB_DIR = Path(web_dir)
        app = app_mod.App()
        tok_f = data / "token.txt"
        token = tok_f.read_text().strip() if tok_f.exists() else ""
        if not token or not app.store.user_for_token(token):
            _, token = app.store.create_user("Owner")
            tok_f.write_text(token)
        sched = SchedulerThread(app.tracker, 30)
        sched.start()
        house = HousekeepingThread(app)
        house.start()
        srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True, name="railvision-http").start()
        _state.update(server=srv, app=app, port=port, token=token, version=__version__, sched=sched, house=house)
        return json.dumps(_public())


def _public() -> dict:
    return {"port": _state["port"], "token": _state["token"], "version": _state["version"], "running": True}


def run_once() -> str:
    """One pass of everything time-based. Safe to call while the background threads are also running."""
    app = _state.get("app")
    if not app:
        return json.dumps({"running": False})
    out: dict = {}
    try:
        out["tracker"] = app.tracker.tick()
    except Exception as e:  # noqa: BLE001
        out["tracker"] = {"error": repr(e)}
    out["housekeeping"] = app.housekeeping()
    return json.dumps(out, default=str)


def status() -> str:
    return json.dumps(_public() if _state.get("server") else {"running": False})
