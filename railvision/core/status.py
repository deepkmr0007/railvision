"""Train running-status builder: turns route + observations into a dashboard-ready record.

Actual (source-reported) and timetable-derived values are kept in separate fields so the
UI can never present an estimate as a reported location.
"""
from __future__ import annotations

from datetime import datetime

from .crossing import AT_STATION_KINDS, _positions, clean_observations, effective_codes
from .models import ObsKind, Route
from .timeutil import iso, minutes


def build_status(route: Route, observations: list, service_date, now: datetime,
                 stale_after_min: float = 30.0) -> dict:
    obs = [o for o in clean_observations(route, observations) if o.service_date == service_date]
    codes, warns = effective_codes(route, obs)
    positions = _positions(codes, route, obs, warns)
    alerts: list[dict] = []
    for o in obs:
        if o.kind in (ObsKind.CANCELLED, ObsKind.DIVERTED, ObsKind.SHORT_TERMINATED, ObsKind.RESCHEDULED):
            alerts.append({"type": o.kind.value, "station": o.station_code, "source": o.source_id,
                           "source_time": iso(o.source_time), "detail": o.payload.get("detail", "")})

    latest = max(positions, key=lambda p: (p.obs.event_time, p.idx)) if positions else None
    furthest = max(positions, key=lambda p: (p.idx, p.obs.event_time)) if positions else None
    if latest and furthest and furthest is not latest and furthest.idx > latest.idx:
        warns.append("Conflicting observations: furthest-reported station is not the most recent report.")
    cur = furthest

    actual: dict[str, dict] = {}
    for p in positions:
        if p.idx == int(p.idx):
            a = actual.setdefault(codes[int(p.idx)], {})
            a[p.obs.kind.value.lower()] = iso(p.obs.event_time)
            if p.obs.delay_min is not None:
                a["delay_min"] = p.obs.delay_min
            if p.obs.payload.get("platform"):
                a["platform"] = p.obs.payload["platform"]

    total_km = (route.stops[-1].km - route.stops[0].km) or 1.0
    stops_out, completed, remaining = [], [], []
    progress_idx = cur.idx if cur else -1
    for i, code in enumerate(codes):
        st = route.stop(code)
        rec = {
            "seq": i, "code": code, "name": st.name if st else code, "km": st.km if st else None,
            "halt": st.halt if st else None, "day": st.day if st else None,
            "sched_arr": iso(st.arr_dt(service_date)) if st else None,
            "sched_dep": iso(st.dep_dt(service_date)) if st else None,
            "platform": (actual.get(code, {}).get("platform") or (st.platform if st else None)),
            "actual": actual.get(code),
            "status": "PASSED" if i < progress_idx or (i == progress_idx and cur and cur.obs.kind != ObsKind.ARRIVED)
                      else ("AT_STATION" if i == progress_idx else "UPCOMING"),
        }
        stops_out.append(rec)
        (completed if rec["status"] == "PASSED" else remaining).append(code)

    state = "UNKNOWN"
    if any(a["type"] == "CANCELLED" for a in alerts):
        state = "CANCELLED"
    elif cur is None:
        first = route.stops[0]
        dep = first.dep_dt(service_date)
        state = "NOT_STARTED" if dep and now < dep else "NO_REPORTS"
    elif int(cur.idx) == len(codes) - 1 and cur.obs.kind == ObsKind.ARRIVED:
        state = "COMPLETED"
    elif cur.obs.kind == ObsKind.ARRIVED:
        state = "AT_STATION"
    else:
        state = "RUNNING"
    if any(a["type"] == "DIVERTED" for a in alerts) and state not in ("CANCELLED", "COMPLETED"):
        state += "_DIVERTED"

    prev_stop = next_stop = None
    if cur:
        ci = int(cur.idx)
        prev_stop = next((codes[j] for j in range(ci, -1, -1) if (route.stop(codes[j]) or None) and route.stop(codes[j]).halt
                          and stops_out[j]["status"] != "UPCOMING"), None)
        next_stop = next((codes[j] for j in range(ci + (0 if state == "AT_STATION" else 1), len(codes))
                          if route.stop(codes[j]) and route.stop(codes[j]).halt and stops_out[j]["status"] == "UPCOMING"), None)
    km_done = None
    if cur:
        from .crossing import _km_at
        km_done = _km_at(route, codes, cur.idx)
    progress = round(100 * max(0.0, (km_done - route.stops[0].km)) / total_km, 1) if km_done is not None else 0.0
    if state == "COMPLETED":
        progress = 100.0

    last_src = max(obs, key=lambda o: o.source_time) if obs else None
    age = minutes(now - last_src.source_time) if last_src else None
    delay = next((p.obs.delay_min for p in sorted(positions, key=lambda p: (p.idx, p.obs.event_time), reverse=True)
                  if p.obs.delay_min is not None), None)
    return {
        "train_number": route.train_number, "train_name": route.train_name,
        "service_date": service_date.isoformat(), "state": state,
        "is_demo": route.is_demo, "data_label": "DEMO — SYNTHETIC DATA, NOT LIVE" if route.is_demo else "LIVE SOURCE DATA",
        "last_event": ({"kind": cur.obs.kind.value, "station": cur.obs.station_code, "km_after": cur.obs.km_after,
                        "time": iso(cur.obs.event_time), "basis": "SOURCE_REPORTED"} if cur else None),
        "prev_stop": prev_stop, "next_stop": next_stop, "delay_min": delay,
        "progress_pct": progress, "completed": completed, "remaining": remaining,
        "stops": stops_out, "alerts": alerts, "warnings": warns,
        "freshness": {"source": last_src.source_id if last_src else route.source_id,
                      "source_time": iso(last_src.source_time) if last_src else None,
                      "retrieved_at": iso(last_src.retrieved_at) if last_src else None,
                      "age_min": round(age, 1) if age is not None else None,
                      "stale": bool(age is not None and age > stale_after_min)},
        "confidence": (0.0 if not cur else (0.95 if age is not None and age <= stale_after_min else 0.6)),
    }
