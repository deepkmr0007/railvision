"""Parse an NTES-style running-status page (visible text + tables + the page's own JSON).

Safety rules (the important part):
  * A station counts as reached ONLY if the page says so (Departed / Arrived / Crossed …) or it lies
    before the station the page names as the current position. "Expected" times are never turned
    into actual events.
  * Menu text like "Cancelled Trains" never marks a train cancelled; only explicit sentences do.
  * Every parse returns a confidence and a list of issues; below 0.6 no live observations are
    emitted — the dashboard shows the official page text/screenshot instead.

The page layout of NTES is NOT verified from this build environment (it was unreachable), so the
parser is deliberately layout-tolerant and must be calibrated on your first real captures.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from ..core.models import LiveObservation, ObsKind, Route, ScheduledStop
from ..core.timeutil import IST, parse_hhmm

PARSER_VERSION = "ntes-parser/1.0"
TIME = r"(?:[01]?\d|2[0-3]):[0-5]\d"
NOT_CODES = {"PF", "AM", "PM", "IST", "HRS", "HR", "KM", "KMS", "DAY", "SCH", "ACT", "ETA", "ETD", "STA", "STD", "ATA",
             "ATD", "RT", "NA", "SRC", "DSTN", "EXP", "ARR", "DEP", "MIN", "MINS", "NO", "PNR", "NTES", "IRCTC", "CRIS", "OK"}
STN_PAREN = re.compile(r"([A-Z][A-Z0-9 .,'/&-]{1,45}?)\s*[\(\[]\s*([A-Z]{1,5})\s*[\)\]]")
STN_DASH = re.compile(r"^\s*([A-Z]{2,5})\s*[-–]\s*([A-Z][A-Z .'/-]{2,45})\s*$")
REACHED = re.compile(r"\b(departed|crossed|arrived|reached|passed|left|dep\.?\s*done)\b", re.I)
NOT_REACHED = re.compile(r"\b(not\s+(?:yet\s+)?(?:arrived|reached|departed|crossed)|yet\s+to\s+(?:arrive|reach|depart|start)|expected|upcoming)\b", re.I)
POS_RE = re.compile(r"(?P<verb>departed\s+from|departed|left|crossed|passed|arrived\s+at|reached|is\s+at|at)\s+"
                    r"(?P<name>[A-Z][A-Za-z .'/-]{1,45}?)\s*[\(\[]\s*(?P<code>[A-Z]{1,5})\s*[\)\]]\s*"
                    r"(?:at|on|@)?\s*(?P<time>" + TIME + r")?", re.I)
KM_RE = re.compile(r"(?P<km>\d+(?:\.\d+)?)\s*kms?\.?\s*(?:after|from|ahead\s+of|beyond|past)\s+"
                   r"(?P<name>[A-Z][A-Za-z .'/-]{1,45}?)\s*[\(\[]\s*(?P<code>[A-Z]{1,5})\s*[\)\]]", re.I)
UPDATED_RE = re.compile(r"last\s*updated\s*(?:on|at)?\s*[:\-]?\s*(?P<d>\d{1,2}[-/ ][A-Za-z]{3}[-/ ]\d{2,4}|\d{1,2}[-/]\d{1,2}[-/]\d{2,4})?"
                        r"\s*,?\s*(?:at\s*)?(?P<t>" + TIME + r")", re.I)
CANCEL_RE = re.compile(r"(train\s+(?:is|has\s+been|stands)\s+(?:fully\s+)?cancel+ed|fully\s+cancel+ed|"
                       r"cancel+ed\s+for\s+(?:the\s+)?(?:journey|date|day)|this\s+train\s+is\s+cancel+ed)", re.I)
DIVERT_RE = re.compile(r"(train\s+(?:is|has\s+been)\s+diverted|diverted\s+via|running\s+on\s+diverted\s+route)", re.I)
NOT_STARTED_RE = re.compile(r"(yet\s+to\s+start|not\s+(?:yet\s+)?started|will\s+start\s+from|has\s+not\s+started)", re.I)
TRAIN_HDR = re.compile(r"\b(\d{5})\b\s*[-–:|]?\s*([A-Z][A-Z0-9 .'/()-]{3,60}?)(?:\s{2,}|\n|$| EXP| SF| \|)", re.M)
DELAY_RE = re.compile(r"(?:delay|late)\s*(?:by)?\s*[:\-]?\s*(?:(?P<h>\d{1,2}):(?P<m>\d{2})\s*(?:hrs?)?|(?:(?P<h2>\d+)\s*h(?:ou)?rs?\.?\s*)?(?P<m2>\d+)\s*m(?:in(?:ute)?s?)?)", re.I)
ONTIME_RE = re.compile(r"\b(right\s*time|on\s*time|no\s*delay)\b", re.I)
EARLY_RE = re.compile(r"(?:before|early)\s*(?:by)?\s*[:\-]?\s*(?:(?P<h>\d{1,2}):(?P<m>\d{2})|(?P<m2>\d+)\s*m)", re.I)
PF_RE = re.compile(r"\b(?:pf|platform)\s*(?:no\.?)?\s*[:\-]?\s*(\d{1,2}[A-Z]?)\b", re.I)
KM_COL = re.compile(r"(\d{1,4}(?:\.\d+)?)\s*km", re.I)


@dataclass
class ParsedStation:
    code: str
    name: str
    sch_arr: str | None = None
    sch_dep: str | None = None
    act_arr: str | None = None
    act_dep: str | None = None
    expected_only: bool = False      # times were labelled "expected/ETA", never actuals
    day: int | None = None
    delay_min: int | None = None
    platform: str | None = None
    km: float | None = None
    reached: bool | None = None
    departed: bool | None = None
    halt: bool = True


@dataclass
class ParsedStatus:
    train_number: str | None = None
    train_name: str = ""
    stations: list[ParsedStation] = field(default_factory=list)
    position: dict | None = None
    last_updated: datetime | None = None
    cancelled: bool = False
    diverted: bool = False
    not_started: bool = False
    headline: str = ""
    confidence: float = 0.0
    issues: list[str] = field(default_factory=list)
    method: str = ""


# ------------------------------------------------------------------ small helpers
def delay_minutes(s: str | None) -> int | None:
    if not s:
        return None
    if ONTIME_RE.search(s):
        return 0
    m = re.search(r"(\d+)\s*m(?:in(?:ute)?s?)?\.?\s*(?:early|before\s+time|ahead)", s, re.I)
    if m:
        return -int(m.group(1))
    m = EARLY_RE.search(s)
    if m:
        return -((int(m.group("h") or 0) * 60 + int(m.group("m") or 0)) if m.group("h") else int(m.group("m2")))
    m = DELAY_RE.search(s)
    if m:
        if m.group("h") is not None:
            return int(m.group("h")) * 60 + int(m.group("m"))
        return int(m.group("h2") or 0) * 60 + int(m.group("m2"))
    return None


def delay_cell(s: str) -> int | None:
    """A value from a 'Delay' column: '6', '6 min', '00:06', 'RT', 'Right Time', '+6', '-2'."""
    s = (s or "").strip()
    m = re.fullmatch(r"([+-]?\d{1,4})\s*(?:m|min|mins|minutes)?\.?", s, re.I)
    if m:
        return int(m.group(1))
    m = re.fullmatch(r"(\d{1,2}):(\d{2})\s*(?:hrs?)?", s, re.I)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    if re.fullmatch(r"(rt|right\s*time|on\s*time|0)", s, re.I):
        return 0
    return delay_minutes(s)


def split_station(cell: str) -> tuple[str, str] | None:
    s = " ".join(cell.split())
    m = STN_PAREN.search(s.upper())
    if m and m.group(2) not in NOT_CODES:
        return m.group(2), m.group(1).strip(" -,")
    m = STN_DASH.match(s.upper())
    if m and m.group(1) not in NOT_CODES:
        return m.group(1), m.group(2).strip()
    return None


def _times(cell: str) -> list[str]:
    return re.findall(TIME, cell or "")


def _norm_key(k: str) -> str:
    return re.sub(r"[^a-z0-9]", "", k.lower())


# ------------------------------------------------------------------ tables
COLS = {
    "station": ["station", "stn", "stationname"],
    "sch_arr": ["scharr", "schedarr", "scheduledarrival", "sta", "scharrival"],
    "act_arr": ["actarr", "actualarrival", "ata", "exparr", "eta", "actarrival"],
    "sch_dep": ["schdep", "scheddep", "scheduleddeparture", "std", "schdeparture"],
    "act_dep": ["actdep", "actualdeparture", "atd", "expdep", "etd", "actdeparture"],
    "arr": ["arr", "arrival", "arrschact", "arrivalschact"],
    "dep": ["dep", "departure", "depschact", "departureschact"],
    "delay": ["delay", "late", "delayarr", "delaydep"],
    "pf": ["pf", "platform", "pfno", "platformno"],
    "day": ["day"],
    "km": ["km", "dist", "distance", "kms"],
    "status": ["status", "remarks", "remark"],
}


def map_header(row: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for i, h in enumerate(row):
        k = _norm_key(h)
        for col, keys in COLS.items():
            if col in out:
                continue
            if k in keys or (col in ("sch_arr", "act_arr", "sch_dep", "act_dep") and any(k.startswith(x) for x in keys if len(x) >= 5)):
                out[col] = i
                break
        else:
            if "station" not in out and "station" in k:
                out["station"] = i
    return out


def parse_tables(tables: list[list[list[str]]], issues: list[str]) -> list[ParsedStation]:
    best: list[ParsedStation] = []
    for tb in tables or []:
        for hi, row in enumerate(tb[:5]):
            hm = map_header(row)
            if "station" not in hm or not ({"sch_arr", "sch_dep", "arr", "dep"} & set(hm)):
                continue
            rows: list[ParsedStation] = []
            for r in tb[hi + 1:]:
                if len(r) <= hm["station"]:
                    continue
                st = split_station(r[hm["station"]])
                if not st:
                    continue
                g = lambda c: r[hm[c]] if c in hm and hm[c] < len(r) else ""  # noqa: E731
                ps = ParsedStation(code=st[0], name=st[1])
                ps.sch_arr = (_times(g("sch_arr")) or [None])[0]
                ps.sch_dep = (_times(g("sch_dep")) or [None])[0]
                ps.act_arr = (_times(g("act_arr")) or [None])[0]
                ps.act_dep = (_times(g("act_dep")) or [None])[0]
                for col, a, b in (("arr", "sch_arr", "act_arr"), ("dep", "sch_dep", "act_dep")):
                    ts = _times(g(col))
                    if ts and getattr(ps, a) is None:
                        setattr(ps, a, ts[0])
                        if len(ts) > 1:
                            setattr(ps, b, ts[1])
                if re.search(r"\b(exp|eta|etd|expected)\b", " ".join(row).lower()) and not re.search(r"\b(act|actual|ata|atd)\b", " ".join(row).lower()):
                    ps.expected_only = True
                row_text = " ".join(r)
                ps.delay_min = delay_cell(g("delay")) if g("delay") else delay_minutes(row_text)
                pf = g("pf").strip()
                ps.platform = pf if re.fullmatch(r"\d{1,2}[A-Z]?", pf or "") else None
                d = re.search(r"\d+", g("day") or "")
                ps.day = int(d.group()) if d else None
                k = KM_COL.search(g("km") + " km") if g("km") else None
                ps.km = float(k.group(1)) if k else None
                status = g("status") or row_text
                if NOT_REACHED.search(status):
                    ps.reached = False
                elif REACHED.search(status):
                    ps.reached = True
                    ps.departed = bool(re.search(r"\b(departed|crossed|passed|left)\b", status, re.I))
                if re.search(r"\b(no\s*halt|non[-\s]?stop|pass(?:ing)?\s*only)\b", row_text, re.I):
                    ps.halt = False
                rows.append(ps)
            if len(rows) > len(best):
                best = rows
    return best


# ------------------------------------------------------------------ free text
def _classify(ctx: str) -> tuple[str | None, str | None, bool]:
    c = ctx.lower()
    kind = "act" if re.search(r"\b(act|actual|ata|atd)\b", c) else "exp" if re.search(r"\b(exp|expected|eta|etd)\b", c) \
        else "sch" if re.search(r"\b(sch|sched|scheduled|sta|std)\b", c) else None
    ev = "arr" if re.search(r"\b(arr|arrival|sta|ata|eta)\b", c) else "dep" if re.search(r"\b(dep|departure|std|atd|etd)\b", c) else None
    return kind, ev, kind == "exp"


def parse_text_blocks(text: str, issues: list[str]) -> list[ParsedStation]:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    blocks: list[tuple[tuple[str, str], list[str]]] = []
    for ln in lines:
        st = split_station(ln) if len(ln) < 70 else None
        if st and not POS_RE.search(ln) and not KM_RE.search(ln):
            blocks.append((st, []))
        elif blocks:
            blocks[-1][1].append(ln)
    out: list[ParsedStation] = []
    assumed = False
    for (code, name), body in blocks:
        txt = "\n".join(body[:8])
        ps = ParsedStation(code=code, name=name)
        labelled = []
        for m in re.finditer(r"([A-Za-z. ]{0,22}?)[:\-]?\s*(" + TIME + r"|SRC|DSTN|SOURCE|DESTINATION)\b", txt):
            kind, ev, exp = _classify(m.group(1))
            labelled.append((kind, ev, m.group(2), exp))
        if labelled and all(k is None and e is None for k, e, _, _ in labelled):
            ts = [t for _, _, t, _ in labelled if re.fullmatch(TIME, t)]
            if len(ts) == 4:
                ps.sch_arr, ps.act_arr, ps.sch_dep, ps.act_dep = ts
                assumed = True
            elif len(ts) == 2:
                ps.sch_dep, ps.act_dep = ts
                assumed = True
            elif len(ts) == 1:
                ps.sch_dep = ts[0]
        else:
            last_ev = None
            for kind, ev, t, exp in labelled:
                ev = ev or last_ev
                last_ev = ev
                if not re.fullmatch(TIME, t) or ev is None:
                    continue
                if kind in (None, "sch"):
                    if getattr(ps, f"sch_{ev}") is None:
                        setattr(ps, f"sch_{ev}", t)
                else:
                    setattr(ps, f"act_{ev}", t)
                    if exp:
                        ps.expected_only = True
        ps.delay_min = delay_minutes(txt)
        m = PF_RE.search(txt)
        ps.platform = m.group(1) if m else None
        m = KM_COL.search(txt)
        ps.km = float(m.group(1)) if m else None
        m = re.search(r"\bday\s*[:\-]?\s*(\d)\b", txt, re.I)
        ps.day = int(m.group(1)) if m else None
        if NOT_REACHED.search(txt):
            ps.reached = False
        elif REACHED.search(txt):
            ps.reached = True
            ps.departed = bool(re.search(r"\b(departed|crossed|passed|left)\b", txt, re.I))
        if re.search(r"\b(no\s*halt|non[-\s]?stop)\b", txt, re.I):
            ps.halt = False
        out.append(ps)
    if assumed:
        issues.append("Time columns had no labels; assumed order Sch-Arr, Act-Arr, Sch-Dep, Act-Dep.")
    return out


# ------------------------------------------------------------------ the page's own JSON
JKEYS = {
    "code": {"stncode", "stationcode", "stncd", "stcode", "code", "stationcd"},
    "name": {"stnname", "stationname", "name", "stnnm"},
    "sch_arr": {"scharr", "scharrtime", "scheduledarrival", "sta", "scharrivaltime"},
    "act_arr": {"actarr", "actarrtime", "actualarrival", "ata", "actarrivaltime"},
    "sch_dep": {"schdep", "schdeptime", "scheduleddeparture", "std", "schdeparturetime"},
    "act_dep": {"actdep", "actdeptime", "actualdeparture", "atd", "actdeparturetime"},
    "delay": {"delay", "delayarr", "delaydep", "latemin", "delayinmin", "delaymin"},
    "day": {"day", "daycnt", "daycount", "dayno"},
    "km": {"distance", "km", "dist", "kms"},
    "pf": {"pf", "platform", "platformno", "pfno"},
    "arrived": {"hasarrived", "arrived", "isarrived"},
    "departed": {"hasdeparted", "departed", "isdeparted", "crossed", "iscrossed"},
    "halt": {"ishalt", "halt", "stoppage", "isstoppage"},
}


def _jlists(obj, depth=0):
    if depth > 6:
        return
    if isinstance(obj, list) and len(obj) >= 3 and all(isinstance(x, dict) for x in obj[:3]):
        yield obj
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _jlists(v, depth + 1)
    elif isinstance(obj, list):
        for v in obj[:50]:
            yield from _jlists(v, depth + 1)


def parse_json(responses: list[dict]) -> list[ParsedStation]:
    best: list[ParsedStation] = []
    for r in responses or []:
        try:
            data = json.loads(r.get("body") or "")
        except ValueError:
            continue
        for lst in _jlists(data):
            km = {_norm_key(k): k for k in lst[0].keys()}
            pick = {c: next((km[k] for k in keys if k in km), None) for c, keys in JKEYS.items()}
            if not pick["code"] or not (pick["sch_arr"] or pick["sch_dep"]):
                continue
            rows = []
            for it in lst:
                code = str(it.get(pick["code"]) or "").upper().strip()
                if not re.fullmatch(r"[A-Z]{1,5}", code):
                    continue
                ps = ParsedStation(code=code, name=str(it.get(pick["name"]) or code) if pick["name"] else code)
                for c in ("sch_arr", "act_arr", "sch_dep", "act_dep"):
                    v = it.get(pick[c]) if pick[c] else None
                    ts = _times(str(v)) if v else []
                    setattr(ps, c, ts[0] if ts else None)
                dv = it.get(pick["delay"]) if pick["delay"] else None
                ps.delay_min = int(dv) if isinstance(dv, (int, float)) or (isinstance(dv, str) and dv.lstrip("-").isdigit()) else delay_minutes(str(dv or ""))
                ps.day = int(it.get(pick["day"])) if pick["day"] and str(it.get(pick["day"]) or "").isdigit() else None
                kv = it.get(pick["km"]) if pick["km"] else None
                ps.km = float(kv) if isinstance(kv, (int, float)) or (isinstance(kv, str) and re.fullmatch(r"\d+(\.\d+)?", kv)) else None
                pf = it.get(pick["pf"]) if pick["pf"] else None
                ps.platform = str(pf) if pf not in (None, "", "-", 0, "0") else None
                tv = lambda c: it.get(pick[c]) in (True, 1, "1", "true", "Y", "y", "YES", "yes") if pick[c] else None  # noqa: E731
                if pick["arrived"] or pick["departed"]:
                    ps.departed = tv("departed")
                    ps.reached = bool(tv("arrived") or ps.departed)
                if pick["halt"]:
                    ps.halt = bool(tv("halt"))
                rows.append(ps)
            if len(rows) > len(best):
                best = rows
    return best


# ------------------------------------------------------------------ main entry
def parse_status(capture: dict, expected_train: str | None = None) -> ParsedStatus:
    text = capture.get("text") or ""
    issues: list[str] = []
    p = ParsedStatus(method=PARSER_VERSION)
    st_json = parse_json(capture.get("json") or [])
    st_tab = parse_tables(capture.get("tables") or [], issues)
    st_txt = parse_text_blocks(text, issues) if not st_tab and not st_json else []
    sources = [("json", st_json), ("table", st_tab), ("text", st_txt)]
    used, stations = max(sources, key=lambda s: (len(s[1]), s[0] != "text"))
    p.stations = stations
    if used == "text":
        issues.append("Parsed from free text (no table found) — check the screenshot.")

    for m in TRAIN_HDR.finditer(text[:3000]):
        if not expected_train or m.group(1) == expected_train:
            p.train_number, p.train_name = m.group(1), m.group(2).strip(" -|")
            break
    if expected_train and p.train_number is None and expected_train in text:
        p.train_number = expected_train
    if expected_train and p.train_number not in (None, expected_train):
        issues.append(f"Page shows train {p.train_number}, expected {expected_train}.")

    u = UPDATED_RE.search(text)
    if u:
        p.last_updated = _resolve_dt(u.group("d"), u.group("t"), capture)
    p.cancelled = bool(CANCEL_RE.search(text))
    p.diverted = bool(DIVERT_RE.search(text))
    p.not_started = bool(NOT_STARTED_RE.search(text))
    m = KM_RE.search(text)
    if m:
        p.position = {"kind": "POSITION", "code": m.group("code").upper(), "km_after": float(m.group("km"))}
    else:
        for m in POS_RE.finditer(text):
            code = m.group("code").upper()
            if code in NOT_CODES or (p.stations and code not in {s.code for s in p.stations}):
                continue
            v = m.group("verb").lower()
            kind = "ARRIVED" if v.startswith(("arrived", "reached", "is at")) or v == "at" else "DEPARTED"
            p.position = {"kind": kind, "code": code, "time": m.group("time")}
            p.headline = m.group(0).strip()
            break

    conf = 0.3
    if used in ("table", "json"):
        conf += 0.3
    elif stations:
        conf += 0.15
    if len(stations) >= 3:
        conf += 0.1
    if p.last_updated:
        conf += 0.1
    else:
        issues.append("No 'last updated' time on the page; using the capture time as the source time.")
    if p.position or p.not_started or p.cancelled:
        conf += 0.1
    if any(s.reached is not None for s in stations):
        conf += 0.1
    if any("assumed" in i for i in issues):
        conf -= 0.2
    if expected_train and p.train_number != expected_train:
        conf = min(conf, 0.3)
    p.confidence = round(max(0.0, min(conf, 0.95)), 2)
    p.issues = issues
    if not p.headline:
        if p.cancelled:
            p.headline = "Train is cancelled (per page)."
        elif p.not_started:
            p.headline = "Train has not started yet (per page)."
        elif p.position and p.position["kind"] == "POSITION":
            p.headline = f"{p.position['km_after']} km after {p.position['code']}"
    return p


def _resolve_dt(dstr: str | None, tstr: str, capture: dict) -> datetime | None:
    try:
        cap = datetime.fromisoformat(capture.get("finished_at")) if capture.get("finished_at") else None
    except ValueError:
        cap = None
    base = cap.astimezone(IST).date() if cap else None
    d = None
    if dstr:
        for f in ("%d-%b-%Y", "%d %b %Y", "%d/%b/%Y", "%d-%m-%Y", "%d/%m/%Y", "%d-%b-%y", "%d-%m-%y", "%d/%m/%y"):
            try:
                d = datetime.strptime(dstr.strip(), f).date()
                break
            except ValueError:
                continue
    d = d or base
    if not d:
        return None
    t = parse_hhmm(tstr)
    dt = datetime.combine(d, t, tzinfo=IST)
    if cap and not dstr and dt > cap + timedelta(minutes=5):
        dt -= timedelta(days=1)          # "updated 23:58" seen just after midnight
    return dt


# ------------------------------------------------------------------ to RAILVISION entities
def to_route_and_obs(p: ParsedStatus, train: str, start_date: date, capture: dict, source_id: str,
                     min_confidence: float = 0.6) -> tuple[Route | None, list[LiveObservation], list[str]]:
    issues = list(p.issues)
    if not p.stations:
        return None, [], issues + ["No station list found on the page."]
    cap_t = datetime.fromisoformat(capture["finished_at"]).astimezone(IST) if capture.get("finished_at") else datetime.now(IST)
    src_t = p.last_updated or cap_t
    # --- schedule & day-of-run
    stops: list[ScheduledStop] = []
    day, prev = 1, None
    for i, s in enumerate(p.stations):
        first = s.sch_arr or s.sch_dep
        if s.day:
            day = s.day
        elif first and prev and parse_hhmm(first) < parse_hhmm(prev) and \
                (datetime.combine(date.min, parse_hhmm(prev)) - datetime.combine(date.min, parse_hhmm(first))).seconds > 30 * 60:
            day += 1
        halt = s.halt and not (s.sch_arr and s.sch_dep and s.sch_arr == s.sch_dep and i not in (0, len(p.stations) - 1))
        stops.append(ScheduledStop(seq=i, code=s.code, name=s.name.title(), km=0.0, day=day,
                                   arr=None if i == 0 else s.sch_arr, dep=None if i == len(p.stations) - 1 else s.sch_dep,
                                   halt=halt if 0 < i < len(p.stations) - 1 else True, platform=s.platform))
        prev = s.sch_dep or s.sch_arr or prev
    have_km = sum(1 for s in p.stations if s.km is not None) >= len(p.stations) - 1
    t0 = stops[0].dep_dt(start_date)
    for st, s in zip(stops, p.stations):
        if have_km:
            st.km = float(s.km or 0.0)
        else:      # proportional proxy from timetable minutes (keeps interpolation monotonic)
            t = st.arr_dt(start_date) or st.dep_dt(start_date)
            st.km = round((t - t0).total_seconds() / 60.0, 1) if (t and t0) else float(st.seq)
    if not have_km:
        issues.append("Distances not on the page; using timetable minutes as distance proxy.")
    route = Route(train_number=train, train_name=p.train_name or train, stops=stops, source_id=source_id)
    route.complete = any(not s.halt for s in stops)      # only halts listed => intermediate stations unknown

    obs: list[LiveObservation] = []
    common = dict(train_number=train, service_date=start_date, source_id=source_id, source_time=src_t, retrieved_at=cap_t)
    if p.cancelled:
        return route, [LiveObservation(kind=ObsKind.CANCELLED, station_code=None, event_time=None, **common)], issues
    if p.confidence < min_confidence:
        return route, [], issues + [f"Parse confidence {p.confidence:.0%} is below {min_confidence:.0%}; "
                                    "no live events created — see the official page capture."]
    codes = [s.code for s in p.stations]
    pos_i = codes.index(p.position["code"]) if p.position and p.position.get("code") in codes else None

    def actual_dt(hhmm: str | None, sched: datetime | None) -> datetime | None:
        if not hhmm:
            return None
        t = parse_hhmm(hhmm)
        base = (sched or src_t).date()
        cands = [datetime.combine(base + timedelta(days=k), t, tzinfo=IST) for k in (-1, 0, 1)]
        ref = sched or src_t
        best = min(cands, key=lambda c: abs((c - ref).total_seconds()))
        return best if best <= cap_t + timedelta(minutes=10) else None    # an "actual" in the future is not actual

    for i, (s, st) in enumerate(zip(p.stations, stops)):
        reached, departed = s.reached, s.departed
        if reached is None and pos_i is not None:
            reached = i <= pos_i
            departed = i < pos_i or (i == pos_i and p.position["kind"] == "DEPARTED")
        if not reached or s.expected_only:
            continue
        a = actual_dt(s.act_arr, st.arr_dt(start_date))
        d = actual_dt(s.act_dep, st.dep_dt(start_date))
        pl = {"platform": s.platform} if s.platform else {}
        if not st.halt and (d or a):
            obs.append(LiveObservation(kind=ObsKind.PASSED, station_code=s.code, event_time=d or a, delay_min=s.delay_min, payload=pl, **common))
            continue
        if a and i > 0:
            obs.append(LiveObservation(kind=ObsKind.ARRIVED, station_code=s.code, event_time=a, delay_min=s.delay_min, payload=pl, **common))
        if d and (departed or departed is None and i < (pos_i if pos_i is not None else -1)) and i < len(stops) - 1:
            obs.append(LiveObservation(kind=ObsKind.DEPARTED, station_code=s.code, event_time=d, delay_min=s.delay_min, payload=pl, **common))
    if p.position and p.position["kind"] == "POSITION" and p.position["code"] in codes:
        obs.append(LiveObservation(kind=ObsKind.POSITION, station_code=p.position["code"], event_time=src_t,
                                   km_after=p.position["km_after"] if have_km else None, **common))
    if p.diverted:
        issues.append("Page says the train is diverted; the new route is not parsed — treat estimates with care.")
    return route, obs, issues
