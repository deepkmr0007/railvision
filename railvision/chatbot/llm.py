"""Optional LLM intent parser (tool-calling). Disabled unless ANTHROPIC_API_KEY and RAILVISION_LLM_MODEL are set.

The model is only asked to CLASSIFY the request into the same structured intent schema the
rule-based parser produces. It never supplies train positions, times, delays or vacancy —
those always come from provider tools. Output is validated against the schema; anything
invalid is discarded.
"""
from __future__ import annotations

import json
import os

from ..delivery.base import urllib_post

INTENTS = ["where", "crossed", "estimate", "stops_at", "track", "delay_alert", "vacancy", "between",
           "board", "pause", "resume", "cancel", "change_interval", "help", "unknown"]

TOOL = {
    "name": "classify_railway_request",
    "description": "Classify an Indian Railways user request (English/Hindi/Hinglish) into a structured intent. "
                   "Do not answer the question; only extract fields.",
    "input_schema": {
        "type": "object",
        "properties": {
            "intent": {"type": "string", "enum": INTENTS},
            "train_number": {"type": ["string", "null"], "pattern": "^[0-9]{5}$"},
            "stations": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
            "interval_min": {"type": ["integer", "null"], "minimum": 5, "maximum": 240},
            "delay_threshold_min": {"type": ["integer", "null"], "minimum": 1, "maximum": 1440},
            "travel_class": {"type": ["string", "null"]},
        },
        "required": ["intent"],
    },
}


class LLMIntentParser:
    def __init__(self, api_key: str | None = None, model: str | None = None, transport=urllib_post):
        self.key = api_key if api_key is not None else os.environ.get("ANTHROPIC_API_KEY", "")
        self.model = model if model is not None else os.environ.get("RAILVISION_LLM_MODEL", "")
        self.transport = transport

    @property
    def enabled(self) -> bool:
        return bool(self.key and self.model)

    def parse(self, text: str) -> dict | None:
        if not self.enabled:
            return None
        body = {"model": self.model, "max_tokens": 300, "tools": [TOOL],
                "tool_choice": {"type": "tool", "name": TOOL["name"]},
                "messages": [{"role": "user", "content": text[:1000]}]}
        status, raw = self.transport("https://api.anthropic.com/v1/messages",
                                     {"x-api-key": self.key, "anthropic-version": "2023-06-01",
                                      "content-type": "application/json"}, json.dumps(body).encode(), 20.0)
        if status != 200:
            return None
        try:
            data = json.loads(raw)
            block = next(b for b in data.get("content", []) if b.get("type") == "tool_use")
            out = block["input"]
        except (ValueError, StopIteration, KeyError, TypeError):
            return None
        return validate(out)


def validate(out: dict) -> dict | None:
    if not isinstance(out, dict) or out.get("intent") not in INTENTS:
        return None
    tn = out.get("train_number")
    if tn is not None and not (isinstance(tn, str) and tn.isdigit() and len(tn) == 5):
        out["train_number"] = None
    iv = out.get("interval_min")
    if iv is not None and not (isinstance(iv, int) and 5 <= iv <= 240):
        out["interval_min"] = None
    return out
