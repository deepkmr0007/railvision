"""Build the standalone dashboard (dist/railvision_dashboard.html) with an embedded DEMO replay.

Every number in the replay is produced by the real backend code (demo provider -> crossing
engine -> status builder -> tracking engine -> chatbot) at fixed scenario times, so the
offline dashboard shows exactly what the server would show. All of it is SYNTHETIC.

    python tools/export_demo_snapshot.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from railvision import stations as stn  # noqa: E402
from railvision.chatbot.engine import ChatEngine, summarize_vacancy  # noqa: E402
from railvision.core.crossing import evaluate_crossing  # noqa: E402
from railvision.core.status import build_status  # noqa: E402
from railvision.core.timeutil import IST, iso  # noqa: E402
from railvision.delivery.channels import (Dispatcher, EventBus, InApp, Telegram)  # noqa: E402
from railvision.delivery.whatsapp import (ChannelCopyQueue, WhatsAppCloudClient, WhatsAppConfig,  # noqa: E402
                                          WhatsAppGroup, WhatsAppPersonal)
from railvision.providers.demo import TRAINS, DemoProvider  # noqa: E402
from railvision.providers.registry import SOURCE_MATRIX, ProviderRegistry  # noqa: E402
from railvision.services.store import Store  # noqa: E402
from railvision.services.tracker import TrackingEngine  # noqa: E402


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def slim_crossing(c: dict) -> dict:
    keep = ("station_code", "station_name", "event_class", "verification", "crossed", "headline", "scheduled_time",
            "event_time", "est_earliest", "est_latest", "est_central", "confidence", "method", "source_id",
            "source_time", "evidence", "warnings", "is_demo")
    return {k: c[k] for k in keep}


def assist_replay() -> dict | None:
    """Run the REAL browser assistant (server + agent + Chromium) against the LOCAL MOCK site and keep
    what the watch-list view needs. Skipped when Playwright is not installed."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    import base64
    import io
    import os
    import threading
    from http.server import ThreadingHTTPServer
    sys.path.insert(0, str(ROOT / "tests" / "mock_sites"))
    from mock_server import TRAINS, make_server
    from railvision.api.app import App
    from railvision.api.server import make_handler
    from railvision.assist.agent import Agent, Client, Pacer
    from railvision.assist.recipes import DEFAULT_RECIPES
    from railvision.core.timeutil import now_ist
    mock = make_server(0)
    mbase = f"http://127.0.0.1:{mock.server_address[1]}"
    os.environ["RAILVISION_MODE"] = "live"
    app = App(store=Store(), registry=ProviderRegistry("live", []), logger=lambda *_: None, assist=True)
    os.environ["RAILVISION_MODE"] = "demo"
    app.assist.pacing.update(min_gap_s=0.2, jitter_s=0.1)
    uid, tok = app.store.create_user("replay")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    cl = Client(f"http://127.0.0.1:{srv.server_address[1]}", tok)
    for name, path in (("ntes_status", "/ntes-a/"), ("irctc_berth", "/irctc/"), ("ntes_cancelled", "/ntes-a/"),
                       ("ntes_rescheduled", "/ntes-a/"), ("ntes_diverted", "/ntes-a/")):      # mock site only, never the real ones
        cl.call("PUT", f"/api/assist/recipes/{name}", {**DEFAULT_RECIPES[name].to_dict(), "start_url": mbase + path})
    today = now_ist().date().isoformat()
    for i, t in enumerate(sorted(TRAINS)[:10]):
        cl.call("POST", "/api/assist/watch", {"train": t, "start_date": today, "interval_min": 15 if i % 3 else 10,
                                              "ref_station": "RE" if i == 0 else None, "end_station": "DLI" if i == 0 else None,
                                              "berth_checks": [{"station": "AWR", "class": "3A"}] if i == 1 else []})
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        Agent(cl, b.new_context(), headed=False, log=lambda *_: None, pacer=Pacer(min_gap_s=0.2, jitter_s=0.1, floor_s=0.2)).round()
        b.close()
    status = cl.call("GET", "/api/assist/status")
    caps = cl.call("GET", "/api/assist/captures?limit=40")["items"]
    shots = {}
    try:
        from PIL import Image
    except ImportError:
        Image = None
    for c in caps:
        if not c["has_shot"]:
            continue
        raw = app.assist.screenshot(uid, c["id"])
        if Image:
            im = Image.open(io.BytesIO(raw)).convert("RGB")
            im.thumbnail((900, 1400))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=50, optimize=True)
            raw = buf.getvalue()
        shots[c["id"]] = "data:image/jpeg;base64," + base64.b64encode(raw).decode()
    for w in status["watch"]:
        w["train_name"] = TRAINS.get(w["train"])
    srv.shutdown()
    mock.shutdown()
    status["agent"]["reason"] = "Local mock site (test run) — not the real NTES/IRCTC pages"
    return {**status, "captures": caps, "shots": shots, "now": iso(now_ist()), "mock": True}


def personal_replay() -> dict:
    """Run the REAL journey engine on demo trains and the REAL announcement pipeline on MOCK feeds."""
    import json as _json
    import os
    from datetime import date as _date
    from urllib.parse import parse_qs, urlsplit
    from railvision.api.app import App
    from railvision.core.timeutil import now_ist
    from railvision.services.extras import HELP_LINKS, HELPLINES, punctuality

    today = now_ist().date()
    T = lambda h, m, d=0: datetime.combine(today + timedelta(days=d), datetime.min.time(), tzinfo=IST).replace(hour=h, minute=m)  # noqa: E731
    clock = Clock(T(5, 0))
    rss = """<?xml version="1.0"?><rss version="2.0"><channel><title>PIB (mock)</title>
<item><title>Festival special trains between Jaipur and Delhi to run from next week (mock feed item)</title><link>https://pib.gov.in/mock/1</link></item>
<item><title>Ministry of Ayush opens research portal (mock feed item)</title><link>https://pib.gov.in/mock/2</link></item>
<item><title>Line block between Alwar and Rewari on Sunday: 90001 to run 40 min late (mock feed item)</title><link>https://pib.gov.in/mock/3</link></item>
</channel></rss>"""
    zone1 = '<html><body><a href="/n/1.pdf">Commissioning of new foot over bridge at Bandikui station (mock notice)</a></body></html>'
    zone2 = zone1.replace("</body>", '<a href="/n/2.pdf">Fog safety drive: trains in the Delhi area may be regulated after 22:00 (mock notice)</a></body>')

    class Net:
        def __init__(self):
            self.zone = zone1

        def __call__(self, url, headers):
            u = urlsplit(url)
            if u.path == "/robots.txt":
                return 404, {}, b""
            if u.netloc == "pib.gov.in":
                return 200, {}, rss.encode()
            if u.netloc == "nwr.mock.example":
                return 200, {}, self.zone.encode()
            if u.netloc == "api.x.com":
                if "/users/by/username/" in u.path:
                    return 200, {}, b'{"data":{"id":"1"}}'
                return 200, {}, _json.dumps({"data": [{"id": "77", "created_at": now_ist().isoformat(),
                                                       "text": "Mock post: Additional AC 3-tier coach attached to 90001 from today. #MockFeed"}]}).encode()
            return 404, {}, b""
    net = Net()
    os.environ["RAILVISION_MODE"] = "demo"
    prov = DemoProvider(clock=clock, anchor=T(6, 20))
    app = App(store=Store(), registry=ProviderRegistry("demo", [prov]), clock=clock, logger=lambda *_: None, news_transport=net)
    app.news_enabled = True             # mock feeds only (see Net above)
    uid, _ = app.store.create_user("replay")
    J = app.journeys
    a = J.add(uid, {"train": "90001", "start_date": today.isoformat(), "board": "AWR", "dest": "DLI", "cls": "3A",
                    "coach": "B2", "berth": "34", "buffer_min": 45, "alarm_min": 30})
    J.add(uid, {"train": "90003", "start_date": today.isoformat(), "board": "BKI", "dest": "DLI", "cls": "SL", "buffer_min": 30})
    c = J.add(uid, {"train": "90001", "start_date": (today + timedelta(days=1)).isoformat(), "board": "JP", "dest": "DLI",
                    "cls": "2A", "pnr": "4512345678", "buffer_min": 60, "berth_check": True})
    J.apply_pnr(uid, {"pnr": "4512345678", "chart_prepared": False, "passengers": [
        {"n": 1, "booking": {"raw": "WL/18", "status": "WL"}, "current": {"raw": "RAC/6", "status": "RAC"}}]})
    # a week of past runs for delay history (synthetic)
    for k in range(1, 8):
        sd = today - timedelta(days=k)
        clock.t = T(23, 0, -k)
        for tn in ("90001", "90003"):
            app.store.save_observations(prov.get_train_status(tn, sd))
    news = app.news
    news.store.x("UPDATE news_sources SET name='PIB press releases (mock feed)' WHERE id='pib_en'")
    src = news.add_source({"kind": "web", "url": "https://nwr.mock.example/notices", "name": "Zonal notices (mock page)"})
    news.x_token = "mock"
    news.update_source("x_railminindia", {"enabled": True})
    news.store.x("UPDATE news_sources SET name='Ministry of Railways on X (mock API)' WHERE id='x_railminindia'")
    t = T(5, 0)
    while t <= T(9, 15):
        clock.t = t
        if t == T(6, 0):
            net.zone = zone2
            news.store.x("UPDATE news_sources SET next_due_at=NULL WHERE id=?", (src["id"],))
        if t == T(7, 30):
            news.ingest_external("ntes_cancelled", "NTES Cancelled trains (mock list)",
                                 [{"guid": "c:90002", "title": "90002 DEMO CANCELLED SPECIAL — Cancelled (mock list)",
                                   "text": "Cancelled (NTES list, mock).", "url": "https://enquiry.indianrail.gov.in/mntes/"}])
        app.housekeeping()
        t += timedelta(minutes=5)
    js = J.list(uid)
    punct = {}
    for j in js:
        punct[f"{j['train']}|{j['board']}"] = punctuality(app.store, j["train"], j["board"], include_demo=True)
    rem = [{**n, "at": n["created_at"]} for n in app.store.list_notifications(uid, 100) if n["type"] == "JOURNEY_REMINDER"]
    srcs = news.sources()
    from railvision.news.service import SUGGESTED_WEB
    return {"now": iso(clock()), "journeys": js, "reminders": rem, "news": news.items(uid, limit=40),
            "sources": {"sources": srcs, "suggested": SUGGESTED_WEB, "x": news.x_usage_today()},
            "help": {"helplines": HELPLINES, "links": HELP_LINKS}, "prefs": news.prefs(uid),
            "digest": app.digest.compose(uid), "punct": punct, "mock_feeds": True}


def main():
    day = datetime(2026, 10, 14, tzinfo=IST)
    start, end, step = day.replace(hour=6, minute=15), day.replace(hour=12, minute=15), timedelta(minutes=15)
    clock = Clock(start)
    prov = DemoProvider(clock=clock, anchor=day.replace(hour=6, minute=20))
    reg = ProviderRegistry("demo", [prov])
    store = Store()
    bus = EventBus()
    wa_cfg = WhatsAppConfig()
    disp = Dispatcher(store, [InApp(bus), WhatsAppPersonal(store, WhatsAppCloudClient(wa_cfg)), WhatsAppGroup(wa_cfg),
                              ChannelCopyQueue(store), Telegram(token="")])
    eng = TrackingEngine(store, reg, disp, "demo", "", clock)
    uid, _ = store.create_user("Demo traveller")

    # --- tracking tasks (created at start, ticked every 5 min across the replay)
    t1 = eng.draft_task(uid, "90001", day.date(), "RE", "DLI", 15,
                        ["app", "whatsapp", "telegram", "whatsapp_group", "whatsapp_channel"], delay_threshold_min=20)
    eng.confirm(t1["id"], uid)
    t2 = eng.draft_task(uid, "90003", day.date(), "RE", None, 30, ["app"])
    eng.confirm(t2["id"], uid)

    frames, notif_seen = [], set()
    notifications = []
    t = start
    tick_t = start
    while t <= end:
        while tick_t <= t:
            clock.t = tick_t
            eng.tick()
            for n in store.list_notifications(uid, 500):
                if n["id"] not in notif_seen:
                    notif_seen.add(n["id"])
                    notifications.append({"id": n["id"], "at": iso(tick_t), "type": n["type"], "title": n["title"],
                                          "body": n["body"], "task_id": n["task_id"],
                                          "deliveries": [{"destination": d["destination"], "status": d["status"],
                                                          "detail": d["detail"]} for d in store.deliveries(n["id"])]})
            tick_t += timedelta(minutes=5)
        clock.t = t
        fr = {"t": iso(t), "trains": {}}
        for no in TRAINS:
            r = prov.get_train_route(no, day.date())
            obs = prov.get_train_status(no, day.date())
            st = build_status(r, obs, day.date(), t)
            fr["trains"][no] = {"status": st, "crossings": {
                s.code: slim_crossing(evaluate_crossing(r, obs, s.code, t, day.date()).to_dict()) for s in r.stops}}
        fr["tasks"] = [{k: v for k, v in x.items() if k != "last_result"} | {"last_crossing": (x.get("last_result") or {}).get("crossing", {}).get("headline")}
                       for x in store.list_tasks(uid)]
        frames.append(fr)
        t += step

    clock.t = day.replace(hour=9, minute=15)
    routes = {}
    for no in TRAINS:
        r = prov.get_train_route(no, day.date())
        routes[no] = {"train_number": no, "train_name": r.train_name, "source": r.source_id, "is_demo": True,
                      "stops": [{**s.__dict__, "lat": getattr(stn.STATIONS.get(s.code), "lat", None),
                                 "lon": getattr(stn.STATIONS.get(s.code), "lon", None)} for s in r.stops]}
    boards = {c: prov.get_station_board(c, clock.t - timedelta(hours=1), clock.t + timedelta(hours=4))
              for c in ("JP", "AWR", "RE", "GGN", "DLI")}
    vac = {}
    for frm, cls in (("JP", "3A"), ("BKI", "3A"), ("AWR", "3A"), ("JP", "2A"), ("JP", "SL")):
        v = prov.get_chart_vacancy("90001", day.date(), frm, cls).to_dict()
        v["summary"] = summarize_vacancy(v)
        vac[f"{frm}|{cls}"] = v

    chat = ChatEngine(eng, reg, store)
    script = ["where is 90001", "does 90001 stop at Rewari?", "has it crossed Rewari?", "90001 kahan hai",
              "रेवाड़ी पार हुई?", "track 90001 from Rewari to Delhi every 15 min", "yes",
              "alert me if the delay exceeds 30 minutes", "change the interval to 30 minutes", "pause this tracking",
              "chart vacancy 90001 3A from Jaipur Junction", "where is 90002", "has 90003 crossed Rewari?",
              "where is 12951", "trains between Jaipur Junction and Alwar"]
    transcript = []
    for q in script:
        res = chat.handle("demo-session", uid, q)
        transcript.append({"q": q, "reply": res["reply"], "intent": res["intent"]})

    snap = {"generated_from": "railvision backend v2.0 demo provider (SYNTHETIC)", "service_date": day.date().isoformat(),
            "frames": frames, "default_frame": 11, "routes": routes, "boards": boards, "vacancy": vac,
            "notifications": notifications, "chat": transcript,
            "sources": {"mode": "demo", "matrix": SOURCE_MATRIX, "health": reg.health(),
                        "destinations": disp.catalogue(), "checked": "2026-10-14"},
            "between": prov.find_trains_between("JP", "DLI", day.date()),
            "assist": assist_replay(),
            "personal": personal_replay()}
    data = json.dumps(snap, ensure_ascii=False, separators=(",", ":"), default=str)
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    marker = "/*RV_SNAPSHOT*/null"
    assert marker in html
    out = ROOT / "dist" / "railvision_dashboard_v2_2.html"
    out.parent.mkdir(exist_ok=True)
    page = html.replace(marker, data.replace("</", "<\\/"))
    if snap["assist"]:
        page = page.replace("<title>RAILVISION AI</title>", "<title>RAILVISION AI v2.2</title>", 1)
    out.write_text(page, encoding="utf-8")
    print(f"frames={len(frames)} notifications={len(notifications)} chat={len(transcript)} "
          f"assist={'yes' if snap['assist'] else 'no (playwright missing)'} size={len(data)/1024:.0f} KB -> {out}")


if __name__ == "__main__":
    main()
