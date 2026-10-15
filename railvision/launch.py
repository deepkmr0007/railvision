"""One command on your own computer:  python -m railvision.launch

Starts the RAILVISION server (live mode + browser assistant), opens the dashboard, and runs the
browser assistant in a visible window. Everything stays on this computer under ./assist_data/.

Options:  --no-agent (server only) · --headless (hide the browser; CAPTCHAs can't be handed to you)
          --port 8000 · --no-open (don't open the dashboard tab)
"""
from __future__ import annotations

import argparse
import os
import secrets
import sys
import threading
import webbrowser
from http.server import ThreadingHTTPServer
from pathlib import Path


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-agent", action="store_true")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--no-open", action="store_true")
    a = ap.parse_args(argv)

    data = Path(os.environ.get("RAILVISION_ASSIST_DIR", "assist_data"))
    data.mkdir(parents=True, exist_ok=True)
    secret_f = data / "secret.txt"
    if not secret_f.exists():
        secret_f.write_text(secrets.token_hex(32))
        try:
            os.chmod(secret_f, 0o600)
        except OSError:
            pass
    os.environ.setdefault("RAILVISION_MODE", "live")
    os.environ.setdefault("RAILVISION_ASSIST", "true")
    os.environ.setdefault("RAILVISION_DB", str(data / "railvision.db"))
    os.environ.setdefault("RAILVISION_SECRET", secret_f.read_text().strip())
    os.environ.setdefault("RAILVISION_APP_URL", f"http://{a.host}:{a.port}")

    from .api.app import App
    from .api.server import make_handler
    from .services.tracker import SchedulerThread

    app = App()
    tok_f = data / "token.txt"
    token = tok_f.read_text().strip() if tok_f.exists() else ""
    if not token or not app.store.user_for_token(token):
        _, token = app.store.create_user("Owner")
        tok_f.write_text(token)
        try:
            os.chmod(tok_f, 0o600)
        except OSError:
            pass
    SchedulerThread(app.tracker, 30).start()
    srv = ThreadingHTTPServer((a.host, a.port), make_handler(app))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://{a.host}:{a.port}/#token={token}"
    print(f"\nRAILVISION AI is running →  http://{a.host}:{a.port}\n(the first link below signs your browser in; keep it private)\n{url}\n")
    if not a.no_open:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    if a.no_agent:
        print("Server only. Ctrl+C to stop.")
        threading.Event().wait()
        return
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright is missing, so the browser assistant can't start.\n"
              "Install it:  pip install playwright  &&  python -m playwright install chromium\n"
              "The dashboard still runs. Ctrl+C to stop.")
        threading.Event().wait()
        return
    from .assist.agent import Agent, Client, launch_context
    with sync_playwright() as pw:
        ctx = launch_context(pw, data / "profile", headed=not a.headless)
        try:
            Agent(Client(f"http://{a.host}:{a.port}", token), ctx, headed=not a.headless, recipes_dir=data / "recipes").loop()
        except KeyboardInterrupt:
            print("\nStopped.")
        finally:
            ctx.close()


if __name__ == "__main__":
    sys.exit(main())
