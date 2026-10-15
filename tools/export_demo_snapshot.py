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
            "between": prov.find_trains_between("JP", "DLI", day.date())}
    data = json.dumps(snap, ensure_ascii=False, separators=(",", ":"), default=str)
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    marker = "/*RV_SNAPSHOT*/null"
    assert marker in html
    out = ROOT / "dist" / "railvision_dashboard.html"
    out.parent.mkdir(exist_ok=True)
    out.write_text(html.replace(marker, data.replace("</", "<\\/")), encoding="utf-8")
    print(f"frames={len(frames)} notifications={len(notifications)} chat={len(transcript)} size={len(data)/1024:.0f} KB -> {out}")


if __name__ == "__main__":
    main()
