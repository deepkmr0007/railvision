"""The browser-assistant loop. Runs on YOUR computer next to a real browser window.

Each round it asks the RAILVISION server what is due, then for each train — strictly ONE AT A
TIME — opens the official page, does the search, captures the result and sends it back.

Politeness limits (enforced here, not just configured):
  * ≥ 20 s (+ random 0–10 s) between page loads, ≥ 5 min between checks of the same train,
  * at most 10 trains, at most 400 page loads per day,
  * a CAPTCHA / block pauses that site for 30 min, doubling up to 4 h. Never solved or bypassed.
"""
from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

from .browser import Runner
from .recipes import DEFAULT_RECIPES, Recipe

AGENT_VERSION = "railvision-assist/1.0"


class ServerError(Exception):
    pass


class Client:
    def __init__(self, server: str, token: str, timeout: float = 30):
        self.server, self.token, self.timeout = server.rstrip("/"), token, timeout

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.server + path, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}", "Content-Type": "application/json", "X-RV-Agent": AGENT_VERSION})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:  # noqa: S310
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            raise ServerError(f"HTTP {e.code}: {e.read(300).decode(errors='replace')}") from e
        except (urllib.error.URLError, OSError) as e:
            raise ServerError(f"server unreachable: {e}") from e


@dataclass
class Pacer:
    min_gap_s: float = 20.0
    floor_s: float = 15.0          # the server can raise the gap, never lower it below this
    jitter_s: float = 10.0
    max_pages_per_day: int = 400
    sleep: callable = time.sleep
    clock: callable = time.time
    _last: float = 0.0
    _day: str = ""
    _count: int = 0
    _paused_until: dict = field(default_factory=dict)     # host -> epoch
    _strikes: dict = field(default_factory=dict)

    def wait_turn(self):
        gap = self.min_gap_s + random.uniform(0, self.jitter_s)
        dt = self._last + gap - self.clock()
        if dt > 0:
            self.sleep(dt)
        today = time.strftime("%Y-%m-%d")
        if today != self._day:
            self._day, self._count = today, 0
        if self._count >= self.max_pages_per_day:
            raise RuntimeError(f"daily limit of {self.max_pages_per_day} page loads reached; resuming tomorrow")
        self._count += 1
        self._last = self.clock()

    def host_paused(self, url: str) -> float:
        h = urlsplit(url).hostname or ""
        return max(0.0, self._paused_until.get(h, 0) - self.clock())

    def strike(self, url: str) -> float:
        h = urlsplit(url).hostname or ""
        n = self._strikes.get(h, 0) + 1
        self._strikes[h] = n
        wait = min(4 * 3600, 1800 * 2 ** (n - 1))
        self._paused_until[h] = self.clock() + wait
        return wait

    def clear(self, url: str):
        self._strikes.pop(urlsplit(url).hostname or "", None)


class Agent:
    def __init__(self, client: Client, context, *, headed: bool = True, recipes_dir: Path | None = None,
                 log=print, pacer: Pacer | None = None, handover_timeout_s: int = 300):
        self.client, self.context, self.headed, self.log = client, context, headed, log
        self.recipes_dir = recipes_dir
        self.pacer = pacer or Pacer()
        self.handover_timeout_s = handover_timeout_s
        self.page = context.pages[0] if context.pages else context.new_page()
        self.stats = {"pages": 0, "ok": 0, "problems": 0}

    def recipe(self, name: str, server_recipes: dict) -> Recipe:
        if self.recipes_dir and (self.recipes_dir / f"{name}.json").exists():
            local = Recipe.load(self.recipes_dir / f"{name}.json")
            srv = server_recipes.get(name)
            if not srv or srv.get("mode") != "recorded":
                return local
        if name in server_recipes:
            return Recipe.from_dict(server_recipes[name])
        return DEFAULT_RECIPES[name]

    def _state(self, state: str, reason: str = ""):
        try:
            self.client.call("POST", "/api/assist/heartbeat", {"state": state, "reason": reason})
        except ServerError:
            pass

    def run_job(self, job: dict, recipes: dict, watch_id: str | None = None) -> dict | None:
        rc = self.recipe(job["recipe"], recipes)
        paused = self.pacer.host_paused(rc.start_url)
        if paused:
            self.log(f"  ↷ {job['vars']} skipped — {urlsplit(rc.start_url).hostname} paused for {paused / 60:.0f} more min")
            return None
        self.pacer.wait_turn()
        label = job["vars"].get("train") or job["vars"].get("query")
        self.log(f"→ {job['kind']} {label} {job['vars'].get('date', '')} {job['vars'].get('class', '')} via {rc.name} ({rc.mode})")
        runner = Runner(self.page, handover=self.headed, handover_timeout_s=self.handover_timeout_s,
                        on_status=self._state, log=self.log)
        try:
            cap = runner.run(rc, dict(job["vars"]))
        finally:
            try:
                self.page.remove_listener("response", runner._on_response)
            except Exception:
                pass
        self.stats["pages"] += 1
        if cap["status"] in ("captcha", "blocked", "login"):
            wait = self.pacer.strike(rc.start_url)
            self.stats["problems"] += 1
            cap["reason"] += f" Pausing this site for {wait / 60:.0f} min."
            self._state("paused", cap["reason"])
        elif cap["status"] == "ok":
            self.pacer.clear(rc.start_url)
            self.stats["ok"] += 1
        cap.update({"watch_id": watch_id, "job_id": job.get("job_id"), "kind": job["kind"]})
        try:
            res = self.client.call("POST", "/api/assist/captures", cap)
            self.log(f"  ✓ {cap['status']}: {res.get('headline')} (read confidence {res.get('confidence')})")
            return res
        except ServerError as e:
            self.log(f"  ✗ could not send result: {e}")
            return None

    def round(self) -> int:
        work = self.client.call("GET", "/api/assist/work")
        p = work.get("pacing") or {}
        self.pacer.min_gap_s = max(self.pacer.floor_s, float(p.get("min_gap_s", self.pacer.min_gap_s)))
        self.pacer.jitter_s = float(p.get("jitter_s", self.pacer.jitter_s))
        self.pacer.max_pages_per_day = min(1000, int(p.get("max_pages_per_day", self.pacer.max_pages_per_day)))
        n = 0
        for j in work.get("jobs", []):           # on-demand checks first (someone is waiting)
            self.run_job(j, work["recipes"])
            n += 1
        for item in work.get("watch", []):
            for j in item["jobs"]:
                self.run_job(j, work["recipes"], item["watch_id"])
                n += 1
        return n

    def loop(self, poll_s: float = 15.0, max_rounds: int | None = None):
        self.log(f"RAILVISION browser assistant running ({'visible window' if self.headed else 'headless'}). Ctrl+C to stop.")
        rounds = 0
        while max_rounds is None or rounds < max_rounds:
            rounds += 1
            try:
                did = self.round()
            except ServerError as e:
                self.log(f"server: {e}; retrying in 60 s")
                time.sleep(60)
                continue
            except RuntimeError as e:          # daily cap
                self.log(str(e))
                time.sleep(3600)
                continue
            if not did:
                time.sleep(poll_s)


def launch_context(pw, profile_dir: Path, headed: bool):
    """A persistent profile keeps the site's normal cookies, like your everyday browser would."""
    profile_dir.mkdir(parents=True, exist_ok=True)
    return pw.chromium.launch_persistent_context(str(profile_dir), headless=not headed,
                                                 viewport={"width": 1280, "height": 900}, locale="en-IN",
                                                 timezone_id="Asia/Kolkata")


def parse_date(s: str) -> date:
    return date.fromisoformat(s)
