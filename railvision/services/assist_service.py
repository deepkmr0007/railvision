"""Server side of the browser assistant.

watch list (≤10 trains/user) ─► /api/assist/work ─► agent on your PC opens the official page
                                                   ─► /api/assist/captures ─► parse ─► observations
                                                   ─► crossing engine / tracking tasks / alerts / WhatsApp
"""
from __future__ import annotations

import base64
import json
import re
import secrets
from dataclasses import asdict
from datetime import date, datetime, timedelta

from ..core.models import LiveObservation, ObsKind, Route, ScheduledStop, VacancySnapshot
from ..core.status import build_status
from ..core.timeutil import fmt_hm, iso, now_ist, parse_iso
from ..parsers import irctc, ntes
from ..providers.base import NotSupported, ProviderAdapter, ProviderError
from ..assist.recipes import DEFAULT_RECIPES, IRCTC_CHARTS_URL, NTES_URL, Recipe

SOURCE_ID = "ntes-official-page (browser assistant)"
BERTH_SOURCE_ID = "irctc-online-charts (browser assistant)"
MAX_WATCH = 10
MIN_INTERVAL, MAX_INTERVAL = 5, 240
LEASE_MIN = 12

SCHEMA = """
CREATE TABLE IF NOT EXISTS assist_watch (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL, train TEXT NOT NULL, start_date TEXT NOT NULL,
  ref_station TEXT, end_station TEXT, interval_min INTEGER NOT NULL, berth_checks TEXT NOT NULL DEFAULT '[]',
  notify TEXT NOT NULL DEFAULT 'on_change', destinations TEXT NOT NULL DEFAULT '["app"]', active INTEGER NOT NULL DEFAULT 1,
  task_id TEXT, label TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, next_due_at TEXT, lease_until TEXT,
  last_fetch_at TEXT, last_status TEXT, last_reason TEXT, last_capture_id INTEGER, last_headline TEXT, last_sig TEXT);
CREATE TABLE IF NOT EXISTS assist_captures (
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, watch_id TEXT, job_id INTEGER, recipe TEXT, kind TEXT,
  vars TEXT, status TEXT, reason TEXT, url TEXT, title TEXT, text TEXT, tables TEXT, json_count INTEGER,
  screenshot BLOB, started_at TEXT, finished_at TEXT, parse TEXT, confidence REAL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_cap_train ON assist_captures(kind, status, created_at);
CREATE TABLE IF NOT EXISTS assist_recipes (name TEXT PRIMARY KEY, recipe TEXT NOT NULL, verified INTEGER NOT NULL DEFAULT 0, updated_at TEXT);
CREATE TABLE IF NOT EXISTS assist_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, kind TEXT NOT NULL, recipe TEXT NOT NULL, vars TEXT NOT NULL,
  status TEXT NOT NULL, created_at TEXT NOT NULL, claimed_at TEXT, capture_id INTEGER, summary TEXT);
CREATE TABLE IF NOT EXISTS assist_agents (user_id TEXT PRIMARY KEY, last_seen TEXT, info TEXT, state TEXT, state_reason TEXT, state_at TEXT);
"""


class NoCaptureYet(ProviderError):
    def __init__(self, train: str, sd: date):
        super().__init__(f"The browser assistant has not checked {train} for {sd:%d %b} yet.")
        self.train, self.sd = train, sd


def route_to_json(r: Route) -> dict:
    return {"train_number": r.train_number, "train_name": r.train_name, "source_id": r.source_id,
            "complete": r.complete, "stops": [asdict(s) for s in r.stops]}


def route_from_json(d: dict) -> Route:
    r = Route(d["train_number"], d["train_name"], [ScheduledStop(**s) for s in d["stops"]], d["source_id"])
    r.complete = d.get("complete", True)
    return r


def obs_from_json(d: dict) -> LiveObservation:
    return LiveObservation(train_number=d["train_number"], service_date=date.fromisoformat(d["service_date"]),
                           kind=ObsKind(d["kind"]), station_code=d["station_code"], event_time=parse_iso(d["event_time"]),
                           source_id=d["source_id"], source_time=parse_iso(d["source_time"]),
                           retrieved_at=parse_iso(d["retrieved_at"]), delay_min=d.get("delay_min"),
                           km_after=d.get("km_after"), payload=d.get("payload") or {}, trace_id=d.get("trace_id") or "")


class AssistProvider(ProviderAdapter):
    """Serves the latest successfully parsed official-page capture. Never fetches by itself."""
    source_id = SOURCE_ID
    display_name = "Official pages via your browser assistant"
    official = True
    capabilities = {"train_status": "VERIFIED AND IMPLEMENTABLE NOW (personal browser assistant)",
                    "chart_vacancy": "VERIFIED AND IMPLEMENTABLE NOW (personal browser assistant)"}

    def __init__(self, svc: "AssistService"):
        self.svc = svc

    def _latest(self, kind: str, match: dict):
        rows = self.svc.store.q("SELECT id, parse, finished_at FROM assist_captures WHERE kind=? AND status='ok' "
                                "AND confidence IS NOT NULL ORDER BY id DESC LIMIT 200", (kind,))
        for r in rows:
            p = json.loads(r["parse"] or "{}")
            if all(str(p.get(k, "")).upper() == str(v).upper() for k, v in match.items()):
                return r, p
        return None, None

    def get_train_route(self, train_number, service_date):
        _, p = self._latest("status", {"train": train_number, "start_date": service_date.isoformat()})
        if not p or not p.get("route"):
            raise NoCaptureYet(train_number, service_date)
        return route_from_json(p["route"])

    def get_train_status(self, train_number, service_date):
        _, p = self._latest("status", {"train": train_number, "start_date": service_date.isoformat()})
        if not p or not p.get("route"):
            raise NoCaptureYet(train_number, service_date)
        return [obs_from_json(o) for o in p.get("obs", [])]

    def get_chart_vacancy(self, train_number, journey_date, boarding_station, travel_class):
        row, p = self._latest("berth", {"train": train_number, "date": journey_date.isoformat(),
                                        "station": boarding_station, "class": travel_class})
        if not p:
            raise NotSupported("No vacant-berth check from your browser assistant yet for this train/date/class.",
                               IRCTC_CHARTS_URL)
        v = p["vacancy"]
        coaches: dict[str, list] = {}
        for b in v["vacant"]:
            coaches.setdefault(b["coach"], []).append({"no": b["berth"], "type": b.get("type") or "",
                                                       "segments": [{"from": b.get("from"), "to": b.get("to"), "occupied": False}]})
        return VacancySnapshot(train_number=train_number, journey_date=journey_date, boarding_station=boarding_station,
                               travel_class=travel_class, chart_no=v.get("chart_no"), source_id=BERTH_SOURCE_ID,
                               source_time=parse_iso(row["finished_at"]), coaches=[{"coach": k, "berths": bs} for k, bs in coaches.items()],
                               official_url=IRCTC_CHARTS_URL,
                               notes=["Only vacant entries listed on the official chart page are shown; other berths are not "
                                      "known to be occupied.", f"Chart prepared: {v.get('chart_prepared')}"])

    def search_trains(self, query):
        rows = self.svc.store.q("SELECT parse FROM assist_captures WHERE kind='search' AND status='ok' ORDER BY id DESC LIMIT 30")
        for r in rows:
            p = json.loads(r["parse"] or "{}")
            if p.get("query", "").lower() == query.lower():
                return p.get("results", [])
        raise NotSupported("Not searched yet — ask the assistant to search the official site.", NTES_URL)

    def get_source_health(self):
        return self.svc.source_health()


class AssistService:
    def __init__(self, store, tracker, dispatcher, clock=now_ist, app_url: str = "", log=print):
        self.store, self.tracker, self.dispatcher, self.clock, self.app_url, self.log = store, tracker, dispatcher, clock, app_url, log
        with store.lock:
            store.db.executescript(SCHEMA)
        self.provider = AssistProvider(self)
        # sent to the agent each round; the agent never goes below its own 15 s floor in real use
        self.pacing = {"min_gap_s": 20, "jitter_s": 10, "max_pages_per_day": 400, "poll_s": 15}

    # ------------------------------------------------------------------ helpers
    def _j(self, v):
        return json.dumps(v, ensure_ascii=False, default=str)

    def _watch_row(self, r):
        r = dict(r)
        r["berth_checks"] = json.loads(r["berth_checks"] or "[]")
        r["destinations"] = json.loads(r["destinations"] or '["app"]')
        r["active"] = bool(r["active"])
        return r

    def recipes(self) -> dict[str, dict]:
        out = {k: v.to_dict() for k, v in DEFAULT_RECIPES.items()}
        for r in self.store.q("SELECT name, recipe, verified FROM assist_recipes"):
            d = json.loads(r["recipe"])
            d["verified"] = bool(r["verified"])
            out[r["name"]] = d
        return out

    def save_recipe(self, name: str, recipe: dict) -> dict:
        if not re.fullmatch(r"[a-z0-9_]{3,40}", name):
            raise ValueError("recipe name: 3-40 chars a-z 0-9 _")
        rc = Recipe.from_dict({**recipe, "name": name})
        if not rc.start_url.startswith("https://") and not rc.start_url.startswith("http://127.0.0.1") and not rc.start_url.startswith("http://localhost"):
            raise ValueError("recipe start_url must be https")
        self.store.x("INSERT OR REPLACE INTO assist_recipes VALUES (?,?,?,?)", (name, self._j(rc.to_dict()), 0, iso(self.clock())))
        return rc.to_dict()

    # ------------------------------------------------------------------ watch list
    def _validate_watch(self, b: dict, partial=False) -> dict:
        out = {}
        if "train" in b or not partial:
            t = str(b.get("train", "")).strip()
            if not re.fullmatch(r"\d{5}", t):
                raise ValueError("train must be a 5-digit number")
            out["train"] = t
        if "start_date" in b or not partial:
            try:
                d = date.fromisoformat(str(b.get("start_date")))
            except ValueError:
                raise ValueError("start_date must be YYYY-MM-DD (the date the train starts from its origin)")
            today = self.clock().date()
            if not (today - timedelta(days=4) <= d <= today + timedelta(days=3)):
                raise ValueError("start_date must be within the last 4 days or next 3 days (live status window)")
            out["start_date"] = d.isoformat()
        if "interval_min" in b or not partial:
            iv = int(b.get("interval_min", 15))
            if not MIN_INTERVAL <= iv <= MAX_INTERVAL:
                raise ValueError(f"interval must be {MIN_INTERVAL}-{MAX_INTERVAL} minutes")
            out["interval_min"] = iv
        for k in ("ref_station", "end_station"):
            if k in b:
                v = (b.get(k) or "").strip().upper() or None
                if v and not re.fullmatch(r"[A-Z]{1,5}", v):
                    raise ValueError(f"{k} must be a station code like RE or NDLS")
                out[k] = v
        if "berth_checks" in b:
            checks = []
            for c in (b.get("berth_checks") or [])[:3]:
                st, cl = str(c.get("station", "")).upper().strip(), str(c.get("class", "")).upper().strip()
                if not re.fullmatch(r"[A-Z]{1,5}", st) or cl not in ("1A", "2A", "3A", "3E", "SL", "CC", "EC", "2S", "EA", "FC"):
                    raise ValueError("berth check needs a station code and a class (1A/2A/3A/3E/SL/CC/EC/2S)")
                jd = c.get("journey_date") or b.get("start_date")
                checks.append({"station": st, "class": cl, "journey_date": str(jd) if jd else None})
            out["berth_checks"] = checks
        if "notify" in b:
            if b["notify"] not in ("on_change", "every", "alerts_only"):
                raise ValueError("notify must be on_change, every or alerts_only")
            out["notify"] = b["notify"]
        if "destinations" in b:
            ds = [d for d in (b.get("destinations") or ["app"]) if d in ("app", "browser", "android_push", "email", "whatsapp",
                                                                         "whatsapp_group", "whatsapp_channel", "telegram")]
            out["destinations"] = list(dict.fromkeys(["app"] + ds))
        if "label" in b:
            out["label"] = str(b.get("label") or "")[:60]
        if "active" in b:
            out["active"] = bool(b["active"])
        return out

    def add_watch(self, user_id: str, b: dict) -> dict:
        f = self._validate_watch(b)
        n = self.store.q("SELECT COUNT(*) n FROM assist_watch WHERE user_id=? AND active=1", (user_id,))[0]["n"]
        if n >= MAX_WATCH:
            raise ValueError(f"watch list is full ({MAX_WATCH} trains). Remove or pause one first.")
        dup = self.store.q("SELECT id FROM assist_watch WHERE user_id=? AND train=? AND start_date=? AND active=1",
                           (user_id, f["train"], f["start_date"]))
        if dup:
            raise ValueError("this train and start date is already on your watch list")
        wid = "w_" + secrets.token_hex(5)
        now = iso(self.clock())
        self.store.x("""INSERT INTO assist_watch(id,user_id,train,start_date,ref_station,end_station,interval_min,berth_checks,
                     notify,destinations,active,label,created_at,updated_at,next_due_at) VALUES (?,?,?,?,?,?,?,?,?,?,1,?,?,?,?)""",
                     (wid, user_id, f["train"], f["start_date"], f.get("ref_station"), f.get("end_station"), f["interval_min"],
                      self._j(f.get("berth_checks", [])), f.get("notify", "on_change"), self._j(f.get("destinations", ["app"])),
                      f.get("label", ""), now, now, now))
        self.store.audit(user_id, "assist.watch.add", wid, f)
        return self.get_watch(user_id, wid)

    def get_watch(self, user_id: str, wid: str) -> dict | None:
        r = self.store.q("SELECT * FROM assist_watch WHERE id=? AND user_id=?", (wid, user_id))
        return self._watch_row(r[0]) if r else None

    def list_watch(self, user_id: str) -> list[dict]:
        return [self._watch_row(r) for r in self.store.q("SELECT * FROM assist_watch WHERE user_id=? ORDER BY created_at", (user_id,))]

    def update_watch(self, user_id: str, wid: str, b: dict) -> dict | None:
        if not self.get_watch(user_id, wid):
            return None
        f = self._validate_watch(b, partial=True)
        if f.get("active") is True:
            n = self.store.q("SELECT COUNT(*) n FROM assist_watch WHERE user_id=? AND active=1 AND id<>?", (user_id, wid))[0]["n"]
            if n >= MAX_WATCH:
                raise ValueError("watch list is full")
        sets, args = [], []
        for k, v in f.items():
            sets.append(f"{k}=?")
            args.append(self._j(v) if k in ("berth_checks", "destinations") else (int(v) if isinstance(v, bool) else v))
        sets += ["updated_at=?"]
        args += [iso(self.clock())]
        self.store.x(f"UPDATE assist_watch SET {', '.join(sets)} WHERE id=? AND user_id=?", (*args, wid, user_id))
        w = self.get_watch(user_id, wid)
        if w.get("task_id"):
            t = self.store.get_task(w["task_id"], user_id)
            if t and t["status"] in ("ACTIVE", "PAUSED"):
                ch = {"interval_min": w["interval_min"], "destinations": w["destinations"]}
                if not w["active"]:
                    ch["status"] = "PAUSED"
                elif t["status"] == "PAUSED":
                    ch["status"] = "ACTIVE"
                self.store.update_task(w["task_id"], user_id, **ch)
        return w

    def check_now(self, user_id: str, wid: str) -> dict | None:
        if not self.get_watch(user_id, wid):
            return None
        self.store.x("UPDATE assist_watch SET next_due_at=?, lease_until=NULL WHERE id=?", (iso(self.clock()), wid))
        return self.get_watch(user_id, wid)

    def delete_watch(self, user_id: str, wid: str) -> bool:
        w = self.get_watch(user_id, wid)
        if not w:
            return False
        if w.get("task_id"):
            t = self.store.get_task(w["task_id"], user_id)
            if t and t["status"] in ("ACTIVE", "PAUSED", "PENDING_CONFIRMATION"):
                self.store.update_task(w["task_id"], user_id, status="CANCELLED")
        self.store.x("DELETE FROM assist_watch WHERE id=? AND user_id=?", (wid, user_id))
        self.store.audit(user_id, "assist.watch.delete", wid)
        return True

    # ------------------------------------------------------------------ jobs (on-demand)
    def queue_job(self, user_id: str, kind: str, vars_: dict) -> dict:
        recipe = {"status": "ntes_status", "search": "ntes_search", "berth": "irctc_berth"}.get(kind)
        if not recipe:
            raise ValueError("kind must be status, search or berth")
        pending = self.store.q("SELECT COUNT(*) n FROM assist_jobs WHERE user_id=? AND status IN ('queued','running')", (user_id,))[0]["n"]
        if pending >= 10:
            raise ValueError("10 checks are already waiting; try again shortly")
        same = self.store.q("SELECT * FROM assist_jobs WHERE user_id=? AND kind=? AND vars=? AND status IN ('queued','running')",
                            (user_id, kind, self._j(vars_)))
        if same:
            return same[0]
        jid = self.store.x("INSERT INTO assist_jobs(user_id,kind,recipe,vars,status,created_at) VALUES (?,?,?,?,?,?)",
                           (user_id, kind, recipe, self._j(vars_), "queued", iso(self.clock()))).lastrowid
        return self.store.q("SELECT * FROM assist_jobs WHERE id=?", (jid,))[0]

    def list_jobs(self, user_id: str, limit=20) -> list[dict]:
        return self.store.q("SELECT * FROM assist_jobs WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, limit))

    # ------------------------------------------------------------------ agent protocol
    def heartbeat(self, user_id: str, info: dict | None = None, state: str | None = None, reason: str = ""):
        now = iso(self.clock())
        ex = self.store.q("SELECT * FROM assist_agents WHERE user_id=?", (user_id,))
        if not ex:
            self.store.x("INSERT INTO assist_agents VALUES (?,?,?,?,?,?)", (user_id, now, self._j(info or {}), state or "online", reason, now))
        else:
            self.store.x("UPDATE assist_agents SET last_seen=?, info=COALESCE(?,info) WHERE user_id=?",
                         (now, self._j(info) if info else None, user_id))
            if state and state != ex[0]["state"]:
                self.store.x("UPDATE assist_agents SET state=?, state_reason=?, state_at=? WHERE user_id=?", (state, reason[:300], now, user_id))
                if state == "needs_you":
                    self._notify(user_id, None, "ASSIST_NEEDS_YOU", f"needsyou:{now[:13]}",
                                 "Browser assistant needs you",
                                 f"⚠️ {reason or 'The official site is asking for a human check.'}\n"
                                 "Open the browser window on your computer to continue. RAILVISION never solves these checks.",
                                 ["app", "whatsapp", "telegram"])

    def agent_state(self, user_id: str) -> dict:
        r = self.store.q("SELECT * FROM assist_agents WHERE user_id=?", (user_id,))
        if not r:
            return {"online": False, "state": "never_connected", "last_seen": None}
        r = r[0]
        age = (self.clock() - parse_iso(r["last_seen"])).total_seconds() if r["last_seen"] else None
        return {"online": age is not None and age < 120, "state": r["state"], "reason": r["state_reason"],
                "last_seen": r["last_seen"], "info": json.loads(r["info"] or "{}")}

    def work(self, user_id: str, info: dict | None = None) -> dict:
        self.heartbeat(user_id, info, "online")
        now = self.clock()
        items = []
        rows = self.store.q("""SELECT * FROM assist_watch WHERE user_id=? AND active=1 AND (next_due_at IS NULL OR next_due_at<=?)
                               AND (lease_until IS NULL OR lease_until<=?) ORDER BY next_due_at LIMIT ?""",
                            (user_id, iso(now), iso(now), MAX_WATCH))
        for r in rows:
            w = self._watch_row(r)
            jobs = [{"recipe": "ntes_status", "kind": "status", "vars": {"train": w["train"], "date": w["start_date"]}}]
            for c in w["berth_checks"]:
                jobs.append({"recipe": "irctc_berth", "kind": "berth",
                             "vars": {"train": w["train"], "date": c.get("journey_date") or w["start_date"],
                                      "station": c["station"], "class": c["class"]}})
            items.append({"watch_id": w["id"], "train": w["train"], "jobs": jobs})
            self.store.x("UPDATE assist_watch SET lease_until=? WHERE id=?", (iso(now + timedelta(minutes=LEASE_MIN)), w["id"]))
        jobs = []
        for j in self.store.q("SELECT * FROM assist_jobs WHERE user_id=? AND (status='queued' OR (status='running' AND claimed_at<=?)) "
                              "ORDER BY id LIMIT 5", (user_id, iso(now - timedelta(minutes=LEASE_MIN)))):
            jobs.append({"job_id": j["id"], "recipe": j["recipe"], "kind": j["kind"], "vars": json.loads(j["vars"])})
            self.store.x("UPDATE assist_jobs SET status='running', claimed_at=? WHERE id=?", (iso(now), j["id"]))
        return {"watch": items, "jobs": jobs, "recipes": self.recipes(),
                "pacing": dict(self.pacing),
                "server_time": iso(now)}

    # ------------------------------------------------------------------ ingest
    def ingest(self, user_id: str, c: dict) -> dict:
        kind = c.get("kind")
        if kind not in ("status", "berth", "search"):
            raise ValueError("kind must be status, berth or search")
        wid, jid = c.get("watch_id"), c.get("job_id")
        w = self.get_watch(user_id, wid) if wid else None
        if wid and not w:
            raise PermissionError("watch not found")
        job = None
        if jid is not None:
            r = self.store.q("SELECT * FROM assist_jobs WHERE id=? AND user_id=?", (int(jid), user_id))
            if not r:
                raise PermissionError("job not found")
            job = r[0]
        vars_ = {k: str(v) for k, v in (c.get("vars") or {}).items()}
        status = c.get("status") if c.get("status") in ("ok", "no_result", "captcha", "login", "blocked", "recipe_failed", "error") else "error"
        shot = None
        if c.get("screenshot_b64"):
            try:
                shot = base64.b64decode(c["screenshot_b64"], validate=True)[:2_500_000]
            except (ValueError, TypeError):
                shot = None
        parse: dict = {"train": vars_.get("train"), "start_date": vars_.get("date"), "date": vars_.get("date"),
                       "station": vars_.get("station"), "class": vars_.get("class"), "query": vars_.get("query")}
        confidence = None
        headline = c.get("reason") or status
        if status == "ok" or (status == "no_result" and c.get("text")):
            if kind == "status":
                ps = ntes.parse_status(c, vars_.get("train"))
                route, obs, issues = (None, [], ["no start date"]) if not vars_.get("date") else \
                    ntes.to_route_and_obs(ps, vars_["train"], date.fromisoformat(vars_["date"]), c, SOURCE_ID)
                confidence = ps.confidence
                parse.update({"confidence": ps.confidence, "issues": issues, "headline": ps.headline,
                              "train_name": ps.train_name, "last_updated": iso(ps.last_updated),
                              "cancelled": ps.cancelled, "diverted": ps.diverted, "not_started": ps.not_started,
                              "stations_found": len(ps.stations), "route": route_to_json(route) if route else None,
                              "obs": [o.to_dict() for o in obs], "parser": ps.method})
                if route:
                    self.store.save_observations(obs)
                headline = ps.headline or (f"{len(ps.stations)} stations read" if ps.stations else "page read, no station list found")
            elif kind == "berth":
                v = irctc.parse_vacancy(c, vars_.get("station"), vars_.get("class"))
                confidence = v["confidence"]
                parse["vacancy"] = v
                headline = ("Chart not prepared yet" if v["chart_prepared"] is False else
                            f"{v['total_vacant']} vacant berth entries ({v['from_boarding']} from {vars_.get('station')})")
            else:
                s = irctc.parse_search(c, vars_.get("query", ""))
                confidence = s["confidence"]
                parse.update(s)
                headline = f"{len(s['results'])} trains found for '{vars_.get('query')}'"
        cid = self.store.x("""INSERT INTO assist_captures(user_id,watch_id,job_id,recipe,kind,vars,status,reason,url,title,text,tables,
                 json_count,screenshot,started_at,finished_at,parse,confidence,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                           (user_id, wid, jid, c.get("recipe"), kind, self._j(vars_), status, (c.get("reason") or "")[:500],
                            (c.get("url") or "")[:500], (c.get("title") or "")[:200], (c.get("text") or "")[:200_000],
                            self._j((c.get("tables") or [])[:25]), len(c.get("json") or []), shot, c.get("started_at"),
                            c.get("finished_at") or iso(self.clock()), self._j(parse), confidence, iso(self.clock()))).lastrowid
        self._prune(wid)
        if status == "ok" and (confidence or 0) >= 0.6:      # a recorded recipe that produced a confident read
            self.store.x("UPDATE assist_recipes SET verified=1 WHERE name=?", (c.get("recipe"),))
        result = {"capture_id": cid, "status": status, "headline": headline, "confidence": confidence}
        if w:
            result.update(self._after_watch(user_id, w, kind, status, cid, parse, headline))
        if job:
            self.store.x("UPDATE assist_jobs SET status=?, capture_id=?, summary=? WHERE id=?",
                         ("done" if status == "ok" else "failed", cid, headline[:300], job["id"]))
            self._job_notification(user_id, job, kind, status, cid, parse, headline, c.get("reason") or "")
        return result

    def _prune(self, wid):
        if wid:
            self.store.x("""DELETE FROM assist_captures WHERE watch_id=? AND id NOT IN
                            (SELECT id FROM assist_captures WHERE watch_id=? ORDER BY id DESC LIMIT 60)""", (wid, wid))

    # ------------------------------------------------------------------ after a watch capture
    def _after_watch(self, user_id, w, kind, status, cid, parse, headline) -> dict:
        now = self.clock()
        out = {}
        if kind == "status":
            nxt = now + timedelta(minutes=w["interval_min"])
            if status in ("captcha", "blocked", "login"):
                nxt = now + timedelta(minutes=max(30, w["interval_min"]))
            self.store.x("""UPDATE assist_watch SET last_fetch_at=?, last_status=?, last_reason=?, last_capture_id=?, last_headline=?,
                            next_due_at=?, lease_until=NULL, updated_at=? WHERE id=?""",
                         (iso(now), status, (parse.get("issues") or [""])[0] if status == "ok" else headline, cid, headline[:300],
                          iso(nxt), iso(now), w["id"]))
            if status != "ok":
                if status in ("captcha", "blocked", "login", "recipe_failed"):
                    self._notify(user_id, None, "ASSIST_PROBLEM", f"{w['id']}:{status}:{now:%Y%m%d%H}",
                                 f"{w['train']} · official page check failed",
                                 f"⚠️ {w['train']}: {headline}\nNext try {nxt:%H:%M} IST.", w["destinations"])
                return out
            route = route_from_json(parse["route"]) if parse.get("route") else None
            if not route:
                return out
            sd = date.fromisoformat(w["start_date"])
            obs = [obs_from_json(o) for o in parse.get("obs", [])]
            st = build_status(route, obs, sd, now)
            out["state"] = st["state"]
            # tracking task for the reference station (crossing / delay / completion alerts)
            if w.get("ref_station"):
                out["task"] = self._ensure_task(user_id, w, route, sd)
                if out["task"]:
                    self.store.update_task(out["task"], None, next_run_at=iso(now))
                    try:
                        self.tracker.tick()
                    except Exception as e:
                        self.log(f"[assist] tracker tick error: {e!r}")
            # change-based running-status report
            sig = f"{st['state']}|{(st['last_event'] or {}).get('station')}|{(st['last_event'] or {}).get('kind')}|{st['delay_min']}|{parse.get('confidence', 0) >= 0.6}"
            changed = sig != (w.get("last_sig") or "")
            self.store.x("UPDATE assist_watch SET last_sig=? WHERE id=?", (sig, w["id"]))
            if w["notify"] == "every" or (w["notify"] == "on_change" and changed and not w.get("ref_station")):
                self._notify(user_id, None, "STATUS_REPORT", f"{w['id']}:rep:{cid}",
                             f"{w['train']} · {st['state'].replace('_', ' ').title()}", self.status_text(w, route, st, parse),
                             w["destinations"])
        elif kind == "berth" and status == "ok":
            v = parse.get("vacancy") or {}
            key = f"{parse.get('station')}|{parse.get('class')}"
            prev = self.store.q("""SELECT parse FROM assist_captures WHERE watch_id=? AND kind='berth' AND status='ok' AND id<>?
                                   ORDER BY id DESC LIMIT 5""", (w["id"], cid))
            prev_total = None
            for p in prev:
                pp = json.loads(p["parse"])
                if f"{pp.get('station')}|{pp.get('class')}" == key:
                    prev_total = (pp.get("vacancy") or {}).get("total_vacant")
                    break
            if prev_total != v.get("total_vacant") or v.get("chart_prepared") is False and prev_total is None:
                self._notify(user_id, None, "VACANCY_CHANGE", f"{w['id']}:berth:{key}:{v.get('total_vacant')}:{v.get('chart_prepared')}",
                             f"{w['train']} · {parse.get('class')} vacant berths", self.berth_text(w, parse), w["destinations"])
        return out

    def _ensure_task(self, user_id, w, route: Route, sd: date) -> str | None:
        if w.get("task_id"):
            t = self.store.get_task(w["task_id"], user_id)
            if t and t["status"] in ("ACTIVE", "PAUSED"):
                return w["task_id"]
            if t and t["status"] in ("COMPLETED", "CANCELLED"):
                return None
        ref = w["ref_station"]
        if route.index_of(ref) is None and route.complete:
            self.store.x("UPDATE assist_watch SET last_reason=? WHERE id=?", (f"{ref} is not on this train's route", w["id"]))
            return None
        t = self.store.create_task(user_id, train_number=w["train"], service_date=sd.isoformat(), reference_station=ref,
                                   end_station=w.get("end_station"), interval_min=w["interval_min"],
                                   destinations=w["destinations"], status="ACTIVE", is_demo=False)
        self.store.x("UPDATE assist_watch SET task_id=? WHERE id=?", (t["id"], w["id"]))
        return t["id"]

    # ------------------------------------------------------------------ messages
    def status_text(self, w, route, st, parse) -> str:
        le = st.get("last_event")
        where = "No position reported on the page"
        if le:
            where = (f"{le['km_after']} km after {le['station']}" if le["kind"] == "POSITION" else
                     f"{le['kind'].title()} {le['station']} at {fmt_hm(parse_iso(le['time']))}")
        conf = parse.get("confidence") or 0
        lines = ["🚆 RAILVISION AI — RUNNING STATUS",
                 f"Train: {w['train']} {parse.get('train_name') or route.train_name or ''}".rstrip(),
                 f"Start date: {w['start_date']}",
                 f"Now: {where}" if conf >= 0.6 else f"Page says: {parse.get('headline') or 'see screenshot'}",
                 f"Delay: {('%+d min' % st['delay_min']) if st.get('delay_min') is not None else 'Unknown'}",
                 f"Next halt: {st.get('next_stop') or 'Unknown'}",
                 f"Source last updated: {fmt_hm(parse_iso(parse.get('last_updated'))) if parse.get('last_updated') else 'not shown'}",
                 f"Checked: {fmt_hm(self.clock())} IST · official NTES page via your browser assistant",
                 f"Reading confidence: {conf:.0%}"]
        if conf < 0.6:
            lines.append("⚠️ The page could not be read reliably — open the screenshot in the dashboard.")
        if self.app_url:
            lines.append(f"Dashboard: {self.app_url.rstrip('/')}/#watch")
        return "\n".join(lines)

    def berth_text(self, w, parse) -> str:
        v = parse.get("vacancy") or {}
        if v.get("chart_prepared") is False:
            body = "Chart not prepared yet (per the official page)."
        else:
            coaches = ", ".join(f"{k}: {n}" for k, n in sorted((v.get("by_coach") or {}).items())) or "none listed"
            body = (f"Vacant entries on chart: {v.get('total_vacant', 0)} (from {parse.get('station')}: {v.get('from_boarding', 0)})\n"
                    f"By coach: {coaches}")
        return (f"🛏️ RAILVISION AI — CHART VACANCY\nTrain: {w['train']} · {parse.get('class')} · from {parse.get('station')}\n"
                f"Journey date: {parse.get('date')}\n{body}\n"
                f"Checked: {fmt_hm(self.clock())} IST · IRCTC online charts via your browser assistant\n"
                "Chart vacancy ≠ bookable seat ≠ empty berth onboard. Book only through official IRCTC channels.")

    def _job_notification(self, user_id, job, kind, status, cid, parse, headline, reason):
        v = json.loads(job["vars"])
        if status != "ok":
            text = f"⚠️ Could not read the official page for {v.get('train') or v.get('query')}: {reason or status}"
        elif kind == "status":
            route = route_from_json(parse["route"]) if parse.get("route") else None
            if route:
                sd = date.fromisoformat(v["date"])
                st = build_status(route, [obs_from_json(o) for o in parse.get("obs", [])], sd, self.clock())
                text = self.status_text({"train": v["train"], "start_date": v["date"]}, route, st, parse)
            else:
                text = f"Checked NTES for {v['train']}: {headline}. Open the dashboard to see the page."
        elif kind == "search":
            rows = parse.get("results") or []
            text = (f"🔎 Trains matching '{v.get('query')}' (official site):\n" +
                    "\n".join(f"{r['number']} {r['name']}" for r in rows[:15])) if rows else f"No trains found for '{v.get('query')}'."
        else:
            text = self.berth_text({"train": v.get("train")}, parse)
        self._notify(user_id, None, "ASSIST_RESULT", f"job:{job['id']}", f"Official page check · {v.get('train') or v.get('query')}",
                     text, ["app", "whatsapp", "telegram"])

    def _notify(self, user_id, task, ntype, key, title, text, destinations):
        nid = self.store.add_notification(user_id, task, ntype, key, title, text, {"short": text[:200]}, False)
        if nid is None:
            return 0
        msg = {"title": title, "text": text, "short": text[:200], "is_demo": False, "template_params": []}
        self.dispatcher.deliver(nid, user_id, msg, None, destinations)
        return 1

    # ------------------------------------------------------------------ reads for the dashboard
    def captures(self, user_id: str, watch_id: str | None = None, limit: int = 20) -> list[dict]:
        sql = ("SELECT id, watch_id, job_id, recipe, kind, vars, status, reason, url, title, started_at, finished_at, parse, "
               "confidence, (screenshot IS NOT NULL) AS has_shot FROM assist_captures WHERE user_id=?")
        args: list = [user_id]
        if watch_id:
            sql += " AND watch_id=?"
            args.append(watch_id)
        rows = self.store.q(sql + " ORDER BY id DESC LIMIT ?", (*args, min(limit, 100)))
        for r in rows:
            r["vars"] = json.loads(r["vars"] or "{}")
            p = json.loads(r["parse"] or "{}")
            p.pop("route", None)
            p.pop("obs", None)
            r["parse"] = p
            r["has_shot"] = bool(r["has_shot"])
        return rows

    def capture(self, user_id: str, cid: int) -> dict | None:
        r = self.store.q("SELECT * FROM assist_captures WHERE id=? AND user_id=?", (cid, user_id))
        if not r:
            return None
        r = dict(r[0])
        r["has_shot"] = r.pop("screenshot") is not None
        for k in ("vars", "parse", "tables"):
            r[k] = json.loads(r[k] or "null")
        return r

    def screenshot(self, user_id: str, cid: int) -> bytes | None:
        r = self.store.q("SELECT screenshot FROM assist_captures WHERE id=? AND user_id=?", (cid, user_id))
        return r[0]["screenshot"] if r and r[0]["screenshot"] else None

    def source_health(self) -> dict:
        rows = self.store.q("SELECT status, COUNT(*) n FROM assist_captures WHERE created_at>=? GROUP BY status",
                            (iso(self.clock() - timedelta(hours=6)),))
        c = {r["status"]: r["n"] for r in rows}
        tot = sum(c.values())
        bad = c.get("captcha", 0) + c.get("blocked", 0) + c.get("login", 0)
        status = "UNKNOWN" if not tot else "DOWN" if bad and bad >= tot / 2 else "DEGRADED" if (tot - c.get("ok", 0)) else "OK"
        return {"source_id": SOURCE_ID, "status": status, "captures_6h": c}
