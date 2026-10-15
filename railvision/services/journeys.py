"""My Journeys: your trips, with reminders that follow the live running status.

Reminders (each sent once, re-sent only when the estimate moves a lot):
  day_before · chart time (≈4 h before the train leaves its origin) · leave home (uses your
  travel time + the live delay) · destination wake-up alarm · arrived.
Every time shown is labelled "scheduled", "expected (live delay)" or "reported".
"""
from __future__ import annotations

import json
import re
import secrets
from datetime import date, datetime, timedelta, timezone

from ..core.crossing import evaluate_crossing
from ..core.status import build_status
from ..core.timeutil import IST, fmt_hm, iso, now_ist, parse_iso
from ..providers.base import NotAuthorized, NotSupported, ProviderError

SCHEMA = """
CREATE TABLE IF NOT EXISTS journeys (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL, train TEXT NOT NULL, start_date TEXT NOT NULL, board TEXT NOT NULL,
  dest TEXT NOT NULL, cls TEXT, pnr TEXT, coach TEXT, berth TEXT, buffer_min INTEGER NOT NULL DEFAULT 60,
  alarm_min INTEGER NOT NULL DEFAULT 30, berth_check INTEGER NOT NULL DEFAULT 0, notes TEXT, label TEXT,
  status TEXT NOT NULL DEFAULT 'upcoming', watch_id TEXT, reminders TEXT NOT NULL DEFAULT '{}', last_pnr TEXT,
  last_eval TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_journeys_user ON journeys(user_id, status);
"""
CLASSES = ("1A", "2A", "3A", "3E", "SL", "CC", "EC", "2S", "EA", "FC")


class JourneyService:
    def __init__(self, store, tracker, dispatcher, clock=now_ist, assist=None, news=None, log=print):
        self.store, self.tracker, self.dispatcher, self.clock = store, tracker, dispatcher, clock
        self.assist, self.news, self.log = assist, news, log
        with store.lock:
            store.db.executescript(SCHEMA)
        if assist:
            assist.on_pnr = self.apply_pnr

    # ------------------------------------------------------------------ CRUD
    def _row(self, r):
        r = dict(r)
        r["reminders"] = json.loads(r["reminders"] or "{}")
        r["last_pnr"] = json.loads(r["last_pnr"]) if r.get("last_pnr") else None
        r["last_eval"] = json.loads(r["last_eval"]) if r.get("last_eval") else None
        r["berth_check"] = bool(r["berth_check"])
        return r

    def _validate(self, b: dict, partial=False) -> dict:
        f = {}
        today = self.clock().date()
        if "train" in b or not partial:
            if not re.fullmatch(r"\d{5}", str(b.get("train", ""))):
                raise ValueError("train must be a 5-digit number")
            f["train"] = str(b["train"])
        if "start_date" in b or not partial:
            try:
                d = date.fromisoformat(str(b.get("start_date")))
            except ValueError:
                raise ValueError("start_date: YYYY-MM-DD — the date the train starts from its origin")
            if not today - timedelta(days=3) <= d <= today + timedelta(days=125):
                raise ValueError("start_date must be between 3 days ago and 125 days ahead")
            f["start_date"] = d.isoformat()
        for k in ("board", "dest"):
            if k in b or not partial:
                v = str(b.get(k, "")).strip().upper()
                if not re.fullmatch(r"[A-Z]{1,5}", v):
                    raise ValueError(f"{k}: station code like NDLS")
                f[k] = v
        if "cls" in b:
            v = (b.get("cls") or "").upper() or None
            if v and v not in CLASSES:
                raise ValueError("class: " + "/".join(CLASSES))
            f["cls"] = v
        if "pnr" in b:
            v = re.sub(r"\D", "", str(b.get("pnr") or "")) or None
            if v and len(v) != 10:
                raise ValueError("PNR must be 10 digits")
            f["pnr"] = v
        for k in ("coach", "berth"):
            if k in b:
                v = str(b.get(k) or "").strip().upper()[:6] or None
                if v and not re.fullmatch(r"[A-Z0-9]{1,6}", v):
                    raise ValueError(f"{k}: letters/digits only")
                f[k] = v
        if "buffer_min" in b:
            f["buffer_min"] = max(0, min(300, int(b["buffer_min"])))
        if "alarm_min" in b:
            f["alarm_min"] = max(5, min(180, int(b["alarm_min"])))
        if "berth_check" in b:
            f["berth_check"] = int(bool(b["berth_check"]))
        for k in ("notes", "label"):
            if k in b:
                f[k] = str(b.get(k) or "")[:200]
        if "status" in b:
            if b["status"] not in ("upcoming", "active", "done", "cancelled"):
                raise ValueError("bad status")
            f["status"] = b["status"]
        if f.get("board") and f.get("dest") and f["board"] == f["dest"]:
            raise ValueError("boarding and destination must differ")
        return f

    def add(self, user_id: str, b: dict) -> dict:
        f = self._validate(b)
        n = self.store.q("SELECT COUNT(*) n FROM journeys WHERE user_id=? AND status IN ('upcoming','active')", (user_id,))[0]["n"]
        if n >= 30:
            raise ValueError("You already have 30 upcoming trips")
        jid = "j_" + secrets.token_hex(5)
        now = iso(self.clock())
        cols = ["id", "user_id", "created_at", "updated_at"] + list(f)
        vals = [jid, user_id, now, now] + list(f.values())
        self.store.x(f"INSERT INTO journeys({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", tuple(vals))
        self.store.audit(user_id, "journey.add", jid, {k: v for k, v in f.items() if k != "pnr"})
        self._ensure_watch(self.get(user_id, jid))
        return self.get(user_id, jid)

    def get(self, user_id: str, jid: str) -> dict | None:
        r = self.store.q("SELECT * FROM journeys WHERE id=? AND user_id=?", (jid, user_id))
        return self._row(r[0]) if r else None

    def list(self, user_id: str, include_done=True) -> list[dict]:
        sql = "SELECT * FROM journeys WHERE user_id=?" + ("" if include_done else " AND status IN ('upcoming','active')")
        return [self._row(r) for r in self.store.q(sql + " ORDER BY start_date, created_at", (user_id,))]

    def update(self, user_id: str, jid: str, b: dict) -> dict | None:
        if not self.get(user_id, jid):
            return None
        f = self._validate(b, partial=True)
        if f:
            sets = ", ".join(f"{k}=?" for k in f) + ", updated_at=?"
            self.store.x(f"UPDATE journeys SET {sets} WHERE id=? AND user_id=?", (*f.values(), iso(self.clock()), jid, user_id))
        return self.get(user_id, jid)

    def delete(self, user_id: str, jid: str) -> bool:
        j = self.get(user_id, jid)
        if not j:
            return False
        if j.get("watch_id") and self.assist:
            self.assist.delete_watch(user_id, j["watch_id"])
        self.store.x("DELETE FROM journeys WHERE id=? AND user_id=?", (jid, user_id))
        return True

    # ------------------------------------------------------------------ watch list link (browser assistant)
    def _ensure_watch(self, j: dict):
        if not self.assist or j["watch_id"] or j["status"] not in ("upcoming", "active"):
            return
        sd = date.fromisoformat(j["start_date"])
        today = self.clock().date()
        if not today - timedelta(days=2) <= sd <= today + timedelta(days=1):
            return                      # added automatically when the trip is near
        body = {"train": j["train"], "start_date": j["start_date"], "interval_min": 15, "ref_station": j["dest"],
                "end_station": j["dest"], "notify": "alerts_only", "label": f"Trip {j['board']}→{j['dest']}",
                "destinations": ["app", "whatsapp", "telegram"]}
        if j["berth_check"] and j.get("cls"):
            body["berth_checks"] = [{"station": j["board"], "class": j["cls"], "journey_date": j["start_date"]}]
        try:
            w = self.assist.add_watch(j["user_id"], body)
            self.store.x("UPDATE journeys SET watch_id=? WHERE id=?", (w["id"], j["id"]))
        except ValueError as e:
            if "already" in str(e):
                r = self.store.q("SELECT id FROM assist_watch WHERE user_id=? AND train=? AND start_date=?",
                                 (j["user_id"], j["train"], j["start_date"]))
                if r:
                    self.store.x("UPDATE journeys SET watch_id=? WHERE id=?", (r[0]["id"], j["id"]))
            else:
                self.log(f"[journeys] watch not added: {e}")

    # ------------------------------------------------------------------ live evaluation
    def evaluate(self, j: dict) -> dict:
        """Times for this trip, each labelled by how we know it."""
        now = self.clock()
        sd = date.fromisoformat(j["start_date"])
        ev = {"board": j["board"], "dest": j["dest"], "route_known": False, "live": False}
        try:
            route = self.tracker.route(j["train"], sd)
        except (NotSupported, ProviderError, NotAuthorized) as e:
            ev["note"] = f"No route yet ({e})"
            return ev
        ev["route_known"] = True
        ev["train_name"] = route.train_name
        bs, ds = route.stop(j["board"]), route.stop(j["dest"])
        if not bs or not ds:
            ev["note"] = f"{j['board'] if not bs else j['dest']} is not on this train's listed route"
            return ev
        ev["origin_dep"] = iso(route.stops[0].dep_dt(sd))
        ev["sched_dep"] = iso(bs.dep_dt(sd) or bs.arr_dt(sd))
        ev["sched_arr"] = iso(ds.arr_dt(sd) or ds.dep_dt(sd))
        ev["platform_board"] = bs.platform
        try:
            obs = self.tracker.observations(j["train"], sd)
        except (NotSupported, ProviderError, NotAuthorized):
            obs = []
        st = build_status(route, obs, sd, now) if obs is not None else None
        delay = st.get("delay_min") if st else None
        ev["delay_min"] = delay
        ev["state"] = st["state"] if st else "UNKNOWN"
        ev["live"] = bool(obs)
        ev["is_demo"] = route.is_demo
        b = evaluate_crossing(route, obs, j["board"], now, sd)
        if b.crossed and b.event_time:
            ev["dep"], ev["dep_basis"] = iso(b.event_time), "reported"
        elif b.crossed:
            ev["dep"], ev["dep_basis"] = iso(b.est_latest or parse_iso(ev["sched_dep"])), "passed (time bracketed)"
        else:
            base = parse_iso(ev["sched_dep"])
            ev["dep"] = iso(base + timedelta(minutes=max(delay or 0, 0)))
            ev["dep_basis"] = "expected (live delay)" if delay is not None else "scheduled"
        d = evaluate_crossing(route, obs, j["dest"], now, sd)
        ev["dest_verdict"] = d.event_class.value
        if d.crossed and d.event_time:
            ev["arr"], ev["arr_basis"] = iso(d.event_time), "reported"
        elif d.est_central and d.verification.value == "ESTIMATED":
            ev["arr"], ev["arr_basis"] = iso(d.est_central), "estimated from live position"
            ev["arr_window"] = [iso(d.est_earliest), iso(d.est_latest)]
        else:
            base = parse_iso(ev["sched_arr"])
            ev["arr"] = iso(base + timedelta(minutes=max(delay or 0, 0)))
            ev["arr_basis"] = "expected (live delay)" if delay is not None else "scheduled"
        ev["arrived"] = bool(d.crossed and d.verification.value in ("VERIFIED_LIVE", "DERIVED"))
        ev["cancelled"] = b.event_class.value == "CANCELLED"
        return ev

    # ------------------------------------------------------------------ reminders
    def _send(self, j: dict, key: str, title: str, text: str, urgent=False) -> int:
        nid = self.store.add_notification(j["user_id"], None, "JOURNEY_REMINDER", f"journey:{j['id']}:{key}", title, text,
                                          {"journey_id": j["id"], "kind": key}, False)
        if not nid:
            return 0
        dests = ["app", "browser", "whatsapp", "telegram"] if urgent else ["app", "browser"]
        if self.news:
            dests = list(dict.fromkeys(dests + self.news.prefs(j["user_id"])["digest_destinations"]))
        self.dispatcher.deliver(nid, j["user_id"], {"title": title, "text": text, "short": text[:200], "is_demo": False,
                                                     "template_params": []}, None, dests)
        return 1

    def _seat(self, j) -> str:
        if j.get("coach") and j.get("berth"):
            return f" · coach {j['coach']} berth {j['berth']}"
        p = j.get("last_pnr") or {}
        cur = [x.get("current") or {} for x in p.get("passengers", [])]
        if cur and cur[0].get("coach"):
            return f" · {cur[0].get('raw')}"
        return ""

    def tick(self) -> dict:
        now = self.clock()
        sent = 0
        for r in self.store.q("SELECT * FROM journeys WHERE status IN ('upcoming','active')"):
            j = self._row(r)
            self._ensure_watch(j)
            try:
                ev = self.evaluate(j)
            except Exception as e:      # one bad trip must not stop the rest
                self.log(f"[journeys] {j['id']}: {e!r}")
                continue
            rem = j["reminders"]
            name = f"{j['train']} {ev.get('train_name') or ''}".strip()
            changed = {}
            if ev.get("cancelled"):
                sent += self._send(j, "cancelled", f"{j['train']} cancelled", f"⛔ {name} is reported CANCELLED for start date "
                                   f"{j['start_date']}. Check refunds/alternatives on IRCTC.", urgent=True)
                changed["status"] = "cancelled"
            elif ev.get("sched_dep"):
                dep = parse_iso(ev["dep"])
                arr = parse_iso(ev["arr"])
                to_dep = (dep - now).total_seconds() / 60
                if now >= dep - timedelta(hours=3) and j["status"] == "upcoming":
                    changed["status"] = "active"
                # day before / same-day heads-up
                if "day_before" not in rem and 180 < to_dep <= 26 * 60:
                    sent += self._send(j, "day_before", f"Trip {fmt_day(dep, now)}: {j['train']}",
                                       f"🧳 {name}\n{j['board']} → {j['dest']} · departs {fmt_hm(dep)} ({ev['dep_basis']}) "
                                       f"on {dep:%a %d %b}{self._seat(j)}" + (f" · PF {ev['platform_board']}" if ev.get("platform_board") else "")
                                       + (f"\nPNR …{j['pnr'][-3:]} saved — check status before the chart." if j.get("pnr") else ""))
                    rem["day_before"] = iso(now)
                # chart time (≈4 h before origin departure) — only matters if not confirmed
                od = parse_iso(ev["origin_dep"])
                pnr_ok = (j.get("last_pnr") or {}).get("all_confirmed")
                if "chart" not in rem and od - timedelta(hours=4, minutes=15) <= now < od and not pnr_ok:
                    sent += self._send(j, "chart", f"Chart time · {j['train']}",
                                       f"📋 The chart for {name} is usually prepared about 4 h before it leaves its origin "
                                       f"({fmt_hm(od)}).\n" + ("Open RAILVISION → My Journeys → Check PNR (you type the CAPTCHA)."
                                                               if j.get("pnr") else "Add your PNR to My Journeys to check it.")
                                       + ("\nVacant-berth check is on for this trip." if j["berth_check"] else ""))
                    rem["chart"] = iso(now)
                # leave home
                leave = dep - timedelta(minutes=j["buffer_min"] + 15)
                if "leave" not in rem and leave - timedelta(minutes=10) <= now < dep:
                    sent += self._send(j, "leave", f"Leave by {fmt_hm(leave)} · {j['train']}",
                                       f"🚶 Leave by {fmt_hm(leave)} for {name}.\nDeparts {j['board']} at {fmt_hm(dep)} ({ev['dep_basis']})"
                                       + (f", delay {ev['delay_min']:+d} min" if ev.get("delay_min") is not None else "")
                                       + f". Your travel time: {j['buffer_min']} min + 15 min margin.", urgent=True)
                    rem["leave"] = iso(now)
                    rem["leave_dep"] = ev["dep"]
                elif "leave" in rem and "leave_update" not in rem and rem.get("leave_dep") and now < dep:
                    shift = (dep - parse_iso(rem["leave_dep"])).total_seconds() / 60
                    if abs(shift) >= 15:
                        sent += self._send(j, "leave_update", f"Update: departure moved {shift:+.0f} min",
                                           f"⏱️ {name} now expected at {j['board']} {fmt_hm(dep)} ({ev['dep_basis']}), "
                                           f"{shift:+.0f} min from the time in your leave reminder.", urgent=True)
                        rem["leave_update"] = iso(now)
                # destination alarm
                to_arr = (arr - now).total_seconds() / 60
                departed = now >= dep or ev.get("dep_basis") in ("reported", "passed (time bracketed)")
                if "dest_alarm" not in rem and departed and 0 < to_arr <= j["alarm_min"] and not ev.get("arrived"):
                    sent += self._send(j, "dest_alarm", f"⏰ ~{to_arr:.0f} min to {j['dest']}",
                                       f"⏰ Wake up: about {to_arr:.0f} min to {j['dest']} (expected {fmt_hm(arr)}, {ev['arr_basis']}).\n"
                                       "Pack your things; check the station name before getting down.", urgent=True)
                    rem["dest_alarm"] = iso(now)
                if ev.get("arrived") and "arrived" not in rem:
                    sent += self._send(j, "arrived", f"Arrived · {j['dest']}", f"✅ {name} reported at {j['dest']} at {fmt_hm(arr)}. "
                                       "Trip marked done. Have a good day!")
                    rem["arrived"] = iso(now)
                    changed["status"] = "done"
                if now > arr + timedelta(hours=6):
                    changed["status"] = "done"
            elif date.fromisoformat(j["start_date"]) < now.date() - timedelta(days=2):
                changed["status"] = "done"
            self.store.x("UPDATE journeys SET reminders=?, last_eval=?, updated_at=?" + (", status=?" if "status" in changed else "") +
                         " WHERE id=?", (json.dumps(rem), json.dumps(ev), iso(now), *([changed["status"]] if "status" in changed else []), j["id"]))
        return {"sent": sent}

    # ------------------------------------------------------------------ PNR
    def check_pnr(self, user_id: str, jid: str) -> dict:
        j = self.get(user_id, jid)
        if not j:
            raise LookupError("journey not found")
        if not j.get("pnr"):
            raise ValueError("Add the 10-digit PNR to this trip first")
        if not self.assist:
            raise ValueError("PNR check needs the browser assistant (start RAILVISION with start.bat / start.sh)")
        return self.assist.queue_job(user_id, "pnr", {"pnr": j["pnr"]})

    def apply_pnr(self, user_id: str, pr: dict):
        if not pr.get("pnr"):
            return
        for r in self.store.q("SELECT * FROM journeys WHERE user_id=? AND pnr=?", (user_id, pr["pnr"])):
            j = self._row(r)
            prev = j.get("last_pnr") or {}
            upd = {"last_pnr": json.dumps(pr)}
            first = (pr.get("passengers") or [{}])[0].get("current") or {}
            if first.get("status") == "CNF" and first.get("coach") and first.get("number"):
                upd.update(coach=first["coach"], berth=first["number"] + (f" {first['berth_type']}" if first.get("berth_type") else ""))
            sets = ", ".join(f"{k}=?" for k in upd)
            self.store.x(f"UPDATE journeys SET {sets} WHERE id=?", (*upd.values(), j["id"]))
            old = [(p.get("current") or {}).get("raw") for p in prev.get("passengers", [])]
            new = [(p.get("current") or {}).get("raw") for p in pr.get("passengers", [])]
            if prev and old != new:
                self._send({**j}, f"pnr_change:{'|'.join(map(str, new))}", f"PNR status changed · {j['train']}",
                           "🎫 PNR status changed: " + "; ".join(f"P{i + 1} {o or '?'} → {n or '?'}" for i, (o, n) in
                                                                 enumerate(zip(old + [None] * len(new), new))), urgent=True)

    # ------------------------------------------------------------------ calendar
    def ics(self, journeys: list[dict]) -> str:
        def z(dt: datetime) -> str:
            return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

        def esc(s: str) -> str:
            return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")
        lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//RAILVISION AI//My Journeys//EN", "CALSCALE:GREGORIAN",
                 "X-WR-CALNAME:RAILVISION trips", "X-WR-TIMEZONE:Asia/Kolkata"]
        for j in journeys:
            ev = j.get("last_eval") or {}
            dep = parse_iso(ev.get("sched_dep")) if ev.get("sched_dep") else None
            arr = parse_iso(ev.get("sched_arr")) if ev.get("sched_arr") else None
            lines += ["BEGIN:VEVENT", f"UID:{j['id']}@railvision", f"DTSTAMP:{z(self.clock())}"]
            if dep and arr and arr > dep:
                lines += [f"DTSTART:{z(dep)}", f"DTEND:{z(arr)}"]
            else:
                d = date.fromisoformat(j["start_date"])
                lines += [f"DTSTART;VALUE=DATE:{d:%Y%m%d}", f"DTEND;VALUE=DATE:{d + timedelta(days=1):%Y%m%d}"]
            desc = [f"Train {j['train']} {ev.get('train_name') or ''}".strip(), f"{j['board']} → {j['dest']}",
                    f"Start date (origin): {j['start_date']}"]
            if j.get("cls"):
                desc.append(f"Class {j['cls']}")
            if j.get("coach"):
                desc.append(f"Coach {j['coach']} berth {j.get('berth') or ''}")
            if j.get("pnr"):
                desc.append(f"PNR {j['pnr']}")
            desc.append("Times are scheduled; RAILVISION sends live updates.")
            summary = "🚆 {} {}→{}".format(j["train"], j["board"], j["dest"])
            lines += [f"SUMMARY:{esc(summary)}",
                      f"LOCATION:{esc(j['board'] + ' railway station')}", f"DESCRIPTION:{esc(chr(10).join(desc))}"]
            if dep:
                lines += ["BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{esc('Leave for ' + j['board'])}",
                          f"TRIGGER:-PT{j['buffer_min'] + 15}M", "END:VALARM"]
            lines.append("END:VEVENT")
        lines.append("END:VCALENDAR")
        return "\r\n".join(lines) + "\r\n"


def fmt_day(dt: datetime, now: datetime) -> str:
    d = dt.astimezone(IST).date()
    return "today" if d == now.date() else "tomorrow" if d == now.date() + timedelta(days=1) else dt.strftime("%a %d %b")
