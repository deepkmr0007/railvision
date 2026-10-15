"""Station reference + ambiguity-aware resolver (English / Hindi / Hinglish).

This bundled list is a small curated seed of major stations so search, NLU and tests work
offline. Production must load the full station master from an authorized dataset
(see docs/FEASIBILITY.md). Coordinates are approximate and for map display only.
"""
from __future__ import annotations

import re
import unicodedata

from .core.models import Station

_SEED: list[tuple] = [
    # code, name, hindi, lat, lon, aliases
    ("NDLS", "New Delhi", "नई दिल्ली", 28.642, 77.219, ("new delhi", "ndls", "delhi")),
    ("DLI", "Delhi Junction", "दिल्ली जंक्शन", 28.661, 77.227, ("old delhi", "delhi jn", "purani dilli", "delhi")),
    ("NZM", "Hazrat Nizamuddin", "हज़रत निज़ामुद्दीन", 28.588, 77.253, ("nizamuddin", "delhi")),
    ("DEE", "Delhi Sarai Rohilla", "दिल्ली सराय रोहिल्ला", 28.664, 77.186, ("sarai rohilla", "delhi")),
    ("ANVT", "Anand Vihar Terminal", "आनंद विहार टर्मिनल", 28.650, 77.316, ("anand vihar", "delhi")),
    ("DEC", "Delhi Cantt", "दिल्ली कैंट", 28.598, 77.135, ("delhi cantonment", "delhi")),
    ("GGN", "Gurugram", "गुरुग्राम", 28.470, 77.021, ("gurgaon", "gurugram")),
    ("RE", "Rewari", "रेवाड़ी", 28.190, 76.620, ("rewari", "rewadi")),
    ("AWR", "Alwar", "अलवर", 27.556, 76.606, ("alwar",)),
    ("BKI", "Bandikui Junction", "बांदीकुई", 27.051, 76.571, ("bandikui",)),
    ("JP", "Jaipur Junction", "जयपुर", 26.920, 75.787, ("jaipur", "jaipur jn")),
    ("GADJ", "Gandhinagar Jaipur", "गांधीनगर जयपुर", 26.887, 75.808, ("gandhinagar jaipur", "jaipur")),
    ("AII", "Ajmer Junction", "अजमेर", 26.457, 74.637, ("ajmer",)),
    ("JU", "Jodhpur Junction", "जोधपुर", 26.284, 73.022, ("jodhpur",)),
    ("BKN", "Bikaner Junction", "बीकानेर", 28.017, 73.317, ("bikaner",)),
    ("KOTA", "Kota Junction", "कोटा", 25.215, 75.865, ("kota",)),
    ("SWM", "Sawai Madhopur", "सवाई माधोपुर", 26.010, 76.355, ("sawai madhopur",)),
    ("MTJ", "Mathura Junction", "मथुरा", 27.480, 77.680, ("mathura",)),
    ("AGC", "Agra Cantt", "आगरा कैंट", 27.158, 77.990, ("agra",)),
    ("CNB", "Kanpur Central", "कानपुर सेंट्रल", 26.453, 80.350, ("kanpur",)),
    ("PRYJ", "Prayagraj Junction", "प्रयागराज", 25.446, 81.827, ("prayagraj", "allahabad")),
    ("BSB", "Varanasi Junction", "वाराणसी", 25.326, 82.987, ("varanasi", "banaras", "kashi")),
    ("PNBE", "Patna Junction", "पटना", 25.603, 85.137, ("patna",)),
    ("HWH", "Howrah Junction", "हावड़ा", 22.583, 88.342, ("howrah", "kolkata")),
    ("SDAH", "Sealdah", "सियालदह", 22.567, 88.370, ("sealdah", "kolkata")),
    ("MMCT", "Mumbai Central", "मुंबई सेंट्रल", 18.970, 72.820, ("mumbai central", "mumbai", "bombay central")),
    ("CSMT", "Chhatrapati Shivaji Maharaj Terminus", "छत्रपति शिवाजी महाराज टर्मिनस", 18.940, 72.835, ("cst", "mumbai", "vt")),
    ("BDTS", "Bandra Terminus", "बांद्रा टर्मिनस", 19.062, 72.841, ("bandra", "mumbai")),
    ("ADI", "Ahmedabad Junction", "अहमदाबाद", 23.026, 72.601, ("ahmedabad",)),
    ("BRC", "Vadodara Junction", "वडोदरा", 22.310, 73.181, ("vadodara", "baroda")),
    ("ST", "Surat", "सूरत", 21.205, 72.840, ("surat",)),
    ("PUNE", "Pune Junction", "पुणे", 18.529, 73.874, ("pune",)),
    ("SBC", "KSR Bengaluru", "बेंगलुरु", 12.978, 77.570, ("bengaluru", "bangalore")),
    ("MAS", "MGR Chennai Central", "चेन्नई सेंट्रल", 13.083, 80.275, ("chennai", "madras")),
    ("SC", "Secunderabad Junction", "सिकंदराबाद", 17.434, 78.501, ("secunderabad", "hyderabad")),
    ("BPL", "Bhopal Junction", "भोपाल", 23.267, 77.413, ("bhopal",)),
    ("NGP", "Nagpur Junction", "नागपुर", 21.152, 79.088, ("nagpur",)),
    ("CDG", "Chandigarh", "चंडीगढ़", 30.704, 76.820, ("chandigarh",)),
    ("ASR", "Amritsar Junction", "अमृतसर", 31.633, 74.866, ("amritsar",)),
    ("UMB", "Ambala Cantt", "अंबाला कैंट", 30.367, 76.833, ("ambala",)),
    ("LDH", "Ludhiana Junction", "लुधियाना", 30.912, 75.849, ("ludhiana",)),
    ("GHY", "Guwahati", "गुवाहाटी", 26.181, 91.751, ("guwahati",)),
    ("BBS", "Bhubaneswar", "भुवनेश्वर", 20.267, 85.843, ("bhubaneswar",)),
]

_CITY_HI = {"delhi": ("dilli", "दिल्ली"), "jaipur": ("जयपुर",), "mumbai": ("मुंबई", "bambai"), "kolkata": ("कोलकाता",)}

STATIONS: dict[str, Station] = {}
for c, n, h, la, lo, a in _SEED:
    extra = tuple(x for city, xs in _CITY_HI.items() if city in a for x in xs)
    STATIONS[c] = Station(code=c, name=n, name_hi=h, lat=la, lon=lo, aliases=a + extra)


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFC", s).lower().strip()
    s = re.sub(r"\b(railway station|station|stn|junction|jn|jn\.)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def register(st: Station) -> None:
    """Add/override a station (e.g. from an authorized station master or a provider route)."""
    STATIONS[st.code] = st


def resolve(query: str) -> list[Station]:
    """Return candidates, best first. Exact code wins outright; otherwise every station whose
    name / Hindi name / alias matches is returned so callers can ask the user to choose."""
    if not query:
        return []
    q = _norm(query)
    up = query.strip().upper()
    if up in STATIONS:
        return [STATIONS[up]]
    exact, partial = [], []
    for st in STATIONS.values():
        names = {_norm(st.name), _norm(st.name_hi), *(_norm(a) for a in st.aliases)}
        if q in names:
            exact.append(st)
        elif any(q and (q in n or (len(n) > 3 and n in q)) for n in names if n):
            partial.append(st)
    return exact or partial


def find_in_text(text: str) -> list[list[Station]]:
    """Find station mentions in free text, in order of appearance. Each item is a candidate
    list (len>1 = ambiguous, ask the user)."""
    t = " " + _norm(text) + " "
    raw = " " + text + " "
    hits: list[tuple[int, str, Station]] = []
    for st in STATIONS.values():
        for n in {st.name.lower(), st.name_hi, *st.aliases}:
            n2 = _norm(n)
            if not n2:
                continue
            m = re.search(r"(?<![\w])" + re.escape(n2) + r"(?![\w])", t)
            if m:
                hits.append((m.start(), n2, st))
        m = re.search(r"(?<![A-Za-z])" + re.escape(st.code) + r"(?![A-Za-z])", raw)
        if m and len(st.code) >= 2 and st.code not in ("ST", "SC"):
            hits.append((m.start(), st.code.lower(), st))
    hits.sort(key=lambda h: (h[0], -len(h[1])))
    groups: list[tuple[int, int, list[Station]]] = []
    for pos, phrase, st in hits:
        end = pos + len(phrase)
        overlap = next((g for g in groups if pos < g[1] and end > g[0]), None)
        if overlap is None:
            groups.append((pos, end, [st]))
        elif (pos, end) == (overlap[0], overlap[1]) and st not in overlap[2]:
            overlap[2].append(st)   # same words name several stations -> ambiguous
    out = []
    low_raw = raw.lower()
    for _, _, cands in groups:
        full = [s for s in cands if len(cands) > 1 and s.name.lower() in low_raw]
        out.append(full if len(full) == 1 else cands)   # "Jaipur Junction" typed in full -> JP only
    return out
