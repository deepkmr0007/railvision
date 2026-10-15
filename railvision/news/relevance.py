"""What an announcement is about, and whether it matters to YOU.

Tags: 5-digit train numbers, stations (codes and names, English + Hindi), railway zones,
and a category (cancellation, diversion, weather, special train, ...).
Score: 1.0 your train · 0.8 your station with a disruption · 0.7 disruption in your station's city ·
0.6 your station · 0.5 your station's city · 0.4 your zone
with a disruption · else 0. Alerts go out at ≥ 0.8 by default (adjustable); the feed shows everything.
"""
from __future__ import annotations

import re

from .. import stations as stn

CATEGORIES = [
    ("cancellation", r"\bcancel+(ed|ation|s)?\b|रद्द|निरस्त"),
    ("diversion", r"\bdivert(ed|ion)?\b|परिवर्तित मार्ग|डायवर्ट"),
    ("reschedule", r"\breschedul\w*|late start|पुनर्निर्धारित"),
    ("short_termination", r"short[- ]terminat\w*|short[- ]originat\w*|आंशिक रूप से रद्द"),
    ("regulation", r"\bregulat(ed|ion)\b|traffic block|line block|mega ?block|power block|ब्लॉक"),
    ("weather", r"\bfog\b|\bflood|heavy rain|cyclone|landslide|water ?logging|कोहरा|बाढ़"),
    ("special_train", r"special trains?|festival special|summer special|holiday special|विशेष (गाड़ी|ट्रेन)"),
    ("extra_coach", r"(additional|extra) coach|augment\w*|अतिरिक्त (कोच|डिब्बे)"),
    ("platform", r"platform (change|no\.?)|प्लेटफार्म"),
    ("ticketing", r"\btatkal\b|\bfare\b|refund|\bpnr\b|ticket|irctc|किराया|टिकट"),
    ("safety", r"accident|derail|\bfire\b|rescue|दुर्घटना"),
    ("new_service", r"vande bharat|amrit bharat|namo bharat|inaugurat\w*|flag(ged)? off|नई (गाड़ी|ट्रेन)"),
]
DISRUPTIVE = {"cancellation", "diversion", "reschedule", "short_termination", "regulation", "weather"}
RAIL_WORDS = re.compile(r"\b(rail\w*|train\w*|irctc|station|loco\w*|vande bharat|coach\w*|metro|rpf|zone|rajdhani|shatabdi|"
                        r"duronto|garib rath|humsafar|tejas|sampark kranti|intercity|express|mail|memu|demu|ntes|pnr|tatkal|"
                        r"platform|chart)\b|रेल|गाड़ी|ट्रेन|स्टेशन|एक्सप्रेस", re.I)
ZONES = {"NR": "Northern", "NWR": "North Western", "NCR": "North Central", "NER": "North Eastern", "NFR": "Northeast Frontier",
         "CR": "Central", "WR": "Western", "WCR": "West Central", "ER": "Eastern", "ECR": "East Central", "ECOR": "East Coast",
         "SR": "Southern", "SCR": "South Central", "SWR": "South Western", "SER": "South Eastern", "SECR": "South East Central",
         "KR": "Konkan", "SCOR": "South Coast"}
ZONE_RE = re.compile(r"\b(" + "|".join(sorted(map(re.escape, ZONES.values()), key=len, reverse=True)) + r")\s+Railway\b", re.I)


def is_railway(text: str) -> bool:
    return bool(RAIL_WORDS.search(text))


def tag(text: str, extra_station_codes: set[str] | None = None) -> dict:
    t = text or ""
    # 5-digit train numbers; skip numbers that are part of dates, PRIDs, phone numbers or amounts
    trains = sorted(set(re.findall(r"(?<![\d/.,:+₹-])(\d{5})(?![\d/.,:-])", t)))
    st: set[str] = set()
    low = t.lower()
    alias_count: dict[str, int] = {}
    for s in stn.STATIONS.values():
        for a in set(x.lower() for x in s.aliases):
            alias_count[a] = alias_count.get(a, 0) + 1
    for s in stn.STATIONS.values():
        # a city name shared by several stations ("Mumbai", "Delhi") is not a station mention
        names = [s.name.lower()] + [a.lower() for a in s.aliases if len(a) >= 5 and alias_count.get(a.lower()) == 1] + \
                ([s.name_hi] if s.name_hi else [])
        if any(n and re.search(r"(?<![\w])" + re.escape(n.lower()) + r"(?![\w])", low) for n in names):
            st.add(s.code)
    # a shared city name ("Delhi", "Jaipur", "Mumbai") is a CITY mention: every station of that city
    cities: set[str] = set()
    for a, n in alias_count.items():
        if n > 1 and len(a) >= 4 and re.search(r"(?<![\w])" + re.escape(a) + r"(?![\w])", low):
            cities |= {s.code for s in stn.STATIONS.values() if a in (x.lower() for x in s.aliases)}
    codes = set(re.findall(r"\b([A-Z]{3,5})\b", t))
    for c in codes & (set(stn.STATIONS) | (extra_station_codes or set())):
        st.add(c)
    cats = [name for name, pat in CATEGORIES if re.search(pat, t, re.I)]
    zones = sorted({m.group(1).title() for m in ZONE_RE.finditer(t)})
    return {"trains": trains, "stations": sorted(st), "city_stations": sorted(cities - st), "categories": cats or ["general"],
            "zones": zones}


def score(tags: dict, interests: dict) -> tuple[float, str]:
    """interests = {"trains": set, "stations": set, "zones": set}"""
    tr = set(tags["trains"]) & interests.get("trains", set())
    if tr:
        return 1.0, f"mentions your train {', '.join(sorted(tr))}"
    disrupt = bool(DISRUPTIVE & set(tags["categories"]))
    st = set(tags["stations"]) & interests.get("stations", set())
    if st and disrupt:
        return 0.8, f"disruption at your station {', '.join(sorted(st))}"
    if st:
        return 0.6, f"mentions your station {', '.join(sorted(st))}"
    city = set(tags.get("city_stations", [])) & interests.get("stations", set())
    if city:
        return (0.7 if disrupt else 0.5), f"{'disruption ' if disrupt else ''}in your station's city ({', '.join(sorted(city))})"
    zn = set(tags.get("zones", [])) & interests.get("zones", set())
    if zn and disrupt:
        return 0.4, f"disruption in {', '.join(sorted(zn))} Railway"
    return 0.0, ""
