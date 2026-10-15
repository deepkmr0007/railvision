"""Polite fetchers for announcements.

* RSS/Atom feeds and official web pages: robots.txt is checked before every new path, conditional
  GET (ETag / Last-Modified) avoids re-downloading, a clear User-Agent names the app, 2 MB cap.
* X (Twitter): ONLY the official X API v2 with your own bearer token. Pay-per-use (checked
  15 Oct 2026: about $0.005 per post read, $0.010 per user lookup; same post re-read within a UTC
  day is not billed twice). A daily post budget you set is enforced before every call.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable

UA = "RAILVISION-AI/2.2 (personal railway announcements reader; respects robots.txt)"
MAX_BYTES = 2_000_000

# transport(url, headers) -> (status, headers_dict_lowercase, body_bytes)
Transport = Callable[[str, dict], tuple[int, dict, bytes]]


def urllib_get(url: str, headers: dict, timeout: float = 15.0) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read(MAX_BYTES)
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, e.read(20_000)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return 0, {}, str(type(e).__name__).encode()


@dataclass
class Item:
    guid: str
    title: str
    url: str
    text: str = ""
    published_at: str | None = None     # ISO-8601
    author: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def uid(self) -> str:
        return hashlib.sha256(self.guid.encode()).hexdigest()[:24]


class FetchError(Exception):
    def __init__(self, status: str, detail: str = ""):
        super().__init__(detail or status)
        self.status = status


class Robots:
    """robots.txt cache. A site with no robots.txt (404) allows everything; an unreachable one is retried later."""

    def __init__(self, transport: Transport, ttl_s: int = 86400, clock=time.time):
        self.transport, self.ttl, self.clock = transport, ttl_s, clock
        self._c: dict[str, tuple[float, urllib.robotparser.RobotFileParser | None]] = {}

    def allowed(self, url: str) -> bool:
        p = urllib.parse.urlsplit(url)
        root = f"{p.scheme}://{p.netloc}"
        hit = self._c.get(root)
        if not hit or self.clock() - hit[0] > self.ttl:
            st, _, body = self.transport(root + "/robots.txt", {"User-Agent": UA})
            rp = urllib.robotparser.RobotFileParser()
            if st == 200:
                rp.parse(body.decode("utf-8", "replace").splitlines())
            elif st in (401, 403):
                rp.disallow_all = True
            elif 400 <= st < 500:
                rp.allow_all = True
            else:
                raise FetchError("unreachable", f"robots.txt not reachable (HTTP {st})")
            self._c[root] = (self.clock(), rp)
            hit = self._c[root]
        return hit[1].can_fetch(UA, url)


def _clean(s: str | None) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def _date(s: str | None) -> str | None:
    if not s:
        return None
    s = s.strip()
    try:
        d = parsedate_to_datetime(s)
    except (TypeError, ValueError):
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.isoformat()


def parse_feed(body: bytes) -> list[Item]:
    txt = body.decode("utf-8", "replace")
    if re.search(r"<!DOCTYPE|<!ENTITY", txt[:2000], re.I):
        txt = re.sub(r"<!DOCTYPE[^>]*(\[[\s\S]*?\])?>", "", txt, count=1, flags=re.I)   # no entity expansion
        if "<!ENTITY" in txt.upper():
            raise FetchError("bad_feed", "feed declares XML entities; refused")
    try:
        root = ET.fromstring(txt.strip().lstrip("﻿"))
    except ET.ParseError as e:
        raise FetchError("bad_feed", f"not a valid RSS/Atom feed: {e}") from e
    ns = {"a": "http://www.w3.org/2005/Atom"}
    out: list[Item] = []
    for it in root.iter("item"):
        g = lambda t: (it.findtext(t) or "").strip()  # noqa: E731
        link = g("link")
        out.append(Item(guid=g("guid") or link or g("title"), title=_clean(g("title")), url=link,
                        text=_clean(g("description")), published_at=_date(g("pubDate") or g("{http://purl.org/dc/elements/1.1/}date"))))
    for e in root.iter("{http://www.w3.org/2005/Atom}entry"):
        link_el = e.find("a:link[@rel='alternate']", ns)
        if link_el is None:                      # (an Element with no children is falsy, so no `or`)
            link_el = e.find("a:link", ns)
        link = link_el.get("href") if link_el is not None else ""
        out.append(Item(guid=(e.findtext("a:id", default="", namespaces=ns) or link), title=_clean(e.findtext("a:title", default="", namespaces=ns)),
                        url=link, text=_clean(e.findtext("a:summary", default="", namespaces=ns) or e.findtext("a:content", default="", namespaces=ns)),
                        published_at=_date(e.findtext("a:updated", default="", namespaces=ns) or e.findtext("a:published", default="", namespaces=ns))))
    return [i for i in out if i.title or i.url]


A_RE = re.compile(r"<a\b[^>]*?href\s*=\s*([\"'])(?P<href>[^\"'#]+)\1[^>]*>(?P<text>[\s\S]*?)</a>", re.I)
SKIP_TEXT = re.compile(r"^(home|login|sign ?in|contact( us)?|sitemap|faq|feedback|screen reader|skip to|hindi|english|"
                       r"हिन्दी|about( us)?|disclaimer|privacy|terms|help|search|more|read more|click here|archive)$", re.I)


def parse_web(body: bytes, base_url: str) -> list[Item]:
    """Turn an official notices page into items: each meaningful link (notice, press release, PDF) is one item."""
    txt = body.decode("utf-8", "replace")
    txt = re.sub(r"<(script|style|nav|footer)\b[\s\S]*?</\1>", " ", txt, flags=re.I)
    out, seen = [], set()
    for m in A_RE.finditer(txt):
        title = _clean(m.group("text"))
        href = urllib.parse.urljoin(base_url, html.unescape(m.group("href").strip()))
        if not href.startswith(("http://", "https://")) or len(title) < 12 or SKIP_TEXT.match(title):
            continue
        if href in seen:
            continue
        seen.add(href)
        out.append(Item(guid=href, title=title[:300], url=href))
    return out[:200]


class FeedFetcher:
    def __init__(self, transport: Transport = urllib_get, robots: Robots | None = None):
        self.transport = transport
        self.robots = robots or Robots(transport)

    def fetch(self, url: str, kind: str, etag: str | None = None, last_modified: str | None = None):
        """Returns (items | None if not modified, new_etag, new_last_modified)."""
        if not url.startswith("https://") and not url.startswith("http://"):
            raise FetchError("bad_url", "URL must start with https://")
        if not self.robots.allowed(url):
            raise FetchError("robots_disallow", "This site's robots.txt asks automated readers not to fetch this page. "
                                                "RAILVISION respects that — open it yourself instead.")
        h = {"User-Agent": UA, "Accept": "application/rss+xml, application/atom+xml, text/html;q=0.9, */*;q=0.5"}
        if etag:
            h["If-None-Match"] = etag
        if last_modified:
            h["If-Modified-Since"] = last_modified
        st, hd, body = self.transport(url, h)
        if st == 304:
            return None, etag, last_modified
        if st in (401, 403, 429):
            raise FetchError("refused", f"HTTP {st} — the site refused; backing off")
        if st != 200:
            raise FetchError("http_error", f"HTTP {st}")
        items = parse_feed(body) if kind == "rss" else parse_web(body, url)
        return items, hd.get("etag"), hd.get("last-modified")


class XClient:
    """Official X API v2, app-only bearer token. Reads a public account's recent posts."""
    BASE = "https://api.x.com/2"
    PRICE_POST, PRICE_USER = 0.005, 0.010          # USD, pay-per-use (checked 15 Oct 2026)

    def __init__(self, token: str, transport: Transport = urllib_get):
        self.token, self.transport = token, transport

    def _get(self, path: str, params: dict) -> dict:
        url = f"{self.BASE}{path}?{urllib.parse.urlencode(params)}" if params else f"{self.BASE}{path}"
        st, hd, body = self.transport(url, {"Authorization": f"Bearer {self.token}", "User-Agent": UA})
        if st == 429:
            raise FetchError("rate_limited", f"X API rate limit; resets at {hd.get('x-rate-limit-reset', '?')}")
        if st in (401, 403):
            raise FetchError("refused", f"X API HTTP {st}: check the bearer token / credits")
        if st != 200:
            raise FetchError("http_error", f"X API HTTP {st}")
        try:
            return json.loads(body)
        except ValueError as e:
            raise FetchError("bad_json", str(e)) from e

    def user_id(self, handle: str) -> str:
        d = self._get(f"/users/by/username/{urllib.parse.quote(handle.lstrip('@'))}", {})
        if not d.get("data", {}).get("id"):
            raise FetchError("not_found", f"X account @{handle} not found")
        return d["data"]["id"]

    def recent_posts(self, uid: str, handle: str, since_id: str | None, max_results: int) -> list[Item]:
        p = {"max_results": max(5, min(100, max_results)), "tweet.fields": "created_at", "exclude": "replies,retweets"}
        if since_id:
            p["since_id"] = since_id
        d = self._get(f"/users/{uid}/tweets", p)
        out = []
        for t in d.get("data", []) or []:
            out.append(Item(guid=f"x:{t['id']}", title=_clean(t.get("text", ""))[:280], text=_clean(t.get("text", "")),
                            url=f"https://x.com/{handle.lstrip('@')}/status/{t['id']}", published_at=_date(t.get("created_at")),
                            author="@" + handle.lstrip("@"), raw={"id": t["id"]}))
        return out
