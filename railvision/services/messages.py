"""Message composition for every delivery channel (WhatsApp / Telegram / email / app).

Every field is filled from validated records. Unknown values print as "Unknown" — never guessed.
Demo data always carries a DEMO banner; in live mode demo payloads are refused outright.
"""
from __future__ import annotations

from ..core.timeutil import fmt_hm, fmt_human, parse_iso

EVENT_TYPES = ("STATUS_REPORT", "CONFIRMED_PASSAGE", "ESTIMATED_CROSSING", "SCHEDULED_HALT", "DELAY_ALERT",
               "CANCELLED", "DIVERTED", "STALE", "COMPLETED", "VACANCY_CHANGE", "NOT_VERIFIED")

DEMO_BANNER = "⚠️ DEMO / SYNTHETIC DATA — NOT LIVE"


class DemoInLiveModeError(RuntimeError):
    pass


def _event_label(crossing: dict | None, event_type: str) -> str:
    if event_type in ("DELAY_ALERT", "CANCELLED", "DIVERTED", "STALE", "COMPLETED", "VACANCY_CHANGE"):
        return event_type.replace("_", " ")
    if not crossing:
        return "RUNNING STATUS"
    ec = crossing["event_class"]
    if ec == "CONFIRMED_PASSAGE":
        return "CONFIRMED PASSAGE" + (" (time bracketed)" if crossing["verification"] == "DERIVED" else "")
    if ec == "ESTIMATED_CROSSING":
        return "ESTIMATED CROSSING (not verified)"
    if ec == "SCHEDULED_HALT":
        return "SCHEDULED HALT" + (" — ESTIMATED arrival" if crossing["verification"] == "ESTIMATED" else "")
    return ec.replace("_", " ")


def _event_time(c: dict | None) -> str:
    if not c:
        return "Unknown"
    if c.get("event_time"):
        return fmt_hm(parse_iso(c["event_time"])) + " IST (source-reported)"
    if c.get("est_earliest") and c.get("est_latest"):
        mid = f", best ~{fmt_hm(parse_iso(c['est_central']))}" if c.get("est_central") else ""
        return f"{fmt_hm(parse_iso(c['est_earliest']))}–{fmt_hm(parse_iso(c['est_latest']))} IST{mid}"
    if c.get("est_latest"):
        return f"before {fmt_hm(parse_iso(c['est_latest']))} IST"
    return "Not verified"


def compose(event_type: str, task: dict, status: dict, crossing: dict | None, app_url: str,
            mode: str, task_state: str = "Active", extra: str = "") -> dict:
    is_demo = bool(status.get("is_demo") or (crossing or {}).get("is_demo"))
    if is_demo and mode == "live":
        raise DemoInLiveModeError("refusing to send demo data in live mode")
    st_name = next((s["name"] for s in status.get("stops", []) if s["code"] == task["reference_station"]),
                   task["reference_station"])
    delay = status.get("delay_min")
    lines = []
    if is_demo:
        lines.append(DEMO_BANNER)
    lines += [
        "🚆 RAILVISION AI — TRAIN UPDATE",
        f"Train: {status['train_number']} {status.get('train_name') or ''}".rstrip(),
        f"Service date: {status['service_date']}",
        f"Reference station: {st_name} ({task['reference_station']})",
        f"Event: {_event_label(crossing, event_type)}",
        f"Event time: {_event_time(crossing)}",
        f"Delay: {('%+d min' % delay) if delay is not None else 'Unknown'}",
        f"Next scheduled stop: {status.get('next_stop') or 'Unknown'}",
        f"Last source update: {fmt_human(parse_iso((status.get('freshness') or {}).get('source_time')))}",
        f"Source: {(status.get('freshness') or {}).get('source') or 'Unknown'}",
        f"Status: {task_state}",
    ]
    if crossing and crossing.get("headline"):
        lines.insert(len(lines) - 1, f"Detail: {crossing['headline']}")
    if extra:
        lines.insert(len(lines) - 1, extra)
    if app_url:
        lines.append(f"Open dashboard: {app_url.rstrip('/')}/#train/{status['train_number']}")
    lines.append("Reply STOP to unsubscribe · HELP for commands")
    text = "\n".join(lines)
    label = _event_label(crossing, event_type)
    if event_type == "STATUS_REPORT":
        label = "Status report · " + label
    title = f"{'[DEMO] ' if is_demo else ''}{status['train_number']} · {label} · {task['reference_station']}"
    # short form for push / group (concise per spec)
    short = (f"{'[DEMO] ' if is_demo else ''}🚆 {status['train_number']} {task['reference_station']}: "
             f"{_event_label(crossing, event_type)} {_event_time(crossing)}; delay "
             f"{('%+d' % delay) if delay is not None else '?'} min; src {fmt_hm(parse_iso((status.get('freshness') or {}).get('source_time')))}")
    return {"title": title, "text": text, "short": short, "is_demo": is_demo,
            "template_params": [f"{status['train_number']} {status.get('train_name') or ''}".strip(),
                                status["service_date"], st_name, _event_label(crossing, event_type),
                                _event_time(crossing), ('%+d min' % delay) if delay is not None else "Unknown",
                                fmt_human(parse_iso((status.get('freshness') or {}).get('source_time'))), task_state]}


HELP_TEXT = """🚆 RAILVISION AI — commands (English / हिंदी / Hinglish)
• where is 12951  |  12951 kahan hai
• has it crossed Rewari?  |  Rewari cross hui?
• does 12951 stop at Rewari?
• track 12951 from Rewari to Delhi every 15 min
• alert me if delay exceeds 30 min
• pause / resume / stop tracking
• change interval to 30 min
• chart vacancy 12951 3A from NDLS
• PNR 4512345678 — check PNR (you type the CAPTCHA on your computer)
• my trips · announcements · news for 12951 · helpline
• search train rajdhani · rajdhani train khojo
• LINK 123456 — link this chat to your RAILVISION account
• STOP — unsubscribe · START — subscribe again · DELETE MY DATA
Live facts always come from the configured source with a timestamp."""
