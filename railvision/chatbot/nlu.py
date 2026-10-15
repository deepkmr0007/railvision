"""Deterministic intent parser — English, Hindi (Devanagari) and Hinglish.

The NLU only extracts intent + slots. Live facts are ALWAYS fetched by backend tools;
nothing about train positions is ever produced from a language model's memory.
An optional LLM (chatbot/llm.py) may be used for harder phrasing, but it must return the
same structured intent JSON and is validated before use.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from .. import stations as stn

DEV_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")

INTENT_PATTERNS: list[tuple[str, str]] = [
    ("helpline", r"helpline|emergency|complain|rail ?madad|\brpf\b|security|police|शिकायत|हेल्पलाइन|आपात"),
    ("pnr", r"\bpnr\b|(?<!\d)\d{10}(?!\d)"),
    ("trips", r"my (trips?|journeys?|travels?|bookings?)|meri yatra|मेरी यात्रा|upcoming (trips?|journeys?)"),
    ("news", r"announcement|\bnews\b|khabar|ख़बर|खबर|सूचना|notices?\b|any updates|kya update"),
    ("help", r"^/?(help|start)$|\bhelp\b|commands|madad|मदद|सहायता"),
    ("link", r"^link\s+\d{6}$"),
    ("confirm", r"^(yes|y|haan|han|ha|haa|ji|ok|okay|confirm|sure|theek hai|thik hai|हाँ|हां|जी|ठीक है)[.! ]*$"),
    ("decline", r"^(no|n|nahi|nahin|na|mat karo|नहीं|ना)[.! ]*$"),
    ("pause", r"\bpause\b|\bhold\b|ruk(o|ja)?\b|रोको|रुको|थाम"),
    ("resume", r"\bresume\b|fir se|phir se|chalu karo|चालू|फिर से"),
    ("cancel", r"stop (updates|tracking|alerts)|cancel|band karo|bandh karo|बंद करो|रद्द|unsubscribe|stop\b.*\btrain"),
    ("change_interval", r"(change|set|update|badlo|बदलो).{0,20}(interval|frequency|har)|interval\s*(to)?\s*\d"),
    ("delay_alert", r"(alert|notify|bata(na|o)|बताना|बताओ).{0,40}(delay|late|लेट|देरी)|(delay|late|लेट).{0,30}(exceed|more than|over|zyada|jyada|ज़्यादा|ज्यादा|>)"),
    ("vacancy", r"vacan|vacant|khali berth|seat khali|खाली|\bberths?\b|\bchart\b"),
    ("search", r"^(search|find|look ?up|khojo|dhundo|खोजो|ढूंढो)\b(?!.*\d{5})|(search|find|khojo|dhundo)\s+(for\s+)?trains?\s+(named|called|name)|train(s)? (named|called)|naam se|\b(khojo|dhundo|खोजो|ढूंढो)[.!? ]*$"),
    ("between", r"trains? (from|between)|between .+ and |gaadiyan|गाड़ियाँ"),
    ("board", r"\b(board|arrivals?|departures?)\b|station (status|board)"),
    ("track", r"\btrack\b|monitor|watch|nazar|नज़र|नजर|ट्रैक|updates? (bhejo|do|send)|keep me (posted|updated)|every \d+|har \d+"),
    ("estimate", r"estimate|when will|kab (tak|pahunch|aayegi|cross)|कब|\beta\b"),
    ("crossed", r"cross(ed)?|pass(ed)? (by|through)?|paar|पार|निकल|nikal|guzar|गुज़र|गुजर"),
    ("stops_at", r"(does|will).{0,30}\bstop\b|\bhalt\b|ruk(ti|egi|ta)|रुकती|रुकेगी|ठहराव|thahrav"),
    ("where", r"where|kaha(n|an)?\b|कहाँ|कहां|status|running|location|live|abhi"),
]


@dataclass
class Parsed:
    intent: str
    train: str | None = None
    stations: list[list] = field(default_factory=list)   # candidate lists in order of mention
    interval_min: int | None = None
    threshold_min: int | None = None
    service_date: date | None = None
    date_ambiguous: bool = False
    travel_class: str | None = None
    link_code: str | None = None
    query: str | None = None
    pnr: str | None = None
    lang: str = "en"
    raw: str = ""


HINGLISH_MARKERS = re.compile(r"\b(hai|kahan|kaha|karo|kab|kya|ki|ke|se|tak|har|bhejo|gaadi|train ko)\b")


def detect_lang(text: str) -> str:
    if re.search(r"[ऀ-ॿ]", text):
        return "hi"
    if HINGLISH_MARKERS.search(text.lower()):
        return "hinglish"
    return "en"


def parse(text: str, today: date) -> Parsed:
    raw = text.strip()
    t = raw.translate(DEV_DIGITS)
    low = t.lower()
    intent = "unknown"
    cmd = re.search(r"\b(pause|resume|cancel|stop)\b\s+(this\s+|the\s+|my\s+)?(tracking|updates|alerts|monitoring)", low)
    patterns = INTENT_PATTERNS if not cmd else [({"stop": "cancel"}.get(cmd.group(1), cmd.group(1)), r".")]
    # track before pause/resume so "track ... continue" isn't read as resume
    for name, pat in patterns:
        if re.search(pat, low):
            intent = name
            break
    # "track ... every N" beats "change interval" when a train number is present
    m_train = re.search(r"(?<!\d)(\d{5})(?!\d)", t)
    if intent == "change_interval" and m_train and re.search(r"\btrack|monitor|ट्रैक", low):
        intent = "track"
    if intent == "unknown" and m_train:
        intent = "where"          # a bare train number (or "search 12951") means "show its status"
    p = Parsed(intent=intent, train=m_train.group(1) if m_train else None, raw=raw, lang=detect_lang(raw))
    if intent == "search":
        q = re.sub(r"^(please\s+)?(search|find|look ?up|khojo|dhundo|खोजो|ढूंढो)\s+(for\s+)?(the\s+)?(trains?\s+)?(named|called|name)?\s*", "", low).strip(" ?.")
        words = q.split()
        while words and words[-1].strip("?.!") in {"khojo", "dhundo", "खोजो", "ढूंढो", "ko", "train", "trains", "gaadi", "naam", "se", "please"}:
            words.pop()
        q = " ".join(words).strip()
        p.query = q or None
    if intent == "pnr":
        m = re.search(r"(?<!\d)(\d{10})(?!\d)", t)
        p.pnr = m.group(1) if m else None
    if intent == "link":
        p.link_code = re.search(r"\d{6}", t).group(0)

    m = re.search(r"(?:every|har|interval(?: to)?|each)\s*(\d{1,3})\s*(min|mins|minute|minutes|मिनट|m\b)?", low) \
        or re.search(r"(\d{1,3})\s*(min|mins|minute|minutes|मिनट)\b", low)
    if m and intent in ("track", "change_interval"):
        p.interval_min = int(m.group(1))
    m = re.search(r"(?:every|har)\s*(\d{1,2})\s*(hour|hours|hr|hrs|ghante|ghanta|घंटे|घंटा)", low)
    if m and intent in ("track", "change_interval"):
        p.interval_min = int(m.group(1)) * 60
    if intent == "delay_alert":
        m = re.search(r"(\d{1,3})\s*(min|mins|minute|minutes|मिनट)?", re.sub(r"\d{5}", "", low))
        if m:
            p.threshold_min = int(m.group(1))

    if re.search(r"\b(today|aaj)\b|आज", low):
        p.service_date = today
    elif re.search(r"\byesterday\b|कल रात", low):
        p.service_date = today - timedelta(days=1)
    elif re.search(r"\btomorrow\b|परसों", low):
        p.service_date = today + timedelta(days=1)
    elif re.search(r"\bkal\b|\bकल\b", low):
        p.date_ambiguous = True       # Hindi "kal" = yesterday OR tomorrow
    m = re.search(r"\b(20\d\d)-(\d\d)-(\d\d)\b", t) or None
    if m:
        p.service_date = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    else:
        m = re.search(r"\b(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{2,4}))?\b", t)
        if m:
            y = int(m.group(3)) if m.group(3) else today.year
            y = y + 2000 if y < 100 else y
            try:
                p.service_date = date(y, int(m.group(2)), int(m.group(1)))   # Indian DD/MM
            except ValueError:
                pass

    m = re.search(r"\b(1A|2A|3A|3E|SL|CC|EC|2S|EA|FC)\b", t.upper())
    if m:
        p.travel_class = m.group(1)
    p.stations = stn.find_in_text(re.sub(r"(?<!\d)\d{5}(?!\d)", " ", t))
    return p
