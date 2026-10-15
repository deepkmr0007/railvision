"""Browser recipes: how to reach a result page on an official site.

A recipe is either
  * "auto"     — no recorded steps; the smart form-filler finds the train/date/station fields and
                 the submit button by their labels (works on many layouts, never guaranteed), or
  * "recorded" — steps captured by TEACH mode while YOU do the search once in a visible browser.

Values may contain placeholders: {train} {query} {station} {class} {date:<strftime>} e.g. {date:%d-%b-%Y}.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

NTES_URL = "https://enquiry.indianrail.gov.in/mntes/"
IRCTC_CHARTS_URL = "https://www.irctc.co.in/online-charts/"

# formats tried when matching / filling a date on an unknown page (Indian sites use DD first)
DATE_FORMATS = ["%d-%b-%Y", "%d-%m-%Y", "%d/%m/%Y", "%d %b %Y", "%d-%b-%y", "%d %b", "%d-%b", "%Y-%m-%d",
                "%a, %d %b", "%d %B %Y", "%d.%m.%Y", "%d%m%Y"]

PLACEHOLDER = re.compile(r"\{(train|query|station|class|date)(?::([^}]*))?\}")


@dataclass
class Step:
    action: str                     # goto | click | fill | select | press | wait | wait_text
    selector: str | None = None
    text: str | None = None         # visible text used to find the element (may contain placeholders)
    value: str | None = None        # fill/select value, url for goto, key for press
    timeout_ms: int = 15000
    optional: bool = False


@dataclass
class Recipe:
    name: str                       # ntes_status | ntes_search | irctc_berth | custom name
    kind: str                       # status | search | berth
    start_url: str
    mode: str = "auto"              # auto | recorded
    steps: list[Step] = field(default_factory=list)
    nav_texts: list[str] = field(default_factory=list)       # auto mode: menu links to try first
    result_texts: list[str] = field(default_factory=list)     # any of these (rendered) means results are shown
    verified: bool = False          # True only after a real successful run on the live site
    notes: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Recipe":
        d = dict(d)
        d["steps"] = [Step(**s) for s in d.get("steps", [])]
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    def save(self, path: Path):
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "Recipe":
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))


def render(template: str | None, vars_: dict) -> str | None:
    if template is None:
        return None

    def sub(m):
        key, fmt = m.group(1), m.group(2)
        v = vars_.get(key)
        if v is None:
            return m.group(0)
        if key == "date":
            d = v if isinstance(v, date) else date.fromisoformat(str(v))
            f = (fmt or "%d-%m-%Y").replace("%-d", str(d.day)).replace("%-m", str(d.month))  # portable no-pad
            return d.strftime(f)
        return str(v)
    return PLACEHOLDER.sub(sub, template)


def date_variants(d: date) -> list[str]:
    out = []
    for f in DATE_FORMATS:
        s = d.strftime(f)
        out.append(s)
        s2 = re.sub(r"\b0(\d)", r"\1", s)        # 05-Oct -> 5-Oct
        if s2 != s:
            out.append(s2)
    return list(dict.fromkeys(out))


def templatize(value: str, vars_: dict) -> str:
    """Turn a concrete recorded value into a template by replacing the sample values with placeholders."""
    if not value:
        return value
    out = value
    d = vars_.get("date")
    if d:
        d = d if isinstance(d, date) else date.fromisoformat(str(d))
        for f in sorted(DATE_FORMATS, key=lambda f: -len(d.strftime(f))):
            for s in {d.strftime(f), re.sub(r"\b0(\d)", r"\1", d.strftime(f))}:
                if s and s in out:
                    out = out.replace(s, "{date:%s}" % (f if s == d.strftime(f) else f.replace("%d", "%-d")))
                    break
            else:
                continue
            break
    for key in ("train", "query", "station", "class"):
        v = vars_.get(key)
        if v and str(v) in out and len(str(v)) >= 2:
            out = out.replace(str(v), "{%s}" % key)
    return out


DEFAULT_RECIPES = {
    "ntes_status": Recipe(
        name="ntes_status", kind="status", start_url=NTES_URL, mode="auto",
        nav_texts=["Spot Your Train", "Live Train Status", "Running Status", "Train Running Status", "Spot Train"],
        result_texts=["{train}"],
        notes="UNVERIFIED auto recipe. Run TEACH once on your computer to record the real NTES steps."),
    "ntes_search": Recipe(
        name="ntes_search", kind="search", start_url=NTES_URL, mode="auto",
        nav_texts=["Train Schedule", "Trains Between Stations", "Spot Your Train", "Live Train Status"],
        result_texts=[], notes="UNVERIFIED. Teach a train-name search once (type a name, pick nothing, capture the list)."),
    "irctc_berth": Recipe(
        name="irctc_berth", kind="berth", start_url=IRCTC_CHARTS_URL, mode="auto",
        nav_texts=[], result_texts=["{train}"],
        notes="UNVERIFIED auto recipe for IRCTC online charts (vacant berths). Teach once; IRCTC may show "
              "bot checks — the assistant will pause and hand the window to you, never solve them."),
}
