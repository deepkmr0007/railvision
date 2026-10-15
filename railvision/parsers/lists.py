"""Parsers for NTES exceptional-train lists (cancelled / rescheduled / diverted) and PNR status pages."""
from __future__ import annotations

import re

TRAIN = re.compile(r"(?<![\d/.:-])(\d{5})(?![\d/.:-])")
DATE = re.compile(r"\b(\d{1,2}[-/ ](?:[A-Za-z]{3}|\d{1,2})[-/ ]\d{2,4}|\d{1,2}[-/ ][A-Za-z]{3})\b")
LIST_LABEL = {"cancelled": "Cancelled", "rescheduled": "Rescheduled", "diverted": "Diverted", "short_terminated": "Short-terminated"}


def parse_exceptions(capture: dict, list_kind: str) -> dict:
    rows: dict[tuple, dict] = {}

    def add(train: str, cells: list[str]):
        rest = [c for c in cells if c and train not in c][:6]
        name = next((c for c in rest if re.search(r"[A-Za-z]{3}", c) and not DATE.fullmatch(c.strip())), "")
        dates = DATE.findall(" ".join(cells))
        detail = " · ".join(c for c in rest if c != name)[:240]
        key = (train, dates[0] if dates else "")
        rows.setdefault(key, {"train": train, "name": name[:80], "date": dates[0] if dates else None, "detail": detail})

    for tb in capture.get("tables") or []:
        for r in tb:
            for cell in r:
                m = TRAIN.search(cell)
                if m:
                    add(m.group(1), [c.strip() for c in r])
                    break
    if not rows:
        for line in (capture.get("text") or "").splitlines():
            m = TRAIN.search(line)
            if m and len(line) < 300:
                parts = [p.strip() for p in re.split(r"\s{2,}|\t|\||,(?=\s)| - ", line) if p.strip()]
                add(m.group(1), parts if len(parts) > 1 else [line.replace(m.group(1), "").strip(" -:|"), line])
    items = list(rows.values())
    return {"list": list_kind, "items": items, "count": len(items), "confidence": 0.8 if items else 0.3}


STATUS = r"(CNF|CONFIRMED|RAC|GNWL|PQWL|RLWL|TQWL|RSWL|NPWL|WL|CAN|CANCELLED|NOSB|NO\s?SEAT)"
PAX = re.compile(STATUS + r"\s*[/,\-\s]?\s*(?:(?P<coach>[A-Z]{1,3}\d{1,2})\s*[/,\-\s]\s*)?(?P<num>\d{1,3})?(?:\s*[/,\-\s]\s*(?P<bt>LB|MB|UB|SL|SU|SM|WS|AS|MS|CB|CP|NC|GN))?", re.I)


def _pax(cell: str) -> dict | None:
    m = PAX.search(cell or "")
    if not m:
        return None
    st = m.group(1).upper().replace("CONFIRMED", "CNF").replace("CANCELLED", "CAN")
    return {"status": st, "coach": m.group("coach"), "number": m.group("num"), "berth_type": m.group("bt"), "raw": cell.strip()[:60]}


def parse_pnr(capture: dict, pnr: str) -> dict:
    text = capture.get("text") or ""
    out = {"pnr": pnr, "found": pnr in text, "train": None, "train_name": None, "doj": None, "from": None, "to": None,
           "class": None, "chart_prepared": None, "passengers": [], "confidence": 0.2}
    if not out["found"]:
        return out
    m = re.search(r"\b(\d{5})\b\s*[-–:|]?\s*([A-Z][A-Z0-9 .'/-]{3,50})", text)
    if m:
        out["train"], out["train_name"] = m.group(1), m.group(2).strip()
    d = DATE.search(text)
    out["doj"] = d.group(1) if d else None
    m = re.search(r"\b(1A|2A|3A|3E|SL|CC|EC|2S|FC|EA)\b", text)
    out["class"] = m.group(1) if m else None
    m = re.search(r"from\s*[:\-]?\s*([A-Z]{2,5})\b.*?\bto\s*[:\-]?\s*([A-Z]{2,5})\b", text, re.I | re.S)
    if m:
        out["from"], out["to"] = m.group(1).upper(), m.group(2).upper()
    if re.search(r"chart\s+(?:not\s+(?:yet\s+)?prepared|status\s*[:\-]?\s*not\s+prepared)", text, re.I):
        out["chart_prepared"] = False
    elif re.search(r"chart\s+(?:is\s+)?prepared|chart\s+status\s*[:\-]?\s*prepared", text, re.I):
        out["chart_prepared"] = True
    # table with Booking / Current status columns
    for tb in capture.get("tables") or []:
        for hi, row in enumerate(tb[:3]):
            h = [c.lower() for c in row]
            bi = next((i for i, c in enumerate(h) if "booking" in c), None)
            ci = next((i for i, c in enumerate(h) if "current" in c), None)
            if bi is None or ci is None:
                continue
            for r in tb[hi + 1:]:
                if max(bi, ci) < len(r):
                    b, c = _pax(r[bi]), _pax(r[ci])
                    if b or c:
                        out["passengers"].append({"n": len(out["passengers"]) + 1, "booking": b, "current": c})
            break
    if not out["passengers"]:
        for m in re.finditer(r"passenger\s*(\d{1,2})\s*[:\-]?\s*(.+)", text, re.I):
            parts = [_pax(x) for x in re.split(r"\s{2,}|\t|\|", m.group(2))]
            parts = [p for p in parts if p]
            if parts:
                out["passengers"].append({"n": int(m.group(1)), "booking": parts[0], "current": parts[-1]})
    cur = [p["current"] for p in out["passengers"] if p.get("current")]
    out["all_confirmed"] = bool(cur) and all(c["status"] == "CNF" for c in cur)
    out["any_waitlisted"] = any("WL" in c["status"] for c in cur)
    out["confidence"] = 0.85 if out["passengers"] and out["train"] else 0.5 if out["passengers"] else 0.3
    return out
