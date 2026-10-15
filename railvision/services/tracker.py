"""Persistent tracking engine.

* Tasks live in the database, so monitoring continues when the app/browser is closed.
* SHARED POLLING: all due tasks for the same (train, service date) share ONE upstream fetch per tick.
* Exponential backoff per (train, date) group on provider failure; stale-data warnings.
* Notification de-duplication via unique dedupe keys (DB constraint), so retries/restarts never double-send.
"""
from __future__ import annotations

import threading
from collections import defaultdict
from datetime import date, datetime, time, timedelta

from ..core.crossing import evaluate_crossing
from ..core.status import build_status
from ..core.timeutil import iso, now_ist, parse_iso
from ..providers.base import NotAuthorized, NotSupported, ProviderError
from .messages import compose
from .resilience import backoff_seconds

MIN_INTERVAL_MIN = 5
MAX_INTERVAL_MIN = 240
ALLOWED_DESTINATIONS = ("app", "browser", "android_push", "email", "whatsapp", "whatsapp_group",
                        "whatsapp_channel", "telegram")


class TaskError(ValueError):
    pass


class TrackingEngine:
    def __init__(self, store, registry, dispatcher, mode: str = "demo", app_url: str = "",
                 clock=now_ist, route_ttl_s: int = 6 * 3600):
        self.store, self.registry, self.dispatcher = store, registry, dispatcher
        self.mode, self.app_url, self.clock = mode, app_url, clock
        self.route_ttl_s = route_ttl_s
        self._routes: dict[tuple, tuple[datetime, object]] = {}
        self._backoff: dict[tuple, tuple[datetime, int]] = {}
        self._lock = threading.Lock()
        self.metrics = defaultdict(int)

    # ------------------------------------------------------------------ data access
    def route(self, train: str, sd: date):
        key = (train, sd)
        hit = self._routes.get(key)
        if hit and (self.clock() - hit[0]).total_seconds() < self.route_ttl_s:
            return hit[1]
        _, r = self.registry.call("get_train_route", train, sd)
        self._routes[key] = (self.clock(), r)
        return r

    def observations(self, train: str, sd: date):
        p, obs = self.registry.call("get_train_status", train, sd)
        self.metrics["upstream_fetches"] += 1
        self.store.save_observations(obs)
        return obs

    def snapshot(self, train: str, sd: date, station: str | None = None) -> dict:
        r = self.route(train, sd)
        obs = self.observations(train, sd)
        now = self.clock()
        out = {"status": build_status(r, obs, sd, now)}
        if station:
            out["crossing"] = evaluate_crossing(r, obs, station, now, sd).to_dict()
        return out

    # ------------------------------------------------------------------ task lifecycle
    def draft_task(self, user_id: str, train: str, service_date: date, reference_station: str,
                   end_station: str | None, interval_min: int, destinations: list[str],
                   delay_threshold_min: int | None = None, quiet: tuple[str, str] | None = None) -> dict:
        if not (MIN_INTERVAL_MIN <= int(interval_min) <= MAX_INTERVAL_MIN):
            raise TaskError(f"interval must be {MIN_INTERVAL_MIN}–{MAX_INTERVAL_MIN} minutes")
        bad = [d for d in destinations if d not in ALLOWED_DESTINATIONS]
        if bad:
            raise TaskError(f"unknown destination(s): {', '.join(bad)}")
        try:
            r = self.route(train, service_date)
        except NotSupported as e:
            raise TaskError(f"Route for {train} unavailable from configured sources: {e}") from e
        ri = r.index_of(reference_station)
        if ri is None:
            raise TaskError(f"{reference_station} is not on the route of {train} ({r.stops[0].code}→{r.stops[-1].code})")
        if end_station:
            ei = r.index_of(end_station)
            if ei is None:
                raise TaskError(f"{end_station} is not on the route of {train}")
            if ei <= ri:
                raise TaskError(f"{end_station} comes before {reference_station} on this train's direction of travel")
        avail = {c["name"]: c for c in self.dispatcher.catalogue()}
        notes = []
        for d in destinations:
            if d in avail and not avail[d]["available"]:
                notes.append(f"{d}: {avail[d]['reason']} — app inbox will be used meanwhile")
        task = self.store.create_task(user_id, train_number=train, service_date=service_date.isoformat(),
                                      reference_station=reference_station, end_station=end_station,
                                      interval_min=int(interval_min), delay_threshold_min=delay_threshold_min,
                                      destinations=destinations, status="PENDING_CONFIRMATION", is_demo=r.is_demo,
                                      quiet_start=quiet[0] if quiet else None, quiet_end=quiet[1] if quiet else None)
        ref = r.stop(reference_station)
        task["summary"] = {
            "train": f"{train} {r.train_name}", "service_date": service_date.isoformat(),
            "reference_station": f"{ref.name} ({ref.code}) — {'scheduled halt' if ref.halt else 'NON-STOP (passes without halting)'}",
            "end_station": end_station or r.stops[-1].code, "interval_min": int(interval_min),
            "delay_threshold_min": delay_threshold_min, "destinations": destinations,
            "data_availability": ("DEMO synthetic data — not live" if r.is_demo else
                                  f"Source: {r.source_id}. Non-stop passage is CONFIRMED only when the source reports "
                                  "it (or the train is reported beyond it); otherwise shown as ESTIMATED."),
            "destination_notes": notes, "needs_confirmation": True,
        }
        return task

    def confirm(self, task_id: str, user_id: str) -> dict:
        t = self.store.get_task(task_id, user_id)
        if not t:
            raise TaskError("task not found")
        if t["status"] != "PENDING_CONFIRMATION":
            raise TaskError(f"task is {t['status']}")
        return self.store.update_task(task_id, user_id, status="ACTIVE", next_run_at=iso(self.clock()))

    def set_state(self, task_id: str, user_id: str, action: str) -> dict:
        t = self.store.get_task(task_id, user_id)
        if not t:
            raise TaskError("task not found")
        transitions = {"pause": ({"ACTIVE"}, "PAUSED"), "resume": ({"PAUSED"}, "ACTIVE"),
                       "cancel": ({"ACTIVE", "PAUSED", "PENDING_CONFIRMATION"}, "CANCELLED")}
        if action not in transitions:
            raise TaskError("action must be pause|resume|cancel")
        allowed_from, to = transitions[action]
        if t["status"] not in allowed_from:
            raise TaskError(f"cannot {action} a {t['status']} task")
        extra = {"next_run_at": iso(self.clock())} if action == "resume" else {}
        return self.store.update_task(task_id, user_id, status=to, **extra)

    def update(self, task_id: str, user_id: str, **changes) -> dict:
        t = self.store.get_task(task_id, user_id)
        if not t:
            raise TaskError("task not found")
        if "interval_min" in changes:
            v = int(changes["interval_min"])
            if not (MIN_INTERVAL_MIN <= v <= MAX_INTERVAL_MIN):
                raise TaskError(f"interval must be {MIN_INTERVAL_MIN}–{MAX_INTERVAL_MIN} minutes")
            changes["interval_min"] = v
            changes["next_run_at"] = iso(self.clock() + timedelta(minutes=v))
        if "destinations" in changes:
            bad = [d for d in changes["destinations"] if d not in ALLOWED_DESTINATIONS]
            if bad:
                raise TaskError(f"unknown destination(s): {', '.join(bad)}")
        return self.store.update_task(task_id, user_id, **changes)

    # ------------------------------------------------------------------ scheduler
    def tick(self) -> dict:
        now = self.clock()
        due = self.store.due_tasks(now)
        groups: dict[tuple, list] = defaultdict(list)
        for t in due:
            groups[(t["train_number"], t["service_date"])].append(t)
        summary = {"due": len(due), "groups": len(groups), "fetches": 0, "notifications": 0, "errors": 0}
        for (train, sd_s), tasks in groups.items():
            sd = date.fromisoformat(sd_s)
            key = (train, sd_s)
            bo = self._backoff.get(key)
            if bo and bo[0] > now:
                for t in tasks:
                    self.store.update_task(t["id"], None, next_run_at=iso(bo[0]))
                continue
            try:
                r = self.route(train, sd)
                obs = self.observations(train, sd)
                summary["fetches"] += 1
                self._backoff.pop(key, None)
            except (ProviderError, NotSupported, NotAuthorized) as e:
                attempts = (bo[1] if bo else 0) + 1
                until = now + timedelta(seconds=backoff_seconds(attempts))
                self._backoff[key] = (until, attempts)
                summary["errors"] += 1
                for t in tasks:
                    fc = t["fail_count"] + 1
                    self.store.update_task(t["id"], None, fail_count=fc, next_run_at=iso(until),
                                           last_run_at=iso(now), last_result={"error": str(e)[:300]})
                    if fc in (2, 6):  # warn once early, once more later
                        summary["notifications"] += self._notify_simple(
                            t, "STALE", f"{t['id']}:srcerr:{fc}",
                            f"⚠️ Data temporarily unavailable for {train}: {str(e)[:160]}. "
                            f"Retrying automatically (next try {until:%H:%M} IST).")
                continue
            status = build_status(r, obs, sd, now)
            for t in tasks:
                try:
                    summary["notifications"] += self._evaluate_task(t, r, obs, status, now)
                except Exception as e:  # one bad task must not block the rest
                    summary["errors"] += 1
                    self.store.update_task(t["id"], None, last_result={"error": f"{type(e).__name__}: {e}"[:300]},
                                           next_run_at=iso(now + timedelta(minutes=t["interval_min"])))
        self.metrics["ticks"] += 1
        return summary

    # ------------------------------------------------------------------ per-task logic
    def _in_quiet(self, t: dict, now: datetime) -> bool:
        if not t.get("quiet_start") or not t.get("quiet_end"):
            return False
        a, b, n = time.fromisoformat(t["quiet_start"]), time.fromisoformat(t["quiet_end"]), now.timetz().replace(tzinfo=None)
        return (a <= n < b) if a <= b else (n >= a or n < b)

    def _emit(self, t, event_type, dedupe_key, status, crossing, state="Active", extra="") -> int:
        msg = compose(event_type, t, status, crossing, self.app_url, self.mode, state, extra)
        nid = self.store.add_notification(t["user_id"], t["id"], event_type, dedupe_key, msg["title"], msg["text"],
                                          {"crossing": crossing, "short": msg["short"]}, msg["is_demo"])
        if nid is None:
            self.metrics["duplicates_suppressed"] += 1
            return 0
        self.dispatcher.deliver(nid, t["user_id"], msg, t, t["destinations"])
        return 1

    def _notify_simple(self, t, event_type, dedupe_key, text) -> int:
        msg = {"title": f"{t['train_number']} · {event_type}", "text": text, "short": text[:200],
               "is_demo": t["is_demo"], "template_params": []}
        nid = self.store.add_notification(t["user_id"], t["id"], event_type, dedupe_key, msg["title"], text, {}, t["is_demo"])
        if nid is None:
            return 0
        # template params unavailable for free-text warnings: deliver to app/telegram/email; WhatsApp only in-session
        self.dispatcher.deliver(nid, t["user_id"], msg, t, [d for d in t["destinations"]])
        return 1

    def _evaluate_task(self, t: dict, route, obs, status: dict, now: datetime) -> int:
        sd = date.fromisoformat(t["service_date"])
        c = evaluate_crossing(route, obs, t["reference_station"], now, sd)
        self.store.record_passage(t["id"], c)
        cd = c.to_dict()
        tid, sent = t["id"], 0
        quiet = self._in_quiet(t, now)
        prev = (t.get("last_result") or {}).get("crossing") or {}

        # Disruptions
        for a in status["alerts"]:
            if a["type"] in ("CANCELLED", "DIVERTED", "SHORT_TERMINATED", "RESCHEDULED"):
                et = "CANCELLED" if a["type"] == "CANCELLED" else "DIVERTED"
                sent += self._emit(t, et, f"{tid}:{a['type']}:{a['source_time']}", status, cd,
                                   extra=f"Alert: {a['type']} {a.get('station') or ''} {a.get('detail') or ''}".strip())
        if cd["event_class"] == "CANCELLED":
            self.store.update_task(tid, None, status="COMPLETED", last_run_at=iso(now), last_result={"crossing": cd})
            return sent

        # Crossing state transitions
        ec, ver = cd["event_class"], cd["verification"]
        if cd["crossed"] and ver == "VERIFIED_LIVE":
            sent += self._emit(t, "CONFIRMED_PASSAGE" if ec == "CONFIRMED_PASSAGE" else "SCHEDULED_HALT",
                               f"{tid}:cross:verified", status, cd)
        elif cd["crossed"] and ver == "DERIVED":
            sent += self._emit(t, "CONFIRMED_PASSAGE" if ec == "CONFIRMED_PASSAGE" else "SCHEDULED_HALT",
                               f"{tid}:cross:derived", status, cd)
        elif ec == "NOT_ON_ROUTE":
            # reference station dropped (diversion / short termination): tell the user once and finish
            if sent == 0:
                sent += self._emit(t, "DIVERTED", f"{tid}:notonroute", status, cd, state="Completed",
                                   extra="Reference station is no longer on this train's route; tracking ended.")
            self.store.update_task(tid, None, status="COMPLETED", last_run_at=iso(now),
                                   last_result={"crossing": cd, "state": status["state"]})
            return sent
        elif ver == "ESTIMATED" and cd.get("est_central") and not quiet:
            prev_c = parse_iso(prev.get("est_central")) if prev.get("est_central") else None
            cur_c = parse_iso(cd["est_central"])
            if prev_c is None or abs((cur_c - prev_c).total_seconds()) >= 10 * 60:
                sent += self._emit(t, "ESTIMATED_CROSSING", f"{tid}:est:{cur_c:%Y%m%d%H}{cur_c.minute // 10}", status, cd)

        # Delay threshold (fires on first breach, then every further +30 min)
        thr, dl = t.get("delay_threshold_min"), status.get("delay_min")
        if thr is not None and dl is not None and dl >= thr:
            step = (dl - thr) // 30
            sent += self._emit(t, "DELAY_ALERT", f"{tid}:delay:{step}", status, cd,
                               extra=f"Delay {dl} min exceeds your {thr}-min threshold")

        # Stale data
        if status["freshness"]["stale"]:
            sent += self._emit(t, "STALE", f"{tid}:stale:{now:%Y%m%d%H}", status, cd, state="Data temporarily unavailable")

        # Completion: reached end station, train completed, or service date long past
        end = t.get("end_station") or route.stops[-1].code
        end_rec = next((s for s in status["stops"] if s["code"] == end), None)
        reached = bool(end_rec and end_rec["status"] in ("PASSED", "AT_STATION") and end_rec.get("actual"))
        expired = now.date() > sd + timedelta(days=3)
        if reached or status["state"] == "COMPLETED" or expired:
            sent += self._emit(t, "COMPLETED", f"{tid}:completed", status, cd, state="Completed",
                               extra=("Train reported at end station " + end) if reached else
                                     ("Service date expired" if expired else ""))
            self.store.update_task(tid, None, status="COMPLETED", last_run_at=iso(now),
                                   last_result={"crossing": cd, "state": status["state"]})
            return sent

        # Periodic report (only if nothing else went out this run, and not in quiet hours)
        if sent == 0 and not quiet:
            slot = t.get("next_run_at") or iso(now)
            sent += self._emit(t, "STATUS_REPORT", f"{tid}:report:{slot}", status, cd)

        self.store.update_task(tid, None, last_run_at=iso(now), fail_count=0,
                               next_run_at=iso(now + timedelta(minutes=t["interval_min"])),
                               last_result={"crossing": cd, "state": status["state"], "delay_min": dl})
        return sent


class SchedulerThread(threading.Thread):
    """Background worker. In production run it as a separate worker process (see deploy/)."""

    def __init__(self, engine: TrackingEngine, every_s: float = 30.0):
        super().__init__(daemon=True, name="railvision-scheduler")
        self.engine, self.every_s = engine, every_s
        self._stop = threading.Event()
        self.last_summary: dict = {}

    def run(self):
        while not self._stop.is_set():
            try:
                self.last_summary = self.engine.tick()
            except Exception as e:  # keep the loop alive
                self.last_summary = {"error": repr(e)}
            self._stop.wait(self.every_s)

    def stop(self):
        self._stop.set()
