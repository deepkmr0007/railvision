"""Framework-agnostic HTTP application (routing, auth, rate limits, security headers).

`App.handle()` takes a plain request and returns (status, headers, body). It is served by the
zero-dependency server in api/server.py and can be mounted under FastAPI/ASGI (api/fastapi_app.py).
"""
from __future__ import annotations

import json
import os
import re
import secrets
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import parse_qs

from .. import __version__
from .. import stations as stn
from ..chatbot.engine import ChatEngine, summarize_vacancy
from ..chatbot.llm import LLMIntentParser
from ..core.crossing import evaluate_crossing
from ..core.timeutil import iso, now_ist
from ..delivery.channels import (AndroidPush, BrowserNotify, Dispatcher, Email, EventBus, InApp, Telegram)
from ..delivery.whatsapp import (ChannelCopyQueue, WhatsAppCloudClient, WhatsAppConfig, WhatsAppGroup,
                                 WhatsAppPersonal, parse_inbound, verify_challenge, verify_signature)
from ..providers.base import NotAuthorized, NotSupported, ProviderError
from ..providers.registry import SOURCE_MATRIX, ProviderRegistry
from ..news.service import SUGGESTED_WEB, NewsService
from ..services.assist_service import AssistService
from ..services.extras import HELP_LINKS, HELPLINES, DigestService, punctuality
from ..services.journeys import JourneyService
from ..services.resilience import KeyedRateLimiter
from ..services.store import Store
from ..services.tracker import TaskError, TrackingEngine
from ..services.wa_bot import WhatsAppBot

WEB_DIR = Path(os.environ.get("RAILVISION_WEB_DIR") or Path(__file__).resolve().parents[2] / "web")   # the phone app sets this
MAX_BODY = 256 * 1024
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
    "Permissions-Policy": "geolocation=(), camera=(), microphone=()",
    "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' "
                               "'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
                               "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'",
}


@dataclass
class Request:
    method: str
    path: str
    query: dict = field(default_factory=dict)
    headers: dict = field(default_factory=dict)   # lower-case keys
    body: bytes = b""
    client_ip: str = "?"

    def json(self) -> dict:
        try:
            return json.loads(self.body or b"{}")
        except ValueError:
            raise HttpError(400, "invalid JSON")

    def q1(self, k, default=None):
        v = self.query.get(k)
        return v[0] if isinstance(v, list) and v else (v if isinstance(v, str) else default)


class HttpError(Exception):
    def __init__(self, status: int, msg: str, extra: dict | None = None):
        super().__init__(msg)
        self.status, self.msg, self.extra = status, msg, extra or {}


def _json(status: int, obj) -> tuple[int, dict, bytes]:
    return status, {"Content-Type": "application/json; charset=utf-8"}, json.dumps(obj, ensure_ascii=False, default=str).encode()


class App:
    def __init__(self, store: Store | None = None, registry: ProviderRegistry | None = None, clock=now_ist,
                 wa_transport=None, tg_transport=None, logger=print, assist: bool | None = None, news_transport=None):
        self.mode = os.environ.get("RAILVISION_MODE", "demo")
        self.clock, self.log = clock, logger
        self.store = store or Store(os.environ.get("RAILVISION_DB", "railvision.db"),
                                    pepper=os.environ.get("RAILVISION_SECRET") or None)
        self.store.clock = clock
        self.registry = registry or ProviderRegistry.from_env(clock=clock if clock is not now_ist else None)
        self.bus = EventBus()
        self.wa_cfg = WhatsAppConfig.from_env()
        kw = {"transport": wa_transport} if wa_transport else {}
        self.wa_client = WhatsAppCloudClient(self.wa_cfg, **kw)
        tkw = {"transport": tg_transport} if tg_transport else {}
        self.telegram = Telegram(chat_lookup=lambda uid, task: self.store.tg_chat_for_user(uid)
                                 or os.environ.get("TELEGRAM_DEFAULT_CHAT_ID"), **tkw)
        self.dispatcher = Dispatcher(self.store, [
            InApp(self.bus), BrowserNotify(self.bus), WhatsAppPersonal(self.store, self.wa_client),
            WhatsAppGroup(self.wa_cfg), ChannelCopyQueue(self.store), self.telegram, Email(), AndroidPush()])
        self.app_url = os.environ.get("RAILVISION_APP_URL", "")
        self.tracker = TrackingEngine(self.store, self.registry, self.dispatcher, self.mode, self.app_url, clock)
        if assist is None:
            assist = os.environ.get("RAILVISION_ASSIST", "false").lower() == "true"
        self.assist = None
        if assist:
            if self.mode != "live":
                raise ValueError("The browser assistant reads real official pages: run it with RAILVISION_MODE=live")
            self.assist = AssistService(self.store, self.tracker, self.dispatcher, clock, self.app_url, logger)
            self.registry.providers.insert(0, self.assist.provider)    # first choice; licensed providers after it
        nkw = {"transport": news_transport} if news_transport else {}
        self.news = NewsService(self.store, self.dispatcher, clock, log=logger, **nkw)
        self.journeys = JourneyService(self.store, self.tracker, self.dispatcher, clock, assist=self.assist, news=self.news, log=logger)
        self.digest = DigestService(self.store, self.dispatcher, self.news, self.journeys, self.assist, clock)
        if self.assist:
            self.assist.news = self.news
            self.news.assist = self.assist
        self.news_enabled = os.environ.get("RAILVISION_NEWS", "true" if self.mode == "live" else "false").lower() == "true"
        self.chat = ChatEngine(self.tracker, self.registry, self.store, LLMIntentParser(), assist=self.assist)
        self.chat.news, self.chat.journeys = self.news, self.journeys
        self.wa_bot = WhatsAppBot(self.store, self.chat, self.wa_client, logger=logger)
        self.ip_limit = KeyedRateLimiter(int(os.environ.get("RAILVISION_RATE_PER_MIN", "120")))
        self.chat_limit = KeyedRateLimiter(30)
        self.hook_limit = KeyedRateLimiter(600)
        self.allow_signup = self.mode == "demo" or os.environ.get("RAILVISION_ALLOW_SIGNUP") == "true"
        self.cors = [o.strip() for o in os.environ.get("RAILVISION_CORS_ORIGINS", "").split(",") if o.strip()]
        self.routes = self._routes()

    # ------------------------------------------------------------------ plumbing
    def _routes(self):
        R = []
        def r(m, pat, fn, auth=False):
            R.append((m, re.compile("^" + pat + "$"), fn, auth))
        r("GET", r"/api/health", self.health)
        r("GET", r"/api/sources", self.sources)
        r("GET", r"/api/stations", self.stations)
        r("GET", r"/api/trains/search", self.train_search)
        r("GET", r"/api/trains/(?P<no>\d{5})/status", self.train_status)
        r("GET", r"/api/trains/(?P<no>\d{5})/route", self.train_route)
        r("GET", r"/api/trains/(?P<no>\d{5})/crossing", self.train_crossing)
        r("GET", r"/api/trains/(?P<no>\d{5})/passage-history", self.passage_history, True)
        r("GET", r"/api/stations/(?P<code>[A-Z0-9]{1,6})/board", self.station_board)
        r("GET", r"/api/between", self.between)
        r("GET", r"/api/vacancy", self.vacancy)
        r("POST", r"/api/session", self.session)
        r("GET", r"/api/me", self.me, True)
        r("GET", r"/api/tasks", self.tasks_list, True)
        r("POST", r"/api/tasks", self.tasks_create, True)
        r("GET", r"/api/tasks/(?P<tid>t_[0-9a-f]+)", self.task_get, True)
        r("PATCH", r"/api/tasks/(?P<tid>t_[0-9a-f]+)", self.task_patch, True)
        r("DELETE", r"/api/tasks/(?P<tid>t_[0-9a-f]+)", self.task_delete, True)
        r("POST", r"/api/tasks/(?P<tid>t_[0-9a-f]+)/(?P<action>confirm|pause|resume|cancel)", self.task_action, True)
        r("POST", r"/api/chat", self.chat_post, True)
        r("GET", r"/api/notifications", self.notifications, True)
        r("POST", r"/api/whatsapp/link-code", self.link_code, True)
        r("GET", r"/api/channel-queue", self.channel_queue, True)
        r("POST", r"/api/channel-queue/(?P<pid>\d+)/published", self.channel_published, True)
        r("GET", r"/api/reports/summary", self.report, True)
        r("POST", r"/api/admin/tick", self.admin_tick, True)
        # announcements, journeys, stats, digest, help
        r("GET", r"/api/news", self.nw_items, True)
        r("GET", r"/api/news/sources", self.nw_sources, True)
        r("POST", r"/api/news/sources", self.nw_source_add, True)
        r("PATCH", r"/api/news/sources/(?P<sid>[a-z0-9_]+)", self.nw_source_patch, True)
        r("DELETE", r"/api/news/sources/(?P<sid>[a-z0-9_]+)", self.nw_source_del, True)
        r("POST", r"/api/news/sources/(?P<sid>[a-z0-9_]+)/fetch-now", self.nw_fetch_now, True)
        r("GET", r"/api/news/usage", self.nw_usage, True)
        r("GET", r"/api/prefs", self.pr_get, True)
        r("PATCH", r"/api/prefs", self.pr_patch, True)
        r("GET", r"/api/journeys", self.jn_list, True)
        r("POST", r"/api/journeys", self.jn_add, True)
        r("GET", r"/api/journeys\.ics", self.jn_ics_all, True)
        r("GET", r"/api/journeys/(?P<jid>j_[0-9a-f]+)\.ics", self.jn_ics_one, True)
        r("GET", r"/api/journeys/(?P<jid>j_[0-9a-f]+)", self.jn_get, True)
        r("PATCH", r"/api/journeys/(?P<jid>j_[0-9a-f]+)", self.jn_patch, True)
        r("DELETE", r"/api/journeys/(?P<jid>j_[0-9a-f]+)", self.jn_del, True)
        r("POST", r"/api/journeys/(?P<jid>j_[0-9a-f]+)/check-pnr", self.jn_pnr, True)
        r("GET", r"/api/stats/punctuality", self.st_punct, True)
        r("GET", r"/api/digest/preview", self.dg_preview, True)
        r("POST", r"/api/digest/send-now", self.dg_send, True)
        r("GET", r"/api/help", self.help)
        # browser assistant
        r("GET", r"/api/assist/status", self.as_status, True)
        r("GET", r"/api/assist/watch", self.as_watch_list, True)
        r("POST", r"/api/assist/watch", self.as_watch_add, True)
        r("PATCH", r"/api/assist/watch/(?P<wid>w_[0-9a-f]+)", self.as_watch_patch, True)
        r("DELETE", r"/api/assist/watch/(?P<wid>w_[0-9a-f]+)", self.as_watch_delete, True)
        r("POST", r"/api/assist/watch/(?P<wid>w_[0-9a-f]+)/check-now", self.as_watch_now, True)
        r("GET", r"/api/assist/work", self.as_work, True)
        r("POST", r"/api/assist/heartbeat", self.as_heartbeat, True)
        r("POST", r"/api/assist/captures", self.as_ingest, True)
        r("GET", r"/api/assist/captures", self.as_captures, True)
        r("GET", r"/api/assist/captures/(?P<cid>\d+)", self.as_capture, True)
        r("GET", r"/api/assist/captures/(?P<cid>\d+)/screenshot", self.as_shot, True)
        r("GET", r"/api/assist/recipes", self.as_recipes, True)
        r("PUT", r"/api/assist/recipes/(?P<name>[a-z0-9_]{3,40})", self.as_recipe_put, True)
        r("GET", r"/api/assist/jobs", self.as_jobs, True)
        r("POST", r"/api/assist/jobs", self.as_job_add, True)
        r("GET", r"/webhooks/whatsapp", self.wa_verify)
        r("POST", r"/webhooks/whatsapp", self.wa_inbound)
        r("POST", r"/webhooks/telegram", self.tg_inbound)
        return R

    def _auth(self, req: Request) -> str:
        h = req.headers.get("authorization", "")
        tok = h[7:] if h.lower().startswith("bearer ") else (req.q1("token") if (req.path == "/api/events" or req.path.endswith(".ics")) else None)
        uid = self.store.user_for_token(tok or "")
        if not uid:
            raise HttpError(401, "authentication required")
        return uid

    def cors_headers(self, req: Request) -> dict:
        origin = req.headers.get("origin")
        if origin and (origin in self.cors or "*" in self.cors):
            return {"Access-Control-Allow-Origin": origin, "Vary": "Origin",
                    "Access-Control-Allow-Headers": "authorization, content-type",
                    "Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS"}
        return {}

    def handle(self, req: Request) -> tuple[int, dict, bytes]:
        try:
            st, hd, body = self._dispatch(req)
        except HttpError as e:
            st, hd, body = _json(e.status, {"error": e.msg, **e.extra})
        except TaskError as e:
            st, hd, body = _json(422, {"error": str(e)})
        except NotSupported as e:
            st, hd, body = _json(404, {"error": str(e), "official_url": e.official_url, "label": "MANUAL-LINK FALLBACK"})
        except NotAuthorized as e:
            st, hd, body = _json(503, {"error": "source not authorized", "detail": str(e)})
        except ProviderError as e:
            st, hd, body = _json(503, {"error": "data temporarily unavailable", "detail": str(e)})
        except Exception as e:  # never leak internals
            self.log(f"[error] {req.method} {req.path}: {type(e).__name__}: {e}")
            st, hd, body = _json(500, {"error": "internal error"})
        hd = {**SECURITY_HEADERS, **self.cors_headers(req), **hd}
        return st, hd, body

    def _dispatch(self, req: Request):
        if req.method == "OPTIONS":
            return 204, {}, b""
        limit = 6 * 1024 * 1024 if req.path == "/api/assist/captures" else MAX_BODY
        if len(req.body) > limit:
            raise HttpError(413, "body too large")
        is_hook = req.path.startswith("/webhooks/")
        if not (self.hook_limit if is_hook else self.ip_limit).allow(req.client_ip):
            raise HttpError(429, "rate limit exceeded")
        if req.method == "GET" and not req.path.startswith(("/api/", "/webhooks/")):
            return self.static(req)
        for m, pat, fn, auth in self.routes:
            mt = pat.match(req.path)
            if mt and m == req.method:
                uid = self._auth(req) if auth else None
                kwargs = mt.groupdict()
                return fn(req, uid, **kwargs) if auth else fn(req, **kwargs)
        raise HttpError(404, "not found")

    def static(self, req: Request):
        name = {"/": "index.html", "/index.html": "index.html", "/manifest.webmanifest": "manifest.webmanifest",
                "/sw.js": "sw.js", "/icon.svg": "icon.svg"}.get(req.path)
        if not name or not (WEB_DIR / name).exists():
            raise HttpError(404, "not found")
        ctype = {"html": "text/html; charset=utf-8", "webmanifest": "application/manifest+json",
                 "js": "text/javascript", "svg": "image/svg+xml"}[name.rsplit(".", 1)[1]]
        data = (WEB_DIR / name).read_bytes()
        if name == "index.html" and not data.lstrip().lower().startswith(b"<!doctype"):
            # the page is authored as a body fragment (also publishable standalone); give it a document skeleton
            data = (b'<!doctype html><html lang="en"><head><meta charset="utf-8">'
                    b'<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
                    b'<link rel="icon" href="icon.svg"></head><body>' + data + b"</body></html>")
        return 200, {"Content-Type": ctype, "Cache-Control": "no-cache"}, data

    def _date(self, req: Request, train: str | None = None) -> date:
        v = req.q1("date")
        if v:
            try:
                return date.fromisoformat(v)
            except ValueError:
                raise HttpError(400, "date must be YYYY-MM-DD")
        p = self.registry.primary()
        if train and hasattr(p, "default_service_date"):
            try:
                return p.default_service_date(train)
            except NotSupported:
                pass
        return self.clock().date()

    # ------------------------------------------------------------------ public endpoints
    def health(self, req):
        return _json(200, {"ok": True, "version": __version__, "mode": self.mode, "time": iso(self.clock()),
                           "timezone": "Asia/Kolkata"})

    def sources(self, req):
        return _json(200, {"mode": self.mode, "matrix": SOURCE_MATRIX, "health": self.registry.health(),
                           "destinations": self.dispatcher.catalogue(), "checked": "2026-10-14"})

    def stations(self, req):
        q = req.q1("q", "")
        return _json(200, {"results": [s.__dict__ for s in stn.resolve(q)][:20],
                           "note": "Seed list only; production loads an authorized station master."})

    def train_search(self, req):
        q = req.q1("q", "")
        try:
            p, rows = self.registry.call("search_trains", q)
            return _json(200, {"results": rows, "source": p.source_id})
        except NotSupported as e:
            return _json(200, {"results": [], "source": None, "note": str(e), "official_url": e.official_url})

    def train_status(self, req, no):
        sd = self._date(req, no)
        snap = self.tracker.snapshot(no, sd, req.q1("station"))
        return _json(200, snap)

    def train_route(self, req, no):
        sd = self._date(req, no)
        r = self.tracker.route(no, sd)
        return _json(200, {"train_number": r.train_number, "train_name": r.train_name, "service_date": sd.isoformat(),
                           "source": r.source_id, "is_demo": r.is_demo,
                           "stops": [{**s.__dict__, "lat": (stn.STATIONS.get(s.code).lat if stn.STATIONS.get(s.code) else None),
                                      "lon": (stn.STATIONS.get(s.code).lon if stn.STATIONS.get(s.code) else None)} for s in r.stops]})

    def train_crossing(self, req, no):
        code = (req.q1("station") or "").upper()
        if not code:
            raise HttpError(400, "station required")
        sd = self._date(req, no)
        r = self.tracker.route(no, sd)
        obs = self.tracker.observations(no, sd)
        c = evaluate_crossing(r, obs, code, self.clock(), sd)
        self.store.record_passage(None, c)
        return _json(200, c.to_dict())

    def passage_history(self, req, uid, no):
        sd = self._date(req, no)
        return _json(200, {"history": self.store.passage_history(no, sd.isoformat(), (req.q1("station") or "").upper())})

    def station_board(self, req, code):
        hours = min(int(req.q1("hours", "4")), 12)
        now = self.clock()
        p, rows = self.registry.call("get_station_board", code, now - timedelta(hours=1), now + timedelta(hours=hours))
        return _json(200, {"station": code, "rows": rows, "source": p.source_id, "is_demo": p.is_demo,
                           "retrieved_at": iso(now)})

    def between(self, req):
        a, b = (req.q1("from") or "").upper(), (req.q1("to") or "").upper()
        if not a or not b:
            raise HttpError(400, "from and to required")
        sd = self._date(req)
        p, rows = self.registry.call("find_trains_between", a, b, sd)
        return _json(200, {"rows": rows, "source": p.source_id, "is_demo": p.is_demo, "date": sd.isoformat()})

    def vacancy(self, req):
        no, frm, cls = req.q1("train"), (req.q1("from") or "").upper(), (req.q1("class") or "").upper()
        if not (no and frm and cls):
            raise HttpError(400, "train, from and class required")
        sd = self._date(req, no)
        p, snap = self.registry.call("get_chart_vacancy", no, sd, frm, cls)
        d = snap.to_dict()
        d["summary"] = summarize_vacancy(d)
        d["disclaimer"] = ("Post-chart vacancy as published by the source at the chart time shown. It is not reservation "
                           "availability and does not guarantee a berth is bookable or physically empty onboard.")
        return _json(200, d)

    # ------------------------------------------------------------------ account & tasks
    def session(self, req):
        if not self.allow_signup:
            raise HttpError(403, "signup disabled")
        name = str(req.json().get("display_name", "Traveller"))[:60]
        uid, token = self.store.create_user(name)
        return _json(201, {"user_id": uid, "token": token,
                           "note": "Keep this token secret; it is shown once." if self.mode == "live" else "Demo session"})

    def me(self, req, uid):
        wa = self.store.wa_for_user(uid)
        return _json(200, {"user_id": uid, "whatsapp_linked": bool(wa), "telegram_linked": bool(self.store.tg_chat_for_user(uid)),
                           "mode": self.mode})

    def tasks_list(self, req, uid):
        return _json(200, {"tasks": self.store.list_tasks(uid)})

    def tasks_create(self, req, uid):
        b = req.json()
        try:
            no = str(b["train_number"])
            ref = str(b["reference_station"]).upper()
        except KeyError:
            raise HttpError(400, "train_number and reference_station required")
        if not re.fullmatch(r"\d{5}", no):
            raise HttpError(400, "train_number must be 5 digits")
        sd = date.fromisoformat(b["service_date"]) if b.get("service_date") else self._date(req, no)
        quiet = (b["quiet_start"], b["quiet_end"]) if b.get("quiet_start") and b.get("quiet_end") else None
        t = self.tracker.draft_task(uid, no, sd, ref, (b.get("end_station") or None) and str(b["end_station"]).upper(),
                                    int(b.get("interval_min", 15)), list(b.get("destinations") or ["app"]),
                                    b.get("delay_threshold_min"), quiet)
        if b.get("confirm"):
            t = {**self.tracker.confirm(t["id"], uid), "summary": t["summary"]}
        return _json(201, t)

    def task_get(self, req, uid, tid):
        t = self.store.get_task(tid, uid)
        if not t:
            raise HttpError(404, "not found")
        return _json(200, t)

    def task_patch(self, req, uid, tid):
        b = req.json()
        allowed = {k: b[k] for k in ("interval_min", "delay_threshold_min", "destinations", "end_station",
                                     "quiet_start", "quiet_end") if k in b}
        t = self.tracker.update(tid, uid, **allowed)
        return _json(200, t)

    def task_delete(self, req, uid, tid):
        if not self.store.delete_task(tid, uid):
            raise HttpError(404, "not found")
        return _json(200, {"deleted": tid})

    def task_action(self, req, uid, tid, action):
        t = self.tracker.confirm(tid, uid) if action == "confirm" else self.tracker.set_state(tid, uid, action)
        return _json(200, t)

    def chat_post(self, req, uid):
        if not self.chat_limit.allow(uid):
            raise HttpError(429, "slow down")
        b = req.json()
        text = str(b.get("text", ""))[:1000]
        sid = "web:" + uid + ":" + re.sub(r"[^a-zA-Z0-9_-]", "", str(b.get("session_id", "main")))[:32]
        return _json(200, self.chat.handle(sid, uid, text, channel="web"))

    def notifications(self, req, uid):
        return _json(200, {"items": self.store.list_notifications(uid, 50, int(req.q1("after", "0") or 0))})

    def link_code(self, req, uid):
        return _json(200, {"code": self.store.new_link_code(uid), "expires_in_min": 15,
                           "instructions": "Send 'LINK <code>' to the RAILVISION WhatsApp number."})

    def channel_queue(self, req, uid):
        return _json(200, {"items": self.store.channel_queue(), "note": "Copy-ready posts for a WhatsApp Channel admin "
                                                                       "(no official publishing API)."})

    def channel_published(self, req, uid, pid):
        self.store.mark_channel_published(int(pid))
        return _json(200, {"ok": True})

    def report(self, req, uid):
        tasks = self.store.list_tasks(uid)
        notes = self.store.list_notifications(uid, 500)
        by_type: dict = {}
        for n in notes:
            by_type[n["type"]] = by_type.get(n["type"], 0) + 1
        return _json(200, {"tasks": {s: sum(1 for t in tasks if t["status"] == s) for s in
                                     ("ACTIVE", "PAUSED", "COMPLETED", "CANCELLED", "PENDING_CONFIRMATION")},
                           "notifications_by_type": by_type, "engine_metrics": dict(self.tracker.metrics),
                           "store": self.store.stats()})

    # ------------------------------------------------------------------ announcements
    def nw_items(self, req, uid):
        items = self.news.items(uid, relevant_only=req.q1("relevant") == "1", category=req.q1("category") or None,
                                q=(req.q1("q") or "")[:60] or None, limit=min(int(req.q1("limit", "60") or 60), 200))
        return _json(200, {"items": items, "enabled": self.news_enabled})

    def nw_sources(self, req, uid):
        return _json(200, {"sources": self.news.sources(), "suggested": SUGGESTED_WEB, "x": self.news.x_usage_today(),
                           "enabled": self.news_enabled})

    def nw_source_add(self, req, uid):
        return _json(201, self._ve(self.news.add_source, req.json()))

    def nw_source_patch(self, req, uid, sid):
        s = self._ve(self.news.update_source, sid, req.json())
        if not s:
            raise HttpError(404, "not found")
        return _json(200, s)

    def nw_source_del(self, req, uid, sid):
        if not self.news.delete_source(sid):
            raise HttpError(404, "not found")
        return _json(200, {"deleted": sid})

    def nw_fetch_now(self, req, uid, sid):
        return _json(200, self.news.fetch_now(sid))

    def nw_usage(self, req, uid):
        return _json(200, self.news.x_usage_today())

    def pr_get(self, req, uid):
        return _json(200, self.news.prefs(uid))

    def pr_patch(self, req, uid):
        return _json(200, self._ve(self.news.set_prefs, uid, req.json()))

    # ------------------------------------------------------------------ journeys
    def jn_list(self, req, uid):
        return _json(200, {"journeys": self.journeys.list(uid, include_done=req.q1("all") == "1")})

    def jn_add(self, req, uid):
        j = self._ve(self.journeys.add, uid, req.json())
        self.journeys.tick()
        return _json(201, self.journeys.get(uid, j["id"]))

    def jn_get(self, req, uid, jid):
        j = self.journeys.get(uid, jid)
        if not j:
            raise HttpError(404, "not found")
        return _json(200, j)

    def jn_patch(self, req, uid, jid):
        j = self._ve(self.journeys.update, uid, jid, req.json())
        if not j:
            raise HttpError(404, "not found")
        return _json(200, j)

    def jn_del(self, req, uid, jid):
        if not self.journeys.delete(uid, jid):
            raise HttpError(404, "not found")
        return _json(200, {"deleted": jid})

    def jn_pnr(self, req, uid, jid):
        try:
            return _json(201, self.journeys.check_pnr(uid, jid))
        except LookupError:
            raise HttpError(404, "not found")
        except ValueError as e:
            raise HttpError(422, str(e))

    def _ics(self, body: str, name: str):
        return 200, {"Content-Type": "text/calendar; charset=utf-8", "Content-Disposition": f'attachment; filename="{name}"'}, body.encode()

    def jn_ics_all(self, req, uid):
        return self._ics(self.journeys.ics(self.journeys.list(uid, include_done=False)), "railvision-trips.ics")

    def jn_ics_one(self, req, uid, jid):
        j = self.journeys.get(uid, jid)
        if not j:
            raise HttpError(404, "not found")
        return self._ics(self.journeys.ics([j]), f"trip-{j['train']}-{j['start_date']}.ics")

    # ------------------------------------------------------------------ stats, digest, help
    def st_punct(self, req, uid):
        t = req.q1("train") or ""
        if not re.fullmatch(r"\d{5}", t):
            raise HttpError(400, "train must be 5 digits")
        st = (req.q1("station") or "").upper() or None
        return _json(200, punctuality(self.store, t, st, min(int(req.q1("days", "30") or 30), 180), include_demo=self.mode == "demo"))

    def dg_preview(self, req, uid):
        return _json(200, {"text": self.digest.compose(uid), "prefs": self.news.prefs(uid)})

    def dg_send(self, req, uid):
        return _json(200, {"sent": self.digest.send(uid)})

    def help(self, req):
        return _json(200, {"helplines": HELPLINES, "links": HELP_LINKS})

    # ------------------------------------------------------------------ background work (every minute)
    def housekeeping(self) -> dict:
        out = {}
        for name, fn in (("news", self.news.tick if self.news_enabled else None), ("journeys", self.journeys.tick),
                         ("digest", self.digest.tick)):
            if fn is None:
                continue
            try:
                out[name] = fn()
            except Exception as e:
                self.log(f"[housekeeping] {name}: {e!r}")
                out[name] = {"error": repr(e)}
        return out

    # ------------------------------------------------------------------ browser assistant
    def _as(self):
        if not self.assist:
            raise HttpError(404, "browser assistant is off (start with RAILVISION_MODE=live RAILVISION_ASSIST=true)")
        return self.assist

    def _ve(self, fn, *a):
        try:
            return fn(*a)
        except ValueError as e:
            raise HttpError(422, str(e))
        except PermissionError as e:
            raise HttpError(404, str(e))

    def as_status(self, req, uid):
        a = self._as()
        return _json(200, {"agent": a.agent_state(uid), "watch": a.list_watch(uid), "health": a.source_health(),
                           "recipes": {k: {"mode": v["mode"], "verified": v["verified"], "steps": len(v["steps"]), "notes": v["notes"]}
                                       for k, v in a.recipes().items()}})

    def as_watch_list(self, req, uid):
        return _json(200, {"watch": self._as().list_watch(uid), "max": 10})

    def as_watch_add(self, req, uid):
        return _json(201, self._ve(self._as().add_watch, uid, req.json()))

    def as_watch_patch(self, req, uid, wid):
        w = self._ve(self._as().update_watch, uid, wid, req.json())
        if not w:
            raise HttpError(404, "not found")
        return _json(200, w)

    def as_watch_delete(self, req, uid, wid):
        if not self._as().delete_watch(uid, wid):
            raise HttpError(404, "not found")
        return _json(200, {"deleted": wid})

    def as_watch_now(self, req, uid, wid):
        w = self._as().check_now(uid, wid)
        if not w:
            raise HttpError(404, "not found")
        return _json(200, w)

    def as_work(self, req, uid):
        info = {"agent": req.headers.get("x-rv-agent", "")[:80]}
        return _json(200, self._as().work(uid, info))

    def as_heartbeat(self, req, uid):
        b = req.json()
        self._as().heartbeat(uid, b.get("info"), b.get("state"), str(b.get("reason", ""))[:300])
        return _json(200, {"ok": True})

    def as_ingest(self, req, uid):
        return _json(201, self._ve(self._as().ingest, uid, req.json()))

    def as_captures(self, req, uid):
        return _json(200, {"items": self._as().captures(uid, req.q1("watch_id"), int(req.q1("limit", "20") or 20))})

    def as_capture(self, req, uid, cid):
        c = self._as().capture(uid, int(cid))
        if not c:
            raise HttpError(404, "not found")
        return _json(200, c)

    def as_shot(self, req, uid, cid):
        b = self._as().screenshot(uid, int(cid))
        if not b:
            raise HttpError(404, "no screenshot")
        return 200, {"Content-Type": "image/jpeg", "Cache-Control": "private, max-age=3600"}, b

    def as_recipes(self, req, uid):
        return _json(200, self._as().recipes())

    def as_recipe_put(self, req, uid, name):
        return _json(200, self._ve(self._as().save_recipe, name, req.json()))

    def as_jobs(self, req, uid):
        return _json(200, {"items": self._as().list_jobs(uid)})

    def as_job_add(self, req, uid):
        b = req.json()
        v = {k: str(x) for k, x in (b.get("vars") or {}).items() if k in ("train", "date", "station", "class", "query")}
        return _json(201, self._ve(self._as().queue_job, uid, str(b.get("kind")), v))

    def admin_tick(self, req, uid):
        if self.mode != "demo":
            raise HttpError(403, "manual tick only in demo mode")
        return _json(200, self.tracker.tick())

    # ------------------------------------------------------------------ webhooks
    def wa_verify(self, req):
        st, body = verify_challenge({k: req.q1(k) for k in ("hub.mode", "hub.verify_token", "hub.challenge")},
                                    self.wa_cfg.verify_token)
        return st, {"Content-Type": "text/plain"}, body.encode()

    def wa_inbound(self, req):
        if not verify_signature(self.wa_cfg.app_secret, req.body, req.headers.get("x-hub-signature-256")):
            raise HttpError(401, "invalid signature")
        msgs, statuses = parse_inbound(req.json())
        results = [self.wa_bot.handle_message(m) for m in msgs]
        for s in statuses:
            if s.get("status") == "failed":
                self.log(f"[wa] delivery failed to {s['recipient']}: {s.get('errors')}")
        return _json(200, {"processed": len(results)})   # always 200 quickly so Meta doesn't retry-storm

    def tg_inbound(self, req):
        if not self.telegram.verify_webhook(req.headers.get("x-telegram-bot-api-secret-token")):
            raise HttpError(401, "invalid secret")
        u = req.json()
        msg = u.get("message") or u.get("channel_post") or {}
        chat_id, text = (msg.get("chat") or {}).get("id"), (msg.get("text") or "")
        if not chat_id or not text:
            return _json(200, {"ok": True})
        if self.store.seen_message(f"tg:{u.get('update_id')}"):
            return _json(200, {"ok": True, "duplicate": True})
        uid = self.store.tg_user(str(chat_id))
        res = self.chat.handle(f"tg:{chat_id}", uid, text[:1000], channel="telegram")
        self.telegram.transport(f"https://api.telegram.org/bot{self.telegram.token}/sendMessage",
                                {"Content-Type": "application/json"},
                                json.dumps(self.telegram.build(str(chat_id), res["reply"])).encode(), 10.0)
        return _json(200, {"ok": True})


def parse_query(qs: str) -> dict:
    return parse_qs(qs, keep_blank_values=True)


def new_secret() -> str:
    return secrets.token_hex(32)
