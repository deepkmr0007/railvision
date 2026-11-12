"""Announcement sources, fetching schedule, storage, relevance and alerts."""
from __future__ import annotations

import json
import os
import re
import secrets
from datetime import timedelta, timezone

from ..core.timeutil import iso, now_ist, parse_iso
import base64

from .. import stations as stn
from .fetch import SKIP_TEXT, FeedFetcher, FetchError, Item, XClient, urllib_get
from .official import LINK_FILTER, ZONE_PAGES, focus, is_pdf, pdf_to_text
from .relevance import is_railway, score, tag

SCHEMA = """
CREATE TABLE IF NOT EXISTS news_sources (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL, url TEXT, handle TEXT, interval_min INTEGER NOT NULL,
  enabled INTEGER NOT NULL, official INTEGER NOT NULL, rail_filter INTEGER NOT NULL DEFAULT 0, verified TEXT, notes TEXT,
  last_fetch_at TEXT, next_due_at TEXT, last_status TEXT, last_detail TEXT, etag TEXT, last_modified TEXT,
  x_user_id TEXT, since_id TEXT, fail_count INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS news_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT UNIQUE NOT NULL, source_id TEXT NOT NULL, title TEXT, text TEXT, url TEXT,
  author TEXT, published_at TEXT, fetched_at TEXT NOT NULL, trains TEXT, stations TEXT, categories TEXT, zones TEXT,
  baseline INTEGER NOT NULL DEFAULT 0, official INTEGER NOT NULL DEFAULT 1);
CREATE INDEX IF NOT EXISTS ix_news_time ON news_items(fetched_at);
CREATE TABLE IF NOT EXISTS news_usage (day TEXT NOT NULL, kind TEXT NOT NULL, units INTEGER NOT NULL, cost_usd REAL NOT NULL,
  PRIMARY KEY(day, kind));
CREATE TABLE IF NOT EXISTS user_prefs (user_id TEXT PRIMARY KEY, prefs TEXT NOT NULL);
"""
MIGRATIONS = {   # columns added after v2.2.0 — applied to existing databases on start
    "news_sources": [("link_filter", "TEXT"), ("fetch_via", "TEXT NOT NULL DEFAULT 'direct'"), ("zone", "TEXT"),
                     ("user_set", "INTEGER NOT NULL DEFAULT 0")],
    "news_items": [("detail_status", "TEXT"), ("detail_note", "TEXT")],
}
DETAILS_PER_RUN = 5

PIB_EN = "https://pib.gov.in/RssMain.aspx?ModId=6&Lang=1&Regid=3"
PIB_HI = "https://pib.gov.in/RssMain.aspx?ModId=6&Lang=2&Regid=3"

DEFAULT_SOURCES = [
    # id, kind, name, url/handle, interval, enabled, official, rail_filter, verified, notes
    ("pib_en", "rss", "PIB press releases (railway items)", PIB_EN, 60, True, True, True,
     "2026-10-15: feed reachable; items carry title + link only, all ministries mixed — filtered to railway items",
     "Press Information Bureau, Government of India"),
    ("pib_hi", "rss", "PIB प्रेस विज्ञप्ति (रेलवे)", PIB_HI, 60, False, True, True,
     "2026-10-15: listed on pib.gov.in/ViewRss.aspx", "Hindi releases; enable if you prefer Hindi"),
    ("x_railminindia", "x", "Ministry of Railways on X", "RailMinIndia", 60, False, True, False, None,
     "Official X API only — needs your X bearer token and credits"),
    ("x_irctc", "x", "IRCTC on X", "IRCTCofficial", 60, False, True, False, None, "Official X API only"),
    ("x_railwayseva", "x", "RailwaySeva on X", "RailwaySeva", 120, False, True, False, None,
     "Mostly replies to complaints; replies are excluded"),
]
SUGGESTED_WEB: list[dict] = []          # zonal press-release pages are now built-in sources (see official.ZONE_PAGES)
X_PRICE_POST = XClient.PRICE_POST
X_PRICE_USER = XClient.PRICE_USER


class NewsService:
    def __init__(self, store, dispatcher, clock=now_ist, transport=urllib_get, log=print):
        self.store, self.dispatcher, self.clock, self.log = store, dispatcher, clock, log
        self.transport = transport
        self.fetcher = FeedFetcher(transport)
        with store.lock:
            store.db.executescript(SCHEMA)
            for tbl, cols in MIGRATIONS.items():
                have = {r[1] for r in store.db.execute(f"PRAGMA table_info({tbl})").fetchall()}
                for col, typ in cols:
                    if col not in have:
                        store.db.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {typ}")
        self.assist = None                # AssistService, for pages read by your browser assistant
        for code, zone, url, evidence in ZONE_PAGES:
            if not store.q("SELECT 1 FROM news_sources WHERE id=?", (f"zone_{code}",)):
                store.x("""INSERT INTO news_sources(id,kind,name,url,interval_min,enabled,official,rail_filter,verified,notes,created_at,
                           link_filter,fetch_via,zone) VALUES (?,?,?,?,?,0,1,0,?,?,?,?,?,?)""",
                        (f"zone_{code}", "web", f"{zone} Railway — press releases", url, 120,
                         f"2026-10-15 {evidence}" if evidence else None,
                         "Official zonal website. Address " + ("found in web search" if evidence else
                                                               "follows the portal's usual pattern — not yet confirmed") +
                         ". Turns on automatically when one of your stations is in this zone.",
                         iso(clock()), LINK_FILTER, "direct", zone))
        for sid, kind, name, target, iv, en, off, rf, ver, notes in DEFAULT_SOURCES:
            if not store.q("SELECT 1 FROM news_sources WHERE id=?", (sid,)):
                store.x("""INSERT INTO news_sources(id,kind,name,url,handle,interval_min,enabled,official,rail_filter,verified,notes,created_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (sid, kind, name, target if kind != "x" else None, target if kind == "x" else None, iv, int(en), int(off),
                         int(rf), ver, notes, iso(clock())))
        self.x_token = os.environ.get("X_BEARER_TOKEN", "")
        self.x_daily_posts = int(os.environ.get("X_DAILY_POST_BUDGET", "60"))      # ≈ $0.30/day at $0.005/post

    # ---------------------------------------------------------------- prefs / interests
    def prefs(self, user_id: str) -> dict:
        r = self.store.q("SELECT prefs FROM user_prefs WHERE user_id=?", (user_id,))
        d = {"zones": [], "keywords": [], "stations": [], "digest_time": "07:00", "digest_enabled": True,
             "digest_destinations": ["app"], "announce_alerts": True, "announce_min_score": 0.8}      # alert: your train, or a disruption at your station
        if r:
            d.update(json.loads(r[0]["prefs"]))
        return d

    def set_prefs(self, user_id: str, p: dict) -> dict:
        cur = self.prefs(user_id)
        if "zones" in p:
            cur["zones"] = [z.title() for z in p["zones"] if isinstance(z, str)][:10]
        if "keywords" in p:
            cur["keywords"] = [k.strip()[:40] for k in p["keywords"] if isinstance(k, str) and k.strip()][:20]
        if "stations" in p:
            cur["stations"] = [s.upper()[:5] for s in p["stations"] if isinstance(s, str) and re.fullmatch(r"[A-Za-z]{1,5}", s)][:20]
        if "digest_time" in p and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(p["digest_time"])):
            cur["digest_time"] = p["digest_time"]
        for k in ("digest_enabled", "announce_alerts"):
            if k in p:
                cur[k] = bool(p[k])
        if "digest_destinations" in p:
            cur["digest_destinations"] = list(dict.fromkeys(["app"] + [d for d in p["digest_destinations"] if d in (
                "app", "whatsapp", "telegram", "email", "browser")]))
        if "announce_min_score" in p:
            cur["announce_min_score"] = min(1.0, max(0.4, float(p["announce_min_score"])))
        self.store.x("INSERT OR REPLACE INTO user_prefs VALUES (?,?)", (user_id, json.dumps(cur)))
        return cur

    def interests(self, user_id: str) -> dict:
        trains, stations = set(), set(self.prefs(user_id)["stations"])
        for t in self.store.q("SELECT train_number, reference_station, end_station FROM tracking_tasks WHERE user_id=? "
                              "AND status IN ('ACTIVE','PAUSED')", (user_id,)):
            trains.add(t["train_number"])
            stations |= {t["reference_station"], t["end_station"]} - {None}
        for tbl, cols in (("assist_watch", "train, ref_station, end_station"), ("journeys", "train, board, dest")):
            try:
                rows = self.store.q(f"SELECT {cols} FROM {tbl} WHERE user_id=?" + (" AND active=1" if tbl == "assist_watch" else
                                                                                   " AND status IN ('upcoming','active')"), (user_id,))
            except Exception:
                continue
            for r in rows:
                v = list(r.values())
                trains.add(v[0])
                stations |= {x for x in v[1:] if x}
        zones = set(self.prefs(user_id)["zones"]) | {stn.STATION_ZONE[c] for c in stations if c in stn.STATION_ZONE}
        return {"trains": trains, "stations": stations, "zones": zones,
                "keywords": [k.lower() for k in self.prefs(user_id)["keywords"]]}

    # ---------------------------------------------------------------- sources
    def sources(self) -> list[dict]:
        rows = self.store.q("SELECT * FROM news_sources ORDER BY kind, name")
        for r in rows:
            r["enabled"], r["official"], r["rail_filter"] = bool(r["enabled"]), bool(r["official"]), bool(r["rail_filter"])
            r["user_set"] = bool(r.get("user_set"))
            r["count"] = self.store.q("SELECT COUNT(*) n FROM news_items WHERE source_id=?", (r["id"],))[0]["n"]
        return rows

    def add_source(self, b: dict) -> dict:
        kind = b.get("kind")
        if kind not in ("rss", "web", "x"):
            raise ValueError("kind must be rss, web or x")
        name = str(b.get("name") or "").strip()[:80]
        if kind == "x":
            h = str(b.get("handle", "")).lstrip("@").strip()
            if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", h):
                raise ValueError("X handle: letters, digits, _ (max 15)")
            target_url, handle = None, h
            name = name or f"@{h} on X"
        else:
            u = str(b.get("url", "")).strip()
            if not re.match(r"https?://[^\s/]+\.[^\s]+", u):
                raise ValueError("url must be a full http(s) address")
            target_url, handle = u, None
            name = name or u.split("/")[2]
        iv = int(b.get("interval_min", 60))
        if iv < (30 if kind == "x" else 15) or iv > 1440:
            raise ValueError("interval: 15-1440 min (X: at least 30)")
        sid = f"{kind}_" + secrets.token_hex(4)
        lf = b.get("link_filter")
        if lf is None and target_url and "indianrailways.gov.in" in target_url:
            lf = LINK_FILTER                       # zonal portals: only announcement links, not the menus
        if lf:
            try:
                re.compile(lf)
            except re.error:
                raise ValueError("link_filter is not a valid pattern")
        via = b.get("fetch_via", "direct")
        if via not in ("direct", "assistant") or (via == "assistant" and kind != "web"):
            raise ValueError("fetch_via: direct, or assistant (web pages only)")
        self.store.x("""INSERT INTO news_sources(id,kind,name,url,handle,interval_min,enabled,official,rail_filter,verified,notes,created_at,
                        link_filter,fetch_via,user_set) VALUES (?,?,?,?,?,?,1,?,?,?,?,?,?,?,1)""",
                     (sid, kind, name, target_url, handle, iv, int(bool(b.get("official", True))), int(bool(b.get("rail_filter", False))),
                      None, str(b.get("notes") or "")[:200], iso(self.clock()), lf, via))
        return next(s for s in self.sources() if s["id"] == sid)

    def update_source(self, sid: str, b: dict) -> dict | None:
        if not self.store.q("SELECT 1 FROM news_sources WHERE id=?", (sid,)):
            return None
        if "enabled" in b:
            self.store.x("UPDATE news_sources SET enabled=?, next_due_at=NULL, user_set=1 WHERE id=?", (int(bool(b["enabled"])), sid))
        if "fetch_via" in b:
            via = b["fetch_via"]
            kind = self.store.q("SELECT kind FROM news_sources WHERE id=?", (sid,))[0]["kind"]
            if via not in ("direct", "assistant") or (via == "assistant" and kind != "web"):
                raise ValueError("fetch_via: direct, or assistant (web pages only)")
            self.store.x("UPDATE news_sources SET fetch_via=?, next_due_at=NULL, fail_count=0, user_set=1 WHERE id=?", (via, sid))
        if "interval_min" in b:
            iv = int(b["interval_min"])
            if not 15 <= iv <= 1440:
                raise ValueError("interval: 15-1440 min")
            self.store.x("UPDATE news_sources SET interval_min=? WHERE id=?", (iv, sid))
        if "rail_filter" in b:
            self.store.x("UPDATE news_sources SET rail_filter=? WHERE id=?", (int(bool(b["rail_filter"])), sid))
        return next(s for s in self.sources() if s["id"] == sid)

    def delete_source(self, sid: str) -> bool:
        if not self.store.q("SELECT 1 FROM news_sources WHERE id=?", (sid,)):
            return False
        self.store.x("DELETE FROM news_items WHERE source_id=?", (sid,))
        self.store.x("DELETE FROM news_sources WHERE id=?", (sid,))
        return True

    def fetch_now(self, sid: str):
        self.store.x("UPDATE news_sources SET next_due_at=NULL WHERE id=?", (sid,))
        return self.tick(only=sid)

    # ---------------------------------------------------------------- X budget
    def x_usage_today(self) -> dict:
        day = self.clock().astimezone(timezone.utc).date().isoformat()        # X bills per UTC day
        r = self.store.q("SELECT units, cost_usd FROM news_usage WHERE day=? AND kind='x_posts'", (day,))
        u = self.store.q("SELECT units, cost_usd FROM news_usage WHERE day=? AND kind='x_users'", (day,))
        posts = r[0]["units"] if r else 0
        return {"day": day, "posts": posts, "users": u[0]["units"] if u else 0, "budget_posts": self.x_daily_posts,
                "cost_usd": round((r[0]["cost_usd"] if r else 0) + (u[0]["cost_usd"] if u else 0), 4),
                "configured": bool(self.x_token)}

    def _bill(self, kind: str, units: int, price: float):
        day = self.x_usage_today()["day"]
        self.store.x("""INSERT INTO news_usage VALUES (?,?,?,?) ON CONFLICT(day,kind) DO UPDATE SET
                        units=units+excluded.units, cost_usd=cost_usd+excluded.cost_usd""", (day, kind, units, units * price))

    # ---------------------------------------------------------------- fetching
    def auto_follow_zones(self) -> list[str]:
        """Turn on the press-release page of every zone your trips / watched trains / stations are in
        (unless you switched that page off yourself)."""
        zones = set()
        for uid in self.users():
            zones |= self.interests(uid)["zones"]
        turned = []
        for r in self.store.q("SELECT id, zone FROM news_sources WHERE zone IS NOT NULL AND enabled=0 AND user_set=0"):
            if r["zone"] in zones:
                self.store.x("UPDATE news_sources SET enabled=1, next_due_at=NULL WHERE id=?", (r["id"],))
                turned.append(r["id"])
        return turned

    def tick(self, only: str | None = None) -> dict:
        now = self.clock()
        summary = {"fetched": 0, "new": 0, "alerts": 0, "errors": 0}
        if not only:
            summary["auto_followed"] = self.auto_follow_zones()
        rows = self.store.q("SELECT * FROM news_sources WHERE enabled=1 AND fetch_via='direct'" + (" AND id=?" if only else "") +
                            " AND (next_due_at IS NULL OR next_due_at<=?)", ((only, iso(now)) if only else (iso(now),)))
        for s in rows:
            try:
                new = self._fetch_source(s)
                summary["fetched"] += 1
                summary["new"] += len(new)
                summary["alerts"] += self._alerts(s, new)
                self.store.x("UPDATE news_sources SET last_fetch_at=?, next_due_at=?, last_status='ok', last_detail=?, fail_count=0 WHERE id=?",
                             (iso(now), iso(now + timedelta(minutes=s["interval_min"])), f"{len(new)} new", s["id"]))
            except FetchError as e:
                summary["errors"] += 1
                if e.status == "robots_disallow" and s["kind"] == "web":
                    e = FetchError(e.status, str(e) + " Switch this page to 'browser assistant' to read it with your own browser.")
                fails = s["fail_count"] + 1
                wait = s["interval_min"] * (2 ** min(fails, 4)) if e.status in ("refused", "rate_limited", "unreachable") else s["interval_min"]
                if e.status in ("robots_disallow", "not_found"):
                    wait = 7 * 1440
                self.store.x("UPDATE news_sources SET last_fetch_at=?, next_due_at=?, last_status=?, last_detail=?, fail_count=? WHERE id=?",
                             (iso(now), iso(now + timedelta(minutes=min(wait, 7 * 1440))), e.status, str(e)[:300], fails, s["id"]))
        return summary

    def _fetch_source(self, s: dict) -> list[dict]:
        if s["kind"] == "x":
            items = self._fetch_x(s)
        else:
            items, etag, lm = self.fetcher.fetch(s["url"], s["kind"], s["etag"], s["last_modified"], s.get("link_filter"))
            self.store.x("UPDATE news_sources SET etag=?, last_modified=? WHERE id=?", (etag, lm, s["id"]))
            if items is None:
                return []
        new = self._store_items(s, items)
        if s["kind"] in ("web", "rss"):
            for it in new[:DETAILS_PER_RUN]:            # read the announcement itself (page or PDF), not just its title
                if not it["url"]:
                    continue
                try:
                    text, note = self.fetcher.fetch_detail(it["url"], it["title"])
                except FetchError as e:
                    self._set_detail(it, "", f"{e.status}: {e}", "failed")
                    continue
                self._set_detail(it, text, note, "ok" if text else "empty")
        return new

    def _set_detail(self, it: dict, text: str, note: str | None, status: str):
        if text:
            t = tag(f"{it['title']} {text}")
            it["text"], it["tags"] = text, t
            self.store.x("""UPDATE news_items SET text=?, trains=?, stations=?, categories=?, zones=?, detail_status=?, detail_note=?
                            WHERE id=?""", (text[:6000], json.dumps(t["trains"]), json.dumps(t["stations"]), json.dumps(t["categories"]),
                                            json.dumps(t["zones"]), status, note, it["id"]))
        else:
            self.store.x("UPDATE news_items SET detail_status=?, detail_note=? WHERE id=?", (status, note, it["id"]))

    def _store_items(self, s: dict, items: list) -> list[dict]:
        first = self.store.q("SELECT COUNT(*) n FROM news_items WHERE source_id=?", (s["id"],))[0]["n"] == 0
        baseline = first and s["kind"] == "web"     # a web page's existing links are the baseline, not "news"
        new = []
        for it in items:
            blob = f"{it.title} {it.text}"
            t = tag(blob)
            if s["rail_filter"] and not (is_railway(blob) or t["trains"] or t["stations"]):
                continue
            cur = self.store.x("""INSERT OR IGNORE INTO news_items(uid,source_id,title,text,url,author,published_at,fetched_at,trains,
                     stations,categories,zones,baseline,official) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                               (f"{s['id']}:{it.uid}", s["id"], it.title, it.text[:4000], it.url, it.author, it.published_at,
                                iso(self.clock()), json.dumps(t["trains"]), json.dumps(t["stations"]), json.dumps(t["categories"]),
                                json.dumps(t["zones"]), int(baseline), s["official"]))
            if cur.rowcount and not baseline:
                new.append({"id": cur.lastrowid, "title": it.title, "text": it.text, "url": it.url, "tags": t,
                            "published_at": it.published_at, "author": it.author})
        self.store.x("""DELETE FROM news_items WHERE source_id=? AND id NOT IN
                        (SELECT id FROM news_items WHERE source_id=? ORDER BY id DESC LIMIT 400)""", (s["id"], s["id"]))
        return new

    def _fetch_x(self, s: dict) -> list[Item]:
        if not self.x_token:
            raise FetchError("not_configured", "Set X_BEARER_TOKEN (official X API) to read X accounts")
        use = self.x_usage_today()
        per_call = 5                         # smallest page X allows: you pay only for posts actually returned
        if use["posts"] + per_call > self.x_daily_posts:
            raise FetchError("budget", f"Daily X budget of {self.x_daily_posts} posts reached (≈${use['cost_usd']:.2f} today)")
        cl = XClient(self.x_token, self.transport)
        uid = s["x_user_id"]
        if not uid:
            uid = cl.user_id(s["handle"])
            self._bill("x_users", 1, X_PRICE_USER)
            self.store.x("UPDATE news_sources SET x_user_id=? WHERE id=?", (uid, s["id"]))
        items = cl.recent_posts(uid, s["handle"], s["since_id"], per_call)
        self._bill("x_posts", len(items), X_PRICE_POST)
        if items:
            self.store.x("UPDATE news_sources SET since_id=? WHERE id=?", (max(items, key=lambda i: int(i.raw["id"])).raw["id"], s["id"]))
        return items

    # ---------------------------------------------------------------- alerts & reads
    def users(self) -> list[str]:
        return [r["id"] for r in self.store.q("SELECT id FROM users")]

    def _alerts(self, s: dict, new: list[dict]) -> int:
        if not new:
            return 0
        n = 0
        for uid in self.users():
            p = self.prefs(uid)
            if not p["announce_alerts"]:
                continue
            ints = self.interests(uid)
            for it in new:
                sc, why = score(it["tags"], ints)
                blob = (it["title"] + " " + it["text"]).lower()
                if not sc and any(k in blob for k in ints["keywords"]):
                    sc, why = 0.7, "matches your keyword"
                if sc < p["announce_min_score"]:
                    continue
                text = (f"📢 {s['name']}{' (official)' if s['official'] else ''}\n{it['title']}\n"
                        f"Why you're seeing this: {why}\n{it['url']}")
                nid = self.store.add_notification(uid, None, "ANNOUNCEMENT", f"news:{uid}:{it['id']}",
                                                  f"Announcement · {why}", text, {"news_id": it["id"], "score": sc}, False)
                if nid:
                    self.dispatcher.deliver(nid, uid, {"title": "Railway announcement", "text": text, "short": text[:200],
                                                       "is_demo": False, "template_params": []},
                                            None, p["digest_destinations"] if sc >= 0.8 else ["app"])
                    n += 1
        return n

    def items(self, user_id: str, relevant_only=False, category=None, q=None, limit=60, since_hours=None) -> list[dict]:
        sql = "SELECT n.*, s.name AS source_name, s.kind AS source_kind FROM news_items n JOIN news_sources s ON s.id=n.source_id WHERE n.baseline=0"
        args: list = []
        if since_hours:
            sql += " AND n.fetched_at>=?"
            args.append(iso(self.clock() - timedelta(hours=since_hours)))
        if q:
            sql += " AND (n.title LIKE ? OR n.text LIKE ?)"
            args += [f"%{q}%", f"%{q}%"]
        rows = self.store.q(sql + " ORDER BY COALESCE(n.published_at, n.fetched_at) DESC, n.id DESC LIMIT 500", tuple(args))
        ints = self.interests(user_id)
        out = []
        for r in rows:
            for k in ("trains", "stations", "categories", "zones"):
                r[k] = json.loads(r[k] or "[]")
            r["city_stations"] = tag(f"{r['title']} {r['text'] or ''}")["city_stations"]
            r["score"], r["why"] = score(r, ints)
            r["official"] = bool(r["official"])
            if category and category not in r["categories"]:
                continue
            if relevant_only and r["score"] < 0.4:
                continue
            out.append(r)
        return out[:limit]

    def ingest_external(self, source_id: str, name: str, items: list[dict], official=True) -> list[dict]:
        """Items produced elsewhere (e.g. NTES exceptional-train lists read by the browser assistant)."""
        if not self.store.q("SELECT 1 FROM news_sources WHERE id=?", (source_id,)):
            self.store.x("""INSERT INTO news_sources(id,kind,name,url,handle,interval_min,enabled,official,rail_filter,verified,notes,created_at)
                            VALUES (?,?,?,?,?,?,0,?,0,?,?,?)""", (source_id, "assist", name, None, None, 60, int(official),
                                                                  "read by your browser assistant", "", iso(self.clock())))
        s = self.store.q("SELECT * FROM news_sources WHERE id=?", (source_id,))[0]
        new = []
        for it in items:
            t = tag(f"{it['title']} {it.get('text', '')}")
            cur = self.store.x("""INSERT OR IGNORE INTO news_items(uid,source_id,title,text,url,author,published_at,fetched_at,trains,
                     stations,categories,zones,baseline,official) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,?)""",
                               (f"{source_id}:{it['guid']}", source_id, it["title"], it.get("text", ""), it.get("url"), "", None,
                                iso(self.clock()), json.dumps(t["trains"]), json.dumps(t["stations"]), json.dumps(t["categories"]),
                                json.dumps(t["zones"]), int(official)))
            if cur.rowcount:
                new.append({"id": cur.lastrowid, "title": it["title"], "text": it.get("text", ""), "url": it.get("url"), "tags": t})
        self._alerts(s, new)
        return new

    # ---------------------------------------------------------------- pages read by your browser assistant
    def due_assistant_pages(self, limit: int = 3) -> list[dict]:
        """Sources set to 'browser assistant' that are due; marks them as handed out."""
        now = self.clock()
        rows = self.store.q("""SELECT * FROM news_sources WHERE enabled=1 AND fetch_via='assistant' AND kind='web'
                               AND (next_due_at IS NULL OR next_due_at<=?) ORDER BY next_due_at LIMIT ?""", (iso(now), limit))
        for r in rows:          # lease: the next round only gets it again after its interval (or 30 min on silence)
            self.store.x("UPDATE news_sources SET next_due_at=? WHERE id=?",
                         (iso(now + timedelta(minutes=max(30, r["interval_min"]))), r["id"]))
        return rows

    def ingest_page(self, source_id: str, capture: dict) -> list[dict]:
        """A list page read by the browser assistant -> new announcement items (+ detail pages to read next)."""
        s = self.store.q("SELECT * FROM news_sources WHERE id=?", (source_id,))
        if not s:
            raise ValueError("unknown news source")
        s = s[0]
        now = self.clock()
        if capture.get("status") != "ok":
            self.store.x("UPDATE news_sources SET last_fetch_at=?, last_status=?, last_detail=?, fail_count=fail_count+1 WHERE id=?",
                         (iso(now), capture.get("status"), (capture.get("reason") or "")[:300], source_id))
            return []
        items, seen = [], set()
        for ln in capture.get("links") or []:
            href, title = (ln.get("h") or "").strip(), re.sub(r"\s+", " ", ln.get("t") or "").strip()
            if not href.startswith(("http://", "https://")) or href in seen or len(title) < 12 or SKIP_TEXT.match(title):
                continue
            if s.get("link_filter") and not re.search(s["link_filter"], href, re.I):
                continue
            seen.add(href)
            items.append(Item(guid=href, title=title[:300], url=href))
        new = self._store_items(s, items[:200])
        self.store.x("""UPDATE news_sources SET last_fetch_at=?, next_due_at=?, last_status='ok', last_detail=?, fail_count=0 WHERE id=?""",
                     (iso(now), iso(now + timedelta(minutes=s["interval_min"])), f"{len(new)} new (browser assistant)", source_id))
        if self.assist:
            for it in new[:DETAILS_PER_RUN]:
                self.assist.queue_page_job({"url": it["url"], "source_id": source_id, "item_id": str(it["id"]), "title": it["title"][:120]})
        self._alerts(s, new)            # alert on the title now; the detail may raise relevance later
        return new

    def ingest_detail(self, item_id: int, capture: dict) -> dict | None:
        r = self.store.q("""SELECT n.*, s.name AS source_name FROM news_items n JOIN news_sources s ON s.id=n.source_id
                            WHERE n.id=?""", (item_id,))
        if not r:
            return None
        it = dict(r[0])
        if capture.get("status") != "ok":
            self._set_detail(it, "", f"{capture.get('status')}: {(capture.get('reason') or '')[:200]}", "failed")
            return it
        if capture.get("pdf_b64"):
            try:
                data = base64.b64decode(capture["pdf_b64"], validate=True)
            except (ValueError, TypeError):
                data = b""
            text, note = pdf_to_text(data)
        else:
            text, note = focus(capture.get("text") or "", it["title"]), None
        self._set_detail(it, text, note, "ok" if text else "empty")
        s = self.store.q("SELECT * FROM news_sources WHERE id=?", (it["source_id"],))[0]
        self._alerts(s, [{"id": it["id"], "title": it["title"], "text": it.get("text") or "", "url": it["url"],
                          "tags": it.get("tags") or tag(it["title"])}])
        return it
