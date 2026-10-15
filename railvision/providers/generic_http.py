"""Config-driven JSON/HTTPS adapter for an AUTHORIZED or LICENSED railway data provider.

Nothing in here is specific to a vendor: the endpoint paths and field mappings come from a
JSON config you fill in from that provider's official documentation and contract
(see config/provider.example.json — every value there is a placeholder, UNVERIFIED).

Built-in protections: per-source rate limit, response cache, circuit breaker, retry with
exponential backoff on 429/5xx, timeouts, secrets read from environment (never logged).
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime
from typing import Any, Callable

from ..core.models import LiveObservation, ObsKind, Route, ScheduledStop
from ..core.timeutil import IST, now_ist, parse_iso, scheduled_dt
from ..services.resilience import CircuitBreaker, TokenBucket, TTLCache
from .base import NotAuthorized, NotSupported, ProviderAdapter, ProviderError

Transport = Callable[[str, dict, float], tuple[int, bytes]]


def urllib_transport(url: str, headers: dict, timeout: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (https enforced below)
            return r.status, r.read(5_000_000)
    except urllib.error.HTTPError as e:
        return e.code, e.read(100_000)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ProviderError(f"network error: {type(e).__name__}") from e


def dig(obj: Any, path: str | None, default=None):
    """Dotted path lookup: 'data.stations[0].code'."""
    if not path:
        return default
    cur = obj
    for part in re.findall(r"[^.\[\]]+|\[\d+\]", path):
        if part.startswith("["):
            i = int(part[1:-1])
            if not isinstance(cur, list) or i >= len(cur):
                return default
            cur = cur[i]
        else:
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
    return cur


def _truthy(v) -> bool:
    return v in (True, 1, "1", "true", "True", "Y", "y", "yes")


class GenericHttpProvider(ProviderAdapter):
    def __init__(self, config: dict, transport: Transport = urllib_transport, clock=now_ist,
                 mono: Callable[[], float] = time.monotonic):
        self.cfg = config
        self.source_id = config["source_id"]
        self.display_name = config.get("display_name", self.source_id)
        self.official = bool(config.get("official", False))
        self.capabilities = config.get("capabilities", {})
        base = config["base_url"].rstrip("/")
        if not base.startswith("https://") and not config.get("allow_insecure_for_tests"):
            raise ValueError("provider base_url must be https://")
        self.base = base
        self.transport, self.clock = transport, clock
        self.bucket = TokenBucket(config.get("rate_limit_per_min", 30), 60.0, clock=mono)
        self.cache = TTLCache(clock=mono)
        self.breaker = CircuitBreaker(config.get("breaker_threshold", 4), config.get("breaker_cooldown_s", 120), clock=mono)
        self.timeout = float(config.get("timeout_s", 8))
        self.max_retries = int(config.get("max_retries", 2))
        self._sleep = config.get("_sleep", time.sleep)
        self.stats = {"requests": 0, "cache_hits": 0, "errors": 0, "last_ok": None}

    # ---- HTTP -----------------------------------------------------------------
    def _headers(self) -> dict:
        h = {"Accept": "application/json", "User-Agent": "RAILVISION-AI/2.0 (+authorized integration)"}
        auth = self.cfg.get("auth") or {}
        if auth.get("type") == "header":
            key = os.environ.get(auth["env"], "")
            if not key:
                raise NotAuthorized(f"{self.source_id}: missing credential env var {auth['env']}")
            h[auth["name"]] = (auth.get("prefix", "") + key)
        return h

    def _fetch(self, endpoint: str, params: dict) -> Any:
        ep = (self.cfg.get("endpoints") or {}).get(endpoint)
        if not ep:
            raise NotSupported(f"{self.source_id} does not provide {endpoint}", self.cfg.get("official_url"))
        path = ep["path"].format(**{k: urllib.parse.quote(str(v), safe="") for k, v in params.items()})
        url = self.base + path
        hit = self.cache.get(url)
        if hit is not None:
            self.stats["cache_hits"] += 1
            return hit
        if not self.breaker.allow():
            raise ProviderError(f"{self.source_id} circuit OPEN (recent failures); backing off")
        if not self.bucket.try_take():
            raise ProviderError(f"{self.source_id} local rate limit reached; request deferred")
        headers = self._headers()
        last = "unknown"
        for attempt in range(self.max_retries + 1):
            self.stats["requests"] += 1
            try:
                status, body = self.transport(url, headers, self.timeout)
            except ProviderError as e:
                last = str(e)
            else:
                if status in (401, 403):
                    self.breaker.failure(f"HTTP {status}")
                    raise NotAuthorized(f"{self.source_id}: HTTP {status} (credentials / entitlement)")
                if status == 404:
                    raise NotSupported(f"{self.source_id}: not found", self.cfg.get("official_url"))
                if status == 200:
                    try:
                        data = json.loads(body.decode("utf-8"))
                    except (ValueError, UnicodeDecodeError) as e:
                        last = f"malformed JSON: {e}"
                    else:
                        ok_path = self.cfg.get("success_path")
                        if ok_path and not _truthy(dig(data, ok_path)):
                            last = f"provider reported failure: {str(dig(data, self.cfg.get('error_path')))[:200]}"
                        else:
                            self.breaker.success()
                            self.stats["last_ok"] = now_ist().isoformat()
                            self.cache.set(url, data, ep.get("ttl", 60))
                            return data
                else:
                    last = f"HTTP {status}"
            if attempt < self.max_retries:
                self._sleep(min(8.0, 0.5 * (2 ** attempt)))
        self.stats["errors"] += 1
        self.breaker.failure(last)
        raise ProviderError(f"{self.source_id}: {last}")

    # ---- mapping helpers --------------------------------------------------------
    def _time(self, value, service_date: date, day) -> datetime | None:
        if value in (None, "", "--", "-", "SRC", "DSTN"):
            return None
        s = str(value)
        if re.fullmatch(r"\d{1,2}:\d{2}", s):
            return scheduled_dt(service_date, int(day or 1), s)
        try:
            return parse_iso(s)
        except ValueError:
            return None

    # ---- contract ------------------------------------------------------------
    def get_train_route(self, train_number: str, service_date: date) -> Route:
        m = self.cfg["map"]["route"]
        data = self._fetch("train_route", {"train_number": train_number, "date": service_date.isoformat()})
        rows = dig(data, m["list"]) or []
        if not rows:
            raise ProviderError(f"{self.source_id}: empty route payload")
        stops = []
        for i, row in enumerate(rows):
            arr, dep = dig(row, m.get("arr")), dig(row, m.get("dep"))
            arr = None if arr in ("", "--", "SRC", "Source") else arr
            dep = None if dep in ("", "--", "DSTN", "Destination") else dep
            halt = _truthy(dig(row, m["halt"])) if m.get("halt") else (arr is None or dep is None or arr != dep)
            stops.append(ScheduledStop(seq=i, code=str(dig(row, m["code"])).upper(), name=str(dig(row, m.get("name"), "")),
                                       km=float(dig(row, m.get("km"), 0) or 0), day=int(dig(row, m.get("day"), 1) or 1),
                                       arr=arr, dep=dep, halt=halt,
                                       platform=(str(dig(row, m["platform"])) if m.get("platform") and dig(row, m["platform"]) else None)))
        return Route(train_number=train_number, train_name=str(dig(data, m.get("train_name"), "")),
                     stops=stops, source_id=self.source_id)

    def get_train_status(self, train_number: str, service_date: date) -> list[LiveObservation]:
        m = self.cfg["map"]["status"]
        data = self._fetch("train_status", {"train_number": train_number, "date": service_date.isoformat()})
        now = self.clock()
        src_time = self._time(dig(data, m.get("updated_at")), service_date, 1) or now
        common = dict(train_number=train_number, service_date=service_date, source_id=self.source_id,
                      source_time=src_time, retrieved_at=now)
        if m.get("cancelled") and _truthy(dig(data, m["cancelled"])):
            return [LiveObservation(kind=ObsKind.CANCELLED, station_code=None, event_time=None, **common)]
        out: list[LiveObservation] = []
        for row in dig(data, m["list"]) or []:
            code = str(dig(row, m["code"]) or "").upper()
            day = dig(row, m.get("day"), 1)
            delay = dig(row, m.get("delay"))
            delay = int(delay) if isinstance(delay, (int, float)) or (isinstance(delay, str) and delay.lstrip("-").isdigit()) else None
            halt = _truthy(dig(row, m["halt"])) if m.get("halt") else True
            ta = self._time(dig(row, m.get("actual_arr")), service_date, day)
            td = self._time(dig(row, m.get("actual_dep")), service_date, day)
            arrived = _truthy(dig(row, m["has_arrived"])) if m.get("has_arrived") else ta is not None
            departed = _truthy(dig(row, m["has_departed"])) if m.get("has_departed") else td is not None
            plat = dig(row, m.get("platform"))
            payload = {"platform": str(plat)} if plat not in (None, "", "-") else {}
            if not halt and (departed or arrived) and (td or ta):
                out.append(LiveObservation(kind=ObsKind.PASSED, station_code=code, event_time=td or ta,
                                           delay_min=delay, payload=payload, **common))
                continue
            if arrived and ta:
                out.append(LiveObservation(kind=ObsKind.ARRIVED, station_code=code, event_time=ta,
                                           delay_min=delay, payload=payload, **common))
            if departed and td:
                out.append(LiveObservation(kind=ObsKind.DEPARTED, station_code=code, event_time=td,
                                           delay_min=delay, payload=payload, **common))
        pos_code = dig(data, m.get("position_station"))
        pos_km = dig(data, m.get("position_km_after"))
        if pos_code and pos_km not in (None, ""):
            out.append(LiveObservation(kind=ObsKind.POSITION, station_code=str(pos_code).upper(),
                                       event_time=src_time, km_after=float(pos_km), **common))
        if m.get("diverted_route") and dig(data, m["diverted_route"]):
            out.append(LiveObservation(kind=ObsKind.DIVERTED, station_code=None, event_time=src_time,
                                       payload={"route": [str(c).upper() for c in dig(data, m["diverted_route"])]}, **common))
        return out

    def get_source_health(self) -> dict:
        return {"source_id": self.source_id, "status": {"CLOSED": "OK", "HALF_OPEN": "DEGRADED", "OPEN": "DOWN"}[self.breaker.state],
                "breaker": self.breaker.state, "last_error": self.breaker.last_error, **self.stats}
