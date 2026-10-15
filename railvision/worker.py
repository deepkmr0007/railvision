"""Standalone scheduler worker:  python -m railvision.worker

Runs tracking ticks against the shared database. Run exactly ONE worker per database for the
SQLite build (the PostgreSQL build should claim due tasks with SELECT ... FOR UPDATE SKIP LOCKED).
"""
from __future__ import annotations

import os
import signal
import time

from .api.app import App


def main():
    app = App()
    every = float(os.environ.get("RAILVISION_TICK_SECONDS", "30"))
    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print(f"RAILVISION worker ({app.mode}) ticking every {every}s")
    while running:
        try:
            s = app.tracker.tick()
            if s["due"]:
                print(f"tick: {s}")
            app.housekeeping()
        except Exception as e:
            print(f"tick error: {e!r}")
        time.sleep(every)


if __name__ == "__main__":
    main()
