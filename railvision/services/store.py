"""Persistence (SQLite for dev/single-node; schema mirrored for PostgreSQL in deploy/).

Tasks, observations, passage-event audit history, notifications (with unique dedupe keys),
delivery attempts, consent records, WhatsApp links, chat context, audit log.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
from datetime import datetime
from typing import Any

from ..core.timeutil import iso, now_ist

SCHEMA_VERSION = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY, display_name TEXT, token_hash TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tracking_tasks (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
  train_number TEXT NOT NULL, service_date TEXT NOT NULL,
  reference_station TEXT NOT NULL, end_station TEXT,
  interval_min INTEGER NOT NULL, delay_threshold_min INTEGER,
  destinations TEXT NOT NULL, status TEXT NOT NULL,
  is_demo INTEGER NOT NULL DEFAULT 0, quiet_start TEXT, quiet_end TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  next_run_at TEXT, last_run_at TEXT, last_result TEXT, fail_count INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_tasks_due ON tracking_tasks(status, next_run_at);
CREATE TABLE IF NOT EXISTS observations (
  id INTEGER PRIMARY KEY AUTOINCREMENT, trace_id TEXT, train_number TEXT, service_date TEXT,
  kind TEXT, station_code TEXT, event_time TEXT, km_after REAL, source_id TEXT,
  source_time TEXT, retrieved_at TEXT, delay_min INTEGER, is_demo INTEGER, payload TEXT,
  UNIQUE(train_number, service_date, kind, station_code, event_time, km_after, source_id));
CREATE TABLE IF NOT EXISTS passage_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, train_number TEXT, service_date TEXT,
  station_code TEXT, event_class TEXT, verification TEXT, crossed INTEGER,
  event_time TEXT, est_earliest TEXT, est_latest TEXT, est_central TEXT, confidence REAL,
  method TEXT, method_version TEXT, source_id TEXT, source_time TEXT, is_demo INTEGER,
  created_at TEXT NOT NULL, supersedes INTEGER);
CREATE TABLE IF NOT EXISTS notifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, task_id TEXT, type TEXT NOT NULL,
  dedupe_key TEXT UNIQUE NOT NULL, title TEXT, body TEXT, payload TEXT, is_demo INTEGER,
  created_at TEXT NOT NULL, read_at TEXT);
CREATE TABLE IF NOT EXISTS delivery_attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, notification_id INTEGER, destination TEXT, status TEXT,
  detail TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS consent_records (
  id INTEGER PRIMARY KEY AUTOINCREMENT, subject_hash TEXT NOT NULL, channel TEXT NOT NULL,
  action TEXT NOT NULL, evidence TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS wa_links (
  phone_hash TEXT PRIMARY KEY, phone TEXT NOT NULL, user_id TEXT, opted_in INTEGER NOT NULL,
  last_inbound_at TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS telegram_links (chat_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS link_codes (code TEXT PRIMARY KEY, user_id TEXT NOT NULL, expires_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS processed_messages (msg_id TEXT PRIMARY KEY, received_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS chat_context (session_id TEXT PRIMARY KEY, user_id TEXT, context TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS channel_queue (
  id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT, text TEXT, task_id TEXT, created_at TEXT, published_at TEXT);
CREATE TABLE IF NOT EXISTS audit_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT, action TEXT, target TEXT, detail TEXT, created_at TEXT NOT NULL);
"""

TASK_STATES = ("PENDING_CONFIRMATION", "ACTIVE", "PAUSED", "COMPLETED", "CANCELLED")


def _j(v):
    return json.dumps(v, ensure_ascii=False, default=str)


class Store:
    def __init__(self, path: str = ":memory:", pepper: str | None = None):
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        if path != ":memory:":
            self.db.execute("PRAGMA journal_mode=WAL")
        self.lock = threading.RLock()
        self.clock = now_ist          # the App replaces this with its own clock (tests, replays)
        self.pepper = (pepper or secrets.token_hex(16)).encode()
        with self.lock:
            self.db.executescript(SCHEMA)
            if not self.db.execute("SELECT 1 FROM schema_version").fetchone():
                self.db.execute("INSERT INTO schema_version VALUES (?)", (SCHEMA_VERSION,))

    # ---- helpers -----------------------------------------------------------------
    def q(self, sql: str, args: tuple = ()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def x(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self.lock:
            return self.db.execute(sql, args)

    def hash_subject(self, value: str) -> str:
        return hmac.new(self.pepper, value.encode(), hashlib.sha256).hexdigest()

    def audit(self, actor: str, action: str, target: str = "", detail: dict | None = None):
        self.x("INSERT INTO audit_events(actor,action,target,detail,created_at) VALUES (?,?,?,?,?)",
               (actor, action, target, _j(detail or {}), iso(self.clock())))

    # ---- users / auth ---------------------------------------------------------------
    def create_user(self, display_name: str) -> tuple[str, str]:
        uid = "u_" + secrets.token_hex(6)
        token = "rv_" + secrets.token_urlsafe(24)
        self.x("INSERT INTO users VALUES (?,?,?,?)", (uid, display_name, self.hash_subject(token), iso(self.clock())))
        self.audit(uid, "user.create", uid)
        return uid, token

    def user_for_token(self, token: str) -> str | None:
        if not token:
            return None
        r = self.q("SELECT id FROM users WHERE token_hash=?", (self.hash_subject(token),))
        return r[0]["id"] if r else None

    # ---- tasks --------------------------------------------------------------------------
    def create_task(self, user_id: str, **f) -> dict:
        tid = "t_" + secrets.token_hex(6)
        now = iso(self.clock())
        self.x("""INSERT INTO tracking_tasks(id,user_id,train_number,service_date,reference_station,end_station,
                  interval_min,delay_threshold_min,destinations,status,is_demo,quiet_start,quiet_end,created_at,updated_at,next_run_at)
                  VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
               (tid, user_id, f["train_number"], f["service_date"], f["reference_station"], f.get("end_station"),
                int(f["interval_min"]), f.get("delay_threshold_min"), _j(f.get("destinations") or ["app"]),
                f.get("status", "PENDING_CONFIRMATION"), int(bool(f.get("is_demo"))), f.get("quiet_start"),
                f.get("quiet_end"), now, now, now))
        self.audit(user_id, "task.create", tid, f)
        return self.get_task(tid, user_id)

    def _task_row(self, r: dict) -> dict:
        r["destinations"] = json.loads(r["destinations"])
        r["last_result"] = json.loads(r["last_result"]) if r.get("last_result") else None
        r["is_demo"] = bool(r["is_demo"])
        return r

    def get_task(self, task_id: str, user_id: str | None) -> dict | None:
        """user_id=None only for the internal scheduler. API callers always pass their user id."""
        if user_id is None:
            rows = self.q("SELECT * FROM tracking_tasks WHERE id=?", (task_id,))
        else:
            rows = self.q("SELECT * FROM tracking_tasks WHERE id=? AND user_id=?", (task_id, user_id))
        return self._task_row(rows[0]) if rows else None

    def list_tasks(self, user_id: str, include_closed: bool = True) -> list[dict]:
        sql = "SELECT * FROM tracking_tasks WHERE user_id=?" + ("" if include_closed else " AND status IN ('ACTIVE','PAUSED','PENDING_CONFIRMATION')")
        return [self._task_row(r) for r in self.q(sql + " ORDER BY created_at DESC", (user_id,))]

    def update_task(self, task_id: str, user_id: str | None, **changes) -> dict | None:
        if not self.get_task(task_id, user_id):
            return None
        allowed = {"interval_min", "delay_threshold_min", "destinations", "status", "end_station", "next_run_at",
                   "last_run_at", "last_result", "fail_count", "quiet_start", "quiet_end", "reference_station"}
        sets, args = [], []
        for k, v in changes.items():
            if k not in allowed:
                raise ValueError(f"field {k} not updatable")
            if k == "status" and v not in TASK_STATES:
                raise ValueError("bad status")
            sets.append(f"{k}=?")
            args.append(_j(v) if k in ("destinations", "last_result") else v)
        sets.append("updated_at=?")
        args.append(iso(self.clock()))
        self.x(f"UPDATE tracking_tasks SET {', '.join(sets)} WHERE id=?", (*args, task_id))
        if user_id:
            self.audit(user_id, "task.update", task_id, {k: v for k, v in changes.items() if k != "last_result"})
        return self.get_task(task_id, None)

    def delete_task(self, task_id: str, user_id: str) -> bool:
        if not self.get_task(task_id, user_id):
            return False
        self.x("DELETE FROM tracking_tasks WHERE id=? AND user_id=?", (task_id, user_id))
        self.audit(user_id, "task.delete", task_id)
        return True

    def due_tasks(self, now: datetime) -> list[dict]:
        return [self._task_row(r) for r in self.q(
            "SELECT * FROM tracking_tasks WHERE status='ACTIVE' AND (next_run_at IS NULL OR next_run_at<=?)", (iso(now),))]

    # ---- observations & passage audit ----------------------------------------------------
    def save_observations(self, obs: list) -> int:
        n = 0
        for o in obs:
            cur = self.x("""INSERT OR IGNORE INTO observations(trace_id,train_number,service_date,kind,station_code,event_time,
                  km_after,source_id,source_time,retrieved_at,delay_min,is_demo,payload) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                         (o.trace_id, o.train_number, o.service_date.isoformat(), o.kind.value, o.station_code,
                          iso(o.event_time), o.km_after, o.source_id, iso(o.source_time), iso(o.retrieved_at),
                          o.delay_min, int(o.is_demo), _j(o.payload)))
            n += cur.rowcount
        return n

    def record_passage(self, task_id: str | None, res) -> int | None:
        """Append a passage evaluation if it differs from the latest one (audit history of corrections)."""
        d = res.to_dict()
        prev = self.q("""SELECT * FROM passage_events WHERE train_number=? AND service_date=? AND station_code=?
                         AND IFNULL(task_id,'')=IFNULL(?, '') ORDER BY id DESC LIMIT 1""",
                      (d["train_number"], d["service_date"], d["station_code"], task_id))
        sig = (d["event_class"], d["verification"], d["crossed"], d["event_time"], d["est_central"])
        if prev:
            p = prev[0]
            if (p["event_class"], p["verification"], None if p["crossed"] is None else bool(p["crossed"]),
                    p["event_time"], p["est_central"]) == sig:
                return None
        cur = self.x("""INSERT INTO passage_events(task_id,train_number,service_date,station_code,event_class,verification,
              crossed,event_time,est_earliest,est_latest,est_central,confidence,method,method_version,source_id,source_time,
              is_demo,created_at,supersedes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (task_id, d["train_number"], d["service_date"], d["station_code"], d["event_class"], d["verification"],
                      None if d["crossed"] is None else int(d["crossed"]), d["event_time"], d["est_earliest"],
                      d["est_latest"], d["est_central"], d["confidence"], d["method"], d["method_version"],
                      d["source_id"], d["source_time"], int(d["is_demo"]), iso(self.clock()),
                      prev[0]["id"] if prev else None))
        return cur.lastrowid

    def passage_history(self, train_number: str, service_date: str, station_code: str) -> list[dict]:
        return self.q("""SELECT * FROM passage_events WHERE train_number=? AND service_date=? AND station_code=?
                         ORDER BY id""", (train_number, service_date, station_code))

    # ---- notifications -------------------------------------------------------------------
    def add_notification(self, user_id: str, task_id: str | None, ntype: str, dedupe_key: str,
                         title: str, body: str, payload: dict, is_demo: bool) -> int | None:
        """Returns id, or None when the dedupe key was already used (duplicate suppressed)."""
        try:
            cur = self.x("""INSERT INTO notifications(user_id,task_id,type,dedupe_key,title,body,payload,is_demo,created_at)
                            VALUES (?,?,?,?,?,?,?,?,?)""",
                         (user_id, task_id, ntype, dedupe_key, title, body, _j(payload), int(is_demo), iso(self.clock())))
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None

    def list_notifications(self, user_id: str, limit: int = 50, after_id: int = 0) -> list[dict]:
        rows = self.q("SELECT * FROM notifications WHERE user_id=? AND id>? ORDER BY id DESC LIMIT ?",
                      (user_id, after_id, limit))
        for r in rows:
            r["payload"] = json.loads(r["payload"] or "{}")
            r["is_demo"] = bool(r["is_demo"])
        return rows

    def add_delivery(self, notification_id: int, destination: str, status: str, detail: str = ""):
        self.x("INSERT INTO delivery_attempts(notification_id,destination,status,detail,created_at) VALUES (?,?,?,?,?)",
               (notification_id, destination, status, detail[:500], iso(self.clock())))

    def deliveries(self, notification_id: int) -> list[dict]:
        return self.q("SELECT * FROM delivery_attempts WHERE notification_id=? ORDER BY id", (notification_id,))

    # ---- WhatsApp links, consent, replay guard --------------------------------------------
    def record_consent(self, subject: str, channel: str, action: str, evidence: str = ""):
        self.x("INSERT INTO consent_records(subject_hash,channel,action,evidence,created_at) VALUES (?,?,?,?,?)",
               (self.hash_subject(subject), channel, action, evidence[:200], iso(self.clock())))

    def consent_state(self, subject: str, channel: str) -> str | None:
        r = self.q("SELECT action FROM consent_records WHERE subject_hash=? AND channel=? ORDER BY id DESC LIMIT 1",
                   (self.hash_subject(subject), channel))
        return r[0]["action"] if r else None

    def wa_upsert(self, phone: str, opted_in: bool | None = None, inbound: bool = False, user_id: str | None = None):
        h = self.hash_subject(phone)
        now = iso(self.clock())
        ex = self.q("SELECT * FROM wa_links WHERE phone_hash=?", (h,))
        if not ex:
            self.x("INSERT INTO wa_links VALUES (?,?,?,?,?,?)",
                   (h, phone, user_id, int(bool(opted_in)), now if inbound else None, now))
        else:
            if opted_in is not None:
                self.x("UPDATE wa_links SET opted_in=? WHERE phone_hash=?", (int(opted_in), h))
            if inbound:
                self.x("UPDATE wa_links SET last_inbound_at=? WHERE phone_hash=?", (now, h))
            if user_id:
                self.x("UPDATE wa_links SET user_id=? WHERE phone_hash=?", (user_id, h))
        return self.q("SELECT * FROM wa_links WHERE phone_hash=?", (h,))[0]

    def wa_for_user(self, user_id: str) -> dict | None:
        r = self.q("SELECT * FROM wa_links WHERE user_id=? AND opted_in=1", (user_id,))
        return r[0] if r else None

    def wa_get(self, phone: str) -> dict | None:
        r = self.q("SELECT * FROM wa_links WHERE phone_hash=?", (self.hash_subject(phone),))
        return r[0] if r else None

    def wa_forget(self, phone: str):
        """Right-to-delete: remove the number; keep only a hashed opt-out consent record."""
        self.x("DELETE FROM wa_links WHERE phone_hash=?", (self.hash_subject(phone),))

    def tg_user(self, chat_id: str) -> str:
        r = self.q("SELECT user_id FROM telegram_links WHERE chat_id=?", (str(chat_id),))
        if r:
            return r[0]["user_id"]
        uid, _ = self.create_user(f"telegram {str(chat_id)[-4:]}")
        self.x("INSERT INTO telegram_links VALUES (?,?,?)", (str(chat_id), uid, iso(self.clock())))
        return uid

    def tg_chat_for_user(self, user_id: str) -> str | None:
        r = self.q("SELECT chat_id FROM telegram_links WHERE user_id=? ORDER BY created_at DESC LIMIT 1", (user_id,))
        return r[0]["chat_id"] if r else None

    def new_link_code(self, user_id: str, ttl_min: int = 15) -> str:
        from datetime import timedelta
        code = f"{secrets.randbelow(10**6):06d}"
        self.x("INSERT OR REPLACE INTO link_codes VALUES (?,?,?)",
               (code, user_id, iso(self.clock() + timedelta(minutes=ttl_min))))
        return code

    def consume_link_code(self, code: str) -> str | None:
        r = self.q("SELECT * FROM link_codes WHERE code=? AND expires_at>?", (code, iso(self.clock())))
        self.x("DELETE FROM link_codes WHERE code=?", (code,))
        return r[0]["user_id"] if r else None

    def seen_message(self, msg_id: str) -> bool:
        """True if this inbound message id was already processed (replay / webhook retry)."""
        try:
            self.x("INSERT INTO processed_messages VALUES (?,?)", (msg_id, iso(self.clock())))
            return False
        except sqlite3.IntegrityError:
            return True

    # ---- chat context & channel queue -----------------------------------------------------
    def get_context(self, session_id: str) -> dict:
        r = self.q("SELECT context FROM chat_context WHERE session_id=?", (session_id,))
        return json.loads(r[0]["context"]) if r else {}

    def set_context(self, session_id: str, user_id: str | None, ctx: dict):
        self.x("INSERT OR REPLACE INTO chat_context VALUES (?,?,?,?)", (session_id, user_id, _j(ctx), iso(self.clock())))

    def queue_channel_post(self, channel: str, text: str, task_id: str | None = None) -> int:
        return self.x("INSERT INTO channel_queue(channel,text,task_id,created_at) VALUES (?,?,?,?)",
                      (channel, text, task_id, iso(self.clock()))).lastrowid

    def channel_queue(self, pending_only: bool = True) -> list[dict]:
        return self.q("SELECT * FROM channel_queue" + (" WHERE published_at IS NULL" if pending_only else "") + " ORDER BY id DESC")

    def mark_channel_published(self, post_id: int):
        self.x("UPDATE channel_queue SET published_at=? WHERE id=?", (iso(self.clock()), post_id))

    def stats(self) -> dict[str, Any]:
        return {t: self.q(f"SELECT COUNT(*) n FROM {t}")[0]["n"] for t in
                ("tracking_tasks", "observations", "passage_events", "notifications", "delivery_attempts")}
