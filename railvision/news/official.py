"""Official Indian Railways zonal websites: press-release / news pages, and reading announcement details.

The zonal portals (xx.indianrailways.gov.in) share one CMS: a section page lists items, and each
item opens at view_detail.jsp?...&dcd=<id>, or as a PDF under /uploads/. Section ids differ per zone.

URL evidence, 15 Oct 2026: "verified" means the page appeared in web-search results with the
title shown. Their robots.txt refused our automated fetch tool for every zone page we tried, so
RAILVISION reads them directly only where robots.txt allows. Otherwise, if you switch a page to
"browser assistant", your own browser reads it, like you would.
"""
from __future__ import annotations

import html
import io
import re

LINK_FILTER = r"view_detail\.jsp|\.pdf(\?|$)|/uploads/"

# code, zone (as used by relevance.ZONES), url, verified-evidence
ZONE_PAGES = [
    ("nr", "Northern", "https://nr.indianrailways.gov.in/view_section.jsp?lang=0&id=0,4,268", "search: 'Press Releases - Northern Railways'"),
    ("wr", "Western", "https://wr.indianrailways.gov.in/view_section.jsp?id=0%2C4%2C268&lang=0", "search: Western Railway press items (view_detail …id=0,4,268)"),
    ("ner", "North Eastern", "https://ner.indianrailways.gov.in/view_section.jsp?lang=0&id=0%2C4%2C268", "search: 'North Eastern Railway'"),
    ("swr", "South Western", "https://swr.indianrailways.gov.in/view_section.jsp?lang=0&id=0%2C4%2C268", "search: 'Press Release - South Western Railway'"),
    ("scr", "South Central", "https://scr.indianrailways.gov.in/view_section.jsp?lang=0&id=0%2C5%2C268", "search: 'Press Releases - South Central Railway'"),
    ("ser", "South Eastern", "https://ser.indianrailways.gov.in/view_section.jsp?lang=0&id=0%2C4%2C423", "search: 'Press Release - South Eastern Railway'"),
    ("cr", "Central", "https://cr.indianrailways.gov.in/cris/view_section.jsp?lang=0&id=0,4,268", "search: 'Central Railway' at this address"),
    ("nwr", "North Western", "https://nwr.indianrailways.gov.in/view_section.jsp?lang=0&id=0,4,268", None),
    ("ncr", "North Central", "https://ncr.indianrailways.gov.in/view_section.jsp?lang=0&id=0,4,268", None),
    ("ecr", "East Central", "https://ecr.indianrailways.gov.in/view_section.jsp?lang=0&id=0,4,268", None),
    ("er", "Eastern", "https://er.indianrailways.gov.in/view_section.jsp?lang=0&id=0,4,268", None),
    ("sr", "Southern", "https://sr.indianrailways.gov.in/view_section.jsp?lang=0&id=0,4,268", None),
    ("wcr", "West Central", "https://wcr.indianrailways.gov.in/view_section.jsp?lang=0&id=0,4,268", None),
    ("secr", "South East Central", "https://secr.indianrailways.gov.in/view_section.jsp?lang=0&id=0,4,268", None),
    ("ecor", "East Coast", "https://eastcoastrail.indianrailways.gov.in/view_section.jsp?lang=0&id=0,4,268", None),
    ("nfr", "Northeast Frontier", "https://nfr.indianrailways.gov.in/view_section.jsp?lang=0&id=0,4,268", None),
]


def html_to_text(body: bytes | str, title_hint: str = "") -> str:
    """Main text of an announcement page: drop menus/scripts, then start at the title if it's on the page."""
    t = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
    t = re.sub(r"<(script|style|nav|header|footer|noscript|select)\b[\s\S]*?</\1>", " ", t, flags=re.I)
    t = re.sub(r"<(ul|ol)\b[^>]*(menu|nav|breadcrumb)[^>]*>[\s\S]*?</\1>", " ", t, flags=re.I)
    t = re.sub(r"<br\s*/?>|</p>|</div>|</tr>|</li>|</h\d>", "\n", t, flags=re.I)
    t = html.unescape(re.sub(r"<[^>]+>", " ", t))
    lines = [re.sub(r"[ \t\xa0]+", " ", ln).strip() for ln in t.splitlines()]
    text = "\n".join(ln for ln in lines if ln)
    return focus(text, title_hint)


def focus(text: str, title_hint: str = "") -> str:
    if title_hint:
        key = title_hint[:40].lower()
        i = text.lower().find(key)
        if i >= 0:
            text = text[i:]
    return text[:6000]


def pdf_to_text(data: bytes, max_pages: int = 6) -> tuple[str, str | None]:
    """Text of a notice PDF. Needs `pypdf` (pip install pypdf); scanned PDFs have no text layer."""
    if not data.lstrip()[:5].startswith(b"%PDF"):
        return "", "not a PDF file"
    try:
        from pypdf import PdfReader
        import logging
        logging.getLogger("pypdf").setLevel(logging.ERROR)
    except ImportError:
        return "", "PDF text needs the pypdf package (pip install pypdf)"
    try:
        r = PdfReader(io.BytesIO(data))
        if r.is_encrypted:
            return "", "PDF is encrypted"
        parts = [(p.extract_text() or "") for p in r.pages[:max_pages]]
    except Exception as e:                       # malformed PDF
        return "", f"PDF could not be read ({type(e).__name__})"
    text = re.sub(r"[ \t]+", " ", "\n".join(parts)).strip()
    if not text:
        return "", "PDF has no text layer (probably a scanned image)"
    return text[:6000], None


def is_pdf(url: str, content_type: str = "", head: bytes = b"") -> bool:
    return head.startswith(b"%PDF") or "pdf" in (content_type or "").lower() or bool(re.search(r"\.pdf(\?|$)", url, re.I))
