"""Delivery adapter contract + shared HTTP transport (stdlib, injectable for tests)."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

PostTransport = Callable[[str, dict, bytes, float], tuple[int, bytes]]

SENT, FAILED, SKIPPED, UNSUPPORTED, QUEUED = "SENT", "FAILED", "SKIPPED", "UNSUPPORTED", "QUEUED_FOR_ADMIN"


def urllib_post(url: str, headers: dict, body: bytes, timeout: float = 10.0) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
            return r.status, r.read(200_000)
    except urllib.error.HTTPError as e:
        return e.code, e.read(50_000)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return 0, json.dumps({"error": type(e).__name__}).encode()


@dataclass
class DeliveryResult:
    status: str
    detail: str = ""
    fallbacks: list[str] = field(default_factory=list)


class Destination:
    name = "base"

    def available(self) -> tuple[bool, str]:
        return True, ""

    def send(self, user_id: str, message: dict, task: dict | None) -> DeliveryResult:
        raise NotImplementedError


def mask_phone(p: str) -> str:
    return (p[:3] + "•" * max(0, len(p) - 7) + p[-4:]) if p and len(p) > 7 else "•••"
