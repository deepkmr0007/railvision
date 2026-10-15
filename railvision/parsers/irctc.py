"""Parse an IRCTC online-charts vacant-berth page and NTES-style train-name search results.

Vacant-berth pages list only VACANT entries (coach, berth, type, from→to). This parser never
invents occupied berths, and the result always carries the chart status and the page's own wording.
"""
from __future__ import annotations

import json
import re

from .ntes import _jlists, _norm_key

PARSER_VERSION = "irctc-vacancy-parser/1.0"
COACH = r"(?:[A-Z]{1,3}\d{1,2})"
BTYPES = r"(?:SIDE\s+LOWER|SIDE\s+UPPER|LOWER|MIDDLE|UPPER|LB|MB|UB|SL|SU|SM|WS|MS|AS|WINDOW|AISLE|CB|CP)"
BERTH_LINE = re.compile(r"\b(?P<coach>" + COACH + r")\b[\s,/|:-]{1,6}(?:berth\s*(?:no\.?)?\s*)?(?P<berth>\d{1,3})\b[\s,/|:-]{0,6}"
                        r"(?P<type>" + BTYPES + r")?\b[\s,/|:-]{0,8}(?:from\s*)?(?P<frm>[A-Z]{2,5})\s*(?:-|–|to|→|>)\s*(?P<to>[A-Z]{2,5})\b", re.I)
CLASS_LINE = re.compile(r"\b(1A|2A|3A|3E|SL|CC|EC|2S|FC|EA|EV|VS)\b\s*[:\-|]?\s*(?P<n>\d{1,4})(?![A-Za-z0-9])\s*(?:vacant|berths?|seats?)?", re.I)
NOT_PREPARED = re.compile(r"chart\s+(?:is\s+)?not\s+(?:yet\s+)?prepared|chart\s+not\s+available", re.I)
CHART_NO = re.compile(r"(first|1st|second|2nd)\s+chart", re.I)

BKEYS = {"coach": {"coachname", "coach", "coachno", "coachid"},
         "berth": {"berthnumber", "berthno", "berth", "seatno", "berthnum"},
         "type": {"berthcode", "berthtype", "type", "seattype"},
         "frm": {"from", "fromstn", "fromstation", "fromstncode", "src"},
         "to": {"to", "tostn", "tostation", "tostncode", "dest"}}


def _from_tables(tables) -> list[dict]:
    out = []
    for tb in tables or []:
        for hi, row in enumerate(tb[:4]):
            h = [_norm_key(c) for c in row]
            ci = lambda keys: next((i for i, x in enumerate(h) if any(x.startswith(k) for k in keys)), None)  # noqa: E731
            c, b = ci(["coach"]), ci(["berth", "seat"])
            f, t = ci(["from", "src", "boarding"]), ci(["to", "dest", "upto"])
            ty = next((i for i, x in enumerate(h) if "type" in x or x in ("code", "berthcode")), None)
            if c is None or b is None:
                continue
            for r in tb[hi + 1:]:
                if max(c, b) >= len(r) or not re.fullmatch(COACH, r[c].strip().upper()) or not re.search(r"\d", r[b]):
                    continue
                out.append({"coach": r[c].strip().upper(), "berth": re.search(r"\d+", r[b]).group(),
                            "type": (r[ty].strip().upper() if ty is not None and ty < len(r) else None) or None,
                            "from": r[f].strip().upper()[:5] if f is not None and f < len(r) else None,
                            "to": r[t].strip().upper()[:5] if t is not None and t < len(r) else None})
            break
    return out


def _from_json(responses) -> list[dict]:
    best = []
    for r in responses or []:
        try:
            data = json.loads(r.get("body") or "")
        except ValueError:
            continue
        for lst in _jlists(data):
            km = {_norm_key(k): k for k in lst[0].keys()}
            pick = {c: next((km[k] for k in keys if k in km), None) for c, keys in BKEYS.items()}
            if not pick["coach"] or not pick["berth"]:
                continue
            rows = [{"coach": str(it.get(pick["coach"])).upper(), "berth": str(it.get(pick["berth"])),
                     "type": str(it.get(pick["type"])).upper() if pick["type"] and it.get(pick["type"]) else None,
                     "from": str(it.get(pick["frm"])).upper() if pick["frm"] and it.get(pick["frm"]) else None,
                     "to": str(it.get(pick["to"])).upper() if pick["to"] and it.get(pick["to"]) else None} for it in lst]
            if len(rows) > len(best):
                best = rows
    return best


def parse_vacancy(capture: dict, boarding: str | None = None, travel_class: str | None = None) -> dict:
    text = capture.get("text") or ""
    issues = []
    rows, method = _from_json(capture.get("json")), "json"
    if not rows:
        rows, method = _from_tables(capture.get("tables")), "table"
    if not rows:
        rows = [{"coach": m.group("coach").upper(), "berth": m.group("berth"), "type": (m.group("type") or "").upper() or None,
                 "from": m.group("frm").upper(), "to": m.group("to").upper()} for m in BERTH_LINE.finditer(text)]
        method = "text"
        if rows:
            issues.append("Parsed from free text; check against the screenshot.")
    seen, uniq = set(), []
    for r in rows:
        k = (r["coach"], r["berth"], r.get("from"), r.get("to"))
        if k not in seen:
            seen.add(k)
            uniq.append(r)
    classes = {}
    for m in CLASS_LINE.finditer(text):
        classes.setdefault(m.group(1).upper(), int(m.group("n")))
    prepared = None if not text else (False if NOT_PREPARED.search(text) else (True if uniq or classes else None))
    cm = CHART_NO.search(text)
    chart_no = (1 if cm.group(1).lower() in ("first", "1st") else 2) if cm else None
    by_coach: dict[str, int] = {}
    for r in uniq:
        by_coach[r["coach"]] = by_coach.get(r["coach"], 0) + 1
    from_board = sum(1 for r in uniq if boarding and r.get("from") == boarding.upper())
    conf = 0.85 if method in ("json", "table") and uniq else 0.6 if uniq else (0.5 if prepared is False or classes else 0.2)
    if prepared is None and not uniq:
        issues.append("No vacant-berth list or chart message found on the page.")
    return {"vacant": uniq, "classes": classes, "chart_prepared": prepared, "chart_no": chart_no,
            "by_coach": by_coach, "from_boarding": from_board, "total_vacant": len(uniq),
            "boarding": boarding, "travel_class": travel_class, "method": method, "confidence": conf,
            "issues": issues, "parser": PARSER_VERSION}


SEARCH_LINE = re.compile(r"\b(\d{5})\b\s*[-–:|]?\s*([A-Z][A-Za-z0-9 .'/()&-]{3,60}?)\s*(?=$|\n|\||\s{2,}|\d{5})", re.M)


def parse_search(capture: dict, query: str) -> dict:
    found: dict[str, str] = {}
    for tb in capture.get("tables") or []:
        for r in tb:
            line = " ".join(r)
            for m in SEARCH_LINE.finditer(line):
                found.setdefault(m.group(1), m.group(2).strip())
    for m in SEARCH_LINE.finditer(capture.get("text") or ""):
        found.setdefault(m.group(1), m.group(2).strip())
    q = (query or "").lower().split()
    rows = [{"number": n, "name": nm} for n, nm in found.items()]
    rows.sort(key=lambda r: -sum(w in r["name"].lower() for w in q))
    if q:
        rows = [r for r in rows if any(w in r["name"].lower() or w == r["number"] for w in q)] or rows
    return {"results": rows[:40], "query": query, "confidence": 0.8 if rows else 0.2}
