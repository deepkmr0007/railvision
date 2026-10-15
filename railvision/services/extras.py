"""Personal extras: punctuality history, daily digest, helplines."""
from __future__ import annotations

import statistics
from datetime import timedelta

from ..core.timeutil import fmt_hm, iso, now_ist, parse_iso

HELPLINES = [
    {"number": "139", "name": "Rail Madad — integrated railway helpline", "use": "Enquiry, complaints, medical help, security on board",
     "hi": "रेल मदद हेल्पलाइन — पूछताछ, शिकायत, चिकित्सा, सुरक्षा"},
    {"number": "182", "name": "RPF security helpline", "use": "Theft, harassment, threats to safety on trains and at stations",
     "hi": "आरपीएफ सुरक्षा हेल्पलाइन"},
    {"number": "112", "name": "National emergency number", "use": "Police, fire, ambulance anywhere in India", "hi": "राष्ट्रीय आपातकालीन नंबर"},
]
HELP_LINKS = [{"name": "Rail Madad (complaints)", "url": "https://railmadad.indianrailways.gov.in/"},
              {"name": "NTES (running status)", "url": "https://enquiry.indianrail.gov.in/mntes/"},
              {"name": "IRCTC (booking, refunds)", "url": "https://www.irctc.co.in/"}]


def punctuality(store, train: str, station: str | None = None, days: int = 30, include_demo: bool = False) -> dict:
    """Delay history from the observations RAILVISION has stored (latest report per run and station)."""
    since = (now_ist().date() - timedelta(days=days)).isoformat()
    rows = store.q("""SELECT service_date, station_code, kind, delay_min, source_time FROM observations
                      WHERE train_number=? AND service_date>=? AND delay_min IS NOT NULL AND kind IN ('ARRIVED','DEPARTED','PASSED')
                      AND is_demo=?""" + (" AND station_code=?" if station else ""),
                   (train, since, int(include_demo), *([station] if station else [])))
    latest: dict[tuple, tuple] = {}
    for r in rows:
        k = (r["service_date"], r["station_code"])
        pref = 1 if r["kind"] == "ARRIVED" else 0
        cur = latest.get(k)
        if not cur or (pref, r["source_time"]) > (cur[0], cur[1]):
            latest[k] = (pref, r["source_time"], r["delay_min"])
    per_station: dict[str, list[tuple[str, int]]] = {}
    for (sd, st), (_, _, d) in latest.items():
        per_station.setdefault(st, []).append((sd, d))

    def summ(vals: list[int]) -> dict:
        vals = sorted(vals)
        return {"runs": len(vals), "avg": round(statistics.mean(vals), 1), "median": statistics.median(vals),
                "p90": vals[min(len(vals) - 1, int(round(0.9 * (len(vals) - 1))))], "max": vals[-1],
                "on_time_pct": round(100 * sum(1 for v in vals if v <= 5) / len(vals))}
    stations = {st: {**summ([d for _, d in v]), "recent": sorted(v)[-10:]} for st, v in per_station.items()}
    allv = [d for v in per_station.values() for _, d in v]
    return {"train": train, "station": station, "days": days, "overall": summ(allv) if allv else None, "stations": stations,
            "note": "Built only from reports RAILVISION has collected on your computer; few runs = rough picture."}


class DigestService:
    """One message a day (default 07:00 IST): your trips, watched trains, and announcements that concern you."""

    def __init__(self, store, dispatcher, news, journeys, assist=None, clock=now_ist):
        self.store, self.dispatcher, self.news, self.journeys, self.assist, self.clock = store, dispatcher, news, journeys, assist, clock

    def compose(self, user_id: str) -> str:
        now = self.clock()
        lines = [f"🌅 RAILVISION daily digest · {now:%a %d %b}"]
        trips = [j for j in self.journeys.list(user_id, include_done=False)]
        soon = []
        for j in trips:
            ev = j.get("last_eval") or {}
            dep = parse_iso(ev["dep"]) if ev.get("dep") else None
            if dep and dep - now <= timedelta(hours=48) or j["start_date"] <= (now.date() + timedelta(days=1)).isoformat():
                soon.append((j, ev, dep))
        if soon:
            lines.append("\n🧳 Your trips")
            for j, ev, dep in soon:
                pnr = j.get("last_pnr") or {}
                cur = ", ".join((p.get("current") or {}).get("raw") or "?" for p in pnr.get("passengers", []))
                lines.append(f"• {j['train']} {j['board']}→{j['dest']} · " + (f"dep {dep:%a %H:%M} ({ev.get('dep_basis')})" if dep else
                                                                                f"start date {j['start_date']}")
                             + (f" · PNR now {cur}" if cur else ""))
        if self.assist:
            ws = [w for w in self.assist.list_watch(user_id) if w["active"]]
            if ws:
                lines.append("\n👀 Watch list")
                for w in ws[:10]:
                    lines.append(f"• {w['train']} (start {w['start_date']}): {w.get('last_headline') or 'not checked yet'}")
        rel = [n for n in self.news.items(user_id, relevant_only=True, since_hours=24, limit=8) if n["score"] >= 0.6]
        if rel:
            lines.append("\n📢 Announcements that concern you (24 h)")
            for n in rel[:6]:
                lines.append(f"• {n['title'][:140]} — {n['source_name']} ({n['why']})")
        if len(lines) == 1:
            lines.append("Nothing needs your attention today. Add a trip in My Journeys to get reminders.")
        lines.append("\nHelpline 139 · Security 182")
        return "\n".join(lines)

    def tick(self) -> int:
        now = self.clock()
        sent = 0
        for u in self.store.q("SELECT id FROM users"):
            uid = u["id"]
            p = self.news.prefs(uid)
            if not p["digest_enabled"]:
                continue
            hh, mm = map(int, p["digest_time"].split(":"))
            due = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if not (due <= now < due + timedelta(hours=3)):
                continue
            if not (self.journeys.list(uid, include_done=False) or (self.assist and self.assist.list_watch(uid))
                    or self.news.items(uid, relevant_only=True, since_hours=24, limit=1)):
                continue                    # nothing personal to say — no daily noise
            sent += self.send(uid, key=f"digest:{uid}:{now.date().isoformat()}")
        return sent

    def send(self, uid: str, key: str | None = None) -> int:
        text = self.compose(uid)
        nid = self.store.add_notification(uid, None, "DIGEST", key or f"digest:{uid}:{iso(self.clock())}", "Daily digest", text, {}, False)
        if not nid:
            return 0
        self.dispatcher.deliver(nid, uid, {"title": "RAILVISION daily digest", "text": text, "short": text[:200], "is_demo": False,
                                           "template_params": []}, None, self.news.prefs(uid)["digest_destinations"])
        return 1

