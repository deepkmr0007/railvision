"""Non-stop station crossing engine (the critical feature).

Decides, for one reference station, which of these is true and with what evidence:

  SCHEDULED_HALT       train is booked to stop there (actual times when reported)
  CONFIRMED_PASSAGE    a non-halt station, crossing proven by the source:
                         - VERIFIED_LIVE: the source reported a passage/departure AT that station
                         - DERIVED: the source reported the train at a LATER station on the route,
                           so it must have passed; time is a bracketed range, not invented
  ESTIMATED_CROSSING   only upstream evidence; time is projected from the latest verified
                       observation + timetable run-time, always given as a range
  NOT_VERIFIED         evidence insufficient (no data / too stale) — "Crossing not verified"
  NOT_ON_ROUTE         station absent from the (possibly diverted / short-terminated) route
  CANCELLED            service cancelled for the date

Rule that is never broken: a scheduled time passing is NOT evidence of crossing.
Estimates never set `crossed=True`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .models import (CrossingResult, EventClass, LiveObservation, ObsKind, Route,
                     ScheduledStop, Verification)
from .timeutil import fmt_hm, minutes

ENGINE_VERSION = "crossing-engine/1.2"

AT_STATION_KINDS = (ObsKind.ARRIVED, ObsKind.DEPARTED, ObsKind.PASSED)


@dataclass
class EngineConfig:
    stale_after_min: float = 30.0          # source record older than this is "stale"
    unusable_after_min: float = 120.0      # older than this: too old to estimate from
    max_estimate_km: float = 450.0         # don't project further than this
    base_uncertainty_min: float = 3.0
    uncertainty_per_100km: float = 9.0
    late_skew: float = 1.6                 # trains lose time more easily than they gain it
    stale_uncertainty_per_min: float = 0.3
    out_of_order_tolerance_min: float = 2.0


class MixedDataError(ValueError):
    """Raised when demo and live observations are combined — forbidden."""


@dataclass
class _Pos:
    idx: float                 # fractional route index (POSITION obs between stations)
    obs: LiveObservation


def clean_observations(route: Route, observations: list[LiveObservation]) -> list[LiveObservation]:
    """Filter to this train/service, drop exact duplicates, order by event time then source time."""
    if observations:
        demo_flags = {o.is_demo for o in observations} | {route.is_demo}
        if len(demo_flags) > 1:
            raise MixedDataError("demo and live data must never be combined")
    seen, out = set(), []
    for o in observations:
        if o.train_number != route.train_number:
            continue
        k = o.dedupe_key()
        if k in seen:
            continue
        seen.add(k)
        out.append(o)
    out.sort(key=lambda o: ((o.event_time or o.source_time), o.source_time))
    return out


def effective_codes(route: Route, obs: list[LiveObservation]) -> tuple[list[str], list[str]]:
    """Station sequence after diversions / short termination. Returns (codes, warnings)."""
    codes = [s.code for s in route.stops]
    warns: list[str] = []
    diversions = [o for o in obs if o.kind == ObsKind.DIVERTED and o.payload.get("route")]
    if diversions:
        latest = max(diversions, key=lambda o: o.source_time)
        codes = list(latest.payload["route"])
        warns.append(f"Train reported DIVERTED by {latest.source_id} at {fmt_hm(latest.source_time)}; using diverted route.")
    shorts = [o for o in obs if o.kind == ObsKind.SHORT_TERMINATED and o.station_code in codes]
    if shorts:
        st = max(shorts, key=lambda o: o.source_time)
        codes = codes[: codes.index(st.station_code) + 1]
        warns.append(f"Train reported SHORT-TERMINATED at {st.station_code}.")
    return codes, warns


def _positions(codes: list[str], route: Route, obs: list[LiveObservation], warns: list[str]) -> list[_Pos]:
    out: list[_Pos] = []
    idx_of = {c: i for i, c in enumerate(codes)}
    for o in obs:
        if o.station_code is None or o.event_time is None:
            continue
        if o.station_code not in idx_of:
            if o.kind in AT_STATION_KINDS or o.kind == ObsKind.POSITION:
                warns.append(f"Observation at {o.station_code} is off the known route — ignored (possible unreported diversion).")
            continue
        i = idx_of[o.station_code]
        if o.kind in AT_STATION_KINDS:
            out.append(_Pos(float(i), o))
        elif o.kind == ObsKind.POSITION and o.km_after is not None:
            frac = 0.0
            a, b = route.stop(codes[i]), route.stop(codes[i + 1]) if i + 1 < len(codes) else None
            if a and b and b.km > a.km:
                frac = min(max(o.km_after / (b.km - a.km), 0.0), 0.999)
            out.append(_Pos(i + frac, o))
    return out


def _sched_pass(route: Route, stop: ScheduledStop | None, service_date) -> datetime | None:
    return stop.pass_dt(service_date) if stop else None


def _sched_at_index(route: Route, codes: list[str], fidx: float, service_date) -> datetime | None:
    """Scheduled time at a fractional route index (linear in km between two timed points)."""
    i = int(fidx)
    a = route.stop(codes[i])
    ta = _sched_pass(route, a, service_date)
    if fidx == i or i + 1 >= len(codes):
        return ta
    b = route.stop(codes[i + 1])
    tb = (b.arr_dt(service_date) or b.dep_dt(service_date)) if b else None
    if ta is None or tb is None:
        return None
    return ta + (tb - ta) * (fidx - i)


def _km_at(route: Route, codes: list[str], fidx: float) -> float | None:
    i = int(fidx)
    a = route.stop(codes[i])
    if a is None:
        return None
    if fidx == i or i + 1 >= len(codes):
        return a.km
    b = route.stop(codes[i + 1])
    return a.km + ((b.km - a.km) * (fidx - i) if b else 0.0)


def _avg_speed_kmpm(route: Route, service_date) -> float | None:
    first, last = route.stops[0], route.stops[-1]
    t0 = first.dep_dt(service_date)
    t1 = last.arr_dt(service_date)
    if not t0 or not t1 or t1 <= t0:
        return None
    return (last.km - first.km) / minutes(t1 - t0)


def evaluate_crossing(route: Route, observations: list[LiveObservation], station_code: str,
                      now: datetime, service_date, cfg: EngineConfig | None = None) -> CrossingResult:
    cfg = cfg or EngineConfig()
    obs = [o for o in clean_observations(route, observations) if o.service_date == service_date]
    is_demo = route.is_demo
    stop = route.stop(station_code)
    name = stop.name if stop else station_code
    sched = _sched_pass(route, stop, service_date)

    def result(ec, ver, crossed, headline, **kw) -> CrossingResult:
        return CrossingResult(train_number=route.train_number, service_date=service_date,
                              station_code=station_code, station_name=name, event_class=ec,
                              verification=ver,
                              crossed=crossed, headline=headline, scheduled_time=sched,
                              method_version=ENGINE_VERSION, is_demo=is_demo, **kw)

    # 1. Cancellation
    cancels = [o for o in obs if o.kind == ObsKind.CANCELLED]
    if cancels:
        c = max(cancels, key=lambda o: o.source_time)
        return result(EventClass.CANCELLED, Verification.VERIFIED_LIVE, False,
                      f"Train {route.train_number} is reported CANCELLED for {service_date:%d %b %Y}.",
                      confidence=0.95, method="source cancellation record", source_id=c.source_id,
                      source_time=c.source_time, evidence=[f"CANCELLED record from {c.source_id}"])

    codes, warns = effective_codes(route, obs)
    if station_code not in codes and not route.complete and route.index_of(station_code) is None:
        return result(EventClass.NOT_VERIFIED, Verification.ESTIMATED, None,
                      f"Crossing not verified — {station_code} is not listed on the source page (it lists only "
                      "halting stations). The train is confirmed past it only once it is reported at a later halt.",
                      confidence=0.0, method="station absent from partial route", warnings=warns)
    if station_code not in codes:
        why = "not on this train's route" if route.index_of(station_code) is None else \
              "dropped from the route (diversion / short termination)"
        return result(EventClass.NOT_ON_ROUTE, Verification.VERIFIED_LIVE if route.index_of(station_code) is not None else Verification.DERIVED,
                      False, f"{name} is {why}.", confidence=0.9, method="route membership", warnings=warns)

    r = codes.index(station_code)
    halt = bool(stop.halt) if stop else False
    positions = _positions(codes, route, obs, warns)

    # 2. Direct evidence at the reference station
    direct = [p.obs for p in positions if p.idx == r]
    if direct:
        by_kind: dict[ObsKind, LiveObservation] = {}
        for o in direct:  # latest source_time wins per kind (corrections)
            prev = by_kind.get(o.kind)
            if prev and prev.event_time != o.event_time:
                warns.append(f"Source corrected {o.kind.value} time at {station_code}: {fmt_hm(prev.event_time)} → {fmt_hm(o.event_time)}")
            if not prev or o.source_time >= prev.source_time:
                by_kind[o.kind] = o
        best = by_kind.get(ObsKind.DEPARTED) or by_kind.get(ObsKind.PASSED) or by_kind[ObsKind.ARRIVED]
        verb = {ObsKind.DEPARTED: "departed", ObsKind.PASSED: "passed", ObsKind.ARRIVED: "arrived at"}[best.kind]
        ec = EventClass.SCHEDULED_HALT if halt else EventClass.CONFIRMED_PASSAGE
        delay = f" ({best.delay_min:+d} min vs schedule)" if best.delay_min is not None else ""
        extra = " — departure not yet reported" if best.kind == ObsKind.ARRIVED else ""
        return result(ec, Verification.VERIFIED_LIVE, True,
                      f"Train {route.train_number} {verb} {name} at {fmt_hm(best.event_time)}{delay}{extra}.",
                      event_time=best.event_time, confidence=0.97, method=f"direct {best.kind.value} record at station",
                      source_id=best.source_id, source_time=best.source_time,
                      evidence=[f"{o.kind.value} {fmt_hm(o.event_time)} via {o.source_id}" for o in by_kind.values()],
                      warnings=warns)

    upstream = [p for p in positions if p.idx < r]
    downstream = [p for p in positions if p.idx > r]

    # 3. Downstream evidence proves passage; bracket the time
    if downstream:
        first_down = min(downstream, key=lambda p: (p.idx, p.obs.event_time))
        last_up = max(upstream, key=lambda p: (p.idx, p.obs.event_time)) if upstream else None
        conf = 0.9
        if last_up and first_down.obs.event_time < last_up.obs.event_time - timedelta(minutes=cfg.out_of_order_tolerance_min):
            warns.append("Out-of-order observations: a later station is timestamped before an earlier one. Confidence reduced.")
            conf = 0.6
        latest = first_down.obs.event_time
        sd_down = _sched_at_index(route, codes, first_down.idx, service_date)
        if last_up:
            earliest = last_up.obs.event_time
            km_u, km_d, km_r = (_km_at(route, codes, last_up.idx), _km_at(route, codes, first_down.idx),
                                _km_at(route, codes, float(r)))
            if None not in (km_u, km_d, km_r) and km_d > km_u:
                central = earliest + (latest - earliest) * ((km_r - km_u) / (km_d - km_u))
            else:
                central = earliest + (latest - earliest) / 2
            method = "bracketed between upstream and downstream source records; km-linear interpolation"
        else:
            if sched and sd_down:
                run = sd_down - sched
                central = latest - run
                earliest = latest - run * cfg.late_skew
            else:
                central, earliest = None, None
            method = "downstream source record minus timetable run-time"
        ec = EventClass.SCHEDULED_HALT if halt else EventClass.CONFIRMED_PASSAGE
        window = f"between {fmt_hm(earliest)} and {fmt_hm(latest)}" if earliest else f"before {fmt_hm(latest)}"
        if central:
            window += f" (best estimate ~{fmt_hm(central)})"
        return result(ec, Verification.DERIVED, True,
                      f"Crossing CONFIRMED: train {route.train_number} is already reported at "
                      f"{first_down.obs.station_code} ({fmt_hm(latest)}), so it has passed {name}. "
                      f"Passage time not reported directly; it was {window}.",
                      est_earliest=earliest, est_latest=latest, est_central=central, confidence=conf,
                      method=method, source_id=first_down.obs.source_id, source_time=first_down.obs.source_time,
                      evidence=[f"{first_down.obs.kind.value} at {first_down.obs.station_code} {fmt_hm(latest)}"] +
                               ([f"{last_up.obs.kind.value} at {last_up.obs.station_code} {fmt_hm(last_up.obs.event_time)}"] if last_up else []),
                      warnings=warns)

    # 4. Only upstream (or nothing): estimate if defensible, else NOT_VERIFIED
    ec_est = EventClass.SCHEDULED_HALT if halt else EventClass.ESTIMATED_CROSSING
    if not upstream:
        first = route.stops[0]
        origin_dep = first.dep_dt(service_date)
        resch = [o for o in obs if o.kind == ObsKind.RESCHEDULED and o.payload.get("new_departure")]
        if resch:
            from .timeutil import parse_iso
            origin_dep = parse_iso(max(resch, key=lambda o: o.source_time).payload["new_departure"])
            warns.append(f"Train RESCHEDULED; new origin departure {fmt_hm(origin_dep)}.")
        state = "has not started yet" if origin_dep and now < origin_dep else "has no position reports from the source yet"
        return result(EventClass.NOT_VERIFIED, Verification.STALE if obs else Verification.ESTIMATED, None,
                      f"Crossing not verified — train {route.train_number} {state}. Monitoring continues.",
                      confidence=0.0, method="insufficient evidence", warnings=warns)

    anchor = max(upstream, key=lambda p: (p.idx, p.obs.event_time))
    a_obs = anchor.obs
    age = minutes(now - a_obs.source_time)
    if age > cfg.unusable_after_min:
        return result(EventClass.NOT_VERIFIED, Verification.STALE, None,
                      f"Crossing not verified — latest source update is {age:.0f} min old "
                      f"({a_obs.station_code} at {fmt_hm(a_obs.event_time)}). Monitoring continues.",
                      confidence=0.0, method="stale data guard", source_id=a_obs.source_id,
                      source_time=a_obs.source_time, warnings=warns)

    s_anchor = _sched_at_index(route, codes, anchor.idx, service_date)
    km_a, km_r = _km_at(route, codes, anchor.idx), _km_at(route, codes, float(r))
    dist = (km_r - km_a) if km_a is not None and km_r is not None else None
    if s_anchor and sched:
        run = sched - s_anchor
        method = "latest source record + timetable run-time"
    elif dist is not None and (spd := _avg_speed_kmpm(route, service_date)):
        run = timedelta(minutes=dist / spd)
        method = "latest source record + average scheduled speed (diverted section has no timetable)"
    else:
        return result(EventClass.NOT_VERIFIED, Verification.ESTIMATED, None,
                      f"Crossing not verified — insufficient route timing data to estimate {name}.",
                      confidence=0.0, method="insufficient route data", warnings=warns)
    if dist is not None and dist > cfg.max_estimate_km:
        return result(EventClass.NOT_VERIFIED, Verification.ESTIMATED, None,
                      f"Crossing not verified — train is {dist:.0f} km away; too far to estimate reliably.",
                      confidence=0.0, method="horizon guard", warnings=warns)

    if a_obs.kind == ObsKind.ARRIVED:
        st = route.stop(a_obs.station_code)
        ts_arr = st.arr_dt(service_date) if st else None
        ts_dep = st.dep_dt(service_date) if st else None
        if ts_arr and ts_dep:
            run += ts_dep - ts_arr if s_anchor == ts_dep else timedelta(0)
    central = a_obs.event_time + run
    d = dist or 0.0
    spread = cfg.base_uncertainty_min + cfg.uncertainty_per_100km * d / 100 + \
        cfg.stale_uncertainty_per_min * max(age - 5, 0)
    earliest = central - timedelta(minutes=spread)
    latest = central + timedelta(minutes=spread * cfg.late_skew)
    conf = max(0.2, min(0.8, 0.8 - 0.0009 * d - 0.006 * age))
    if age > cfg.stale_after_min:
        warns.append(f"Source data is {age:.0f} min old — estimate widened.")
    if latest < now:
        when = (f"Estimated to have crossed {name} around {fmt_hm(central)} "
                f"(window {fmt_hm(earliest)}–{fmt_hm(latest)}) — NOT yet verified by the source")
    elif earliest <= now:
        when = f"Estimated crossing of {name} is happening now (window {fmt_hm(earliest)}–{fmt_hm(latest)}) — not verified"
    else:
        when = f"ESTIMATED crossing of {name} at about {fmt_hm(central)} (window {fmt_hm(earliest)}–{fmt_hm(latest)})"
    if halt:
        when = when.replace("crossing of", "arrival at").replace("crossed", "reached")
    return result(ec_est, Verification.ESTIMATED, None, when + ".",
                  est_earliest=earliest, est_latest=latest, est_central=central, confidence=round(conf, 2),
                  method=method, source_id=a_obs.source_id, source_time=a_obs.source_time,
                  evidence=[f"latest: {a_obs.kind.value} {a_obs.station_code} {fmt_hm(a_obs.event_time)}"
                            + (f", {d:.0f} km before {station_code}" if dist is not None else "")],
                  warnings=warns)
