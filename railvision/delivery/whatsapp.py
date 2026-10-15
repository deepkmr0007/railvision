"""WhatsApp delivery via the OFFICIAL Meta WhatsApp Business Platform (Cloud API) only.

Capabilities researched 14 Oct 2026 — the three WhatsApp targets are NOT the same API:

1. One-to-one  — supported. Opt-in required. Free-form text only inside the 24 h customer-service
                 window after the user's last message; outside it an APPROVED TEMPLATE is required.
2. Groups      — Meta Groups API: Official Business Accounts (OBA) only, invite-only groups the
                 business creates, max 8 participants, per-message pricing. It cannot post into an
                 arbitrary existing WhatsApp group. Gated by WA_OBA_VERIFIED=true; otherwise we fall back.
3. Channels    — no official publishing API found. Only unofficial client-emulation gateways claim
                 this; they are excluded by policy (account-ban risk, ToS). We queue copy-ready posts
                 for a channel admin instead.

Never used: WhatsApp Web automation, QR-session hijacking, cookie extraction, unofficial libraries.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta

from ..core.timeutil import now_ist, parse_iso
from .base import (FAILED, QUEUED, SENT, SKIPPED, UNSUPPORTED, DeliveryResult, Destination, PostTransport,
                   mask_phone, urllib_post)


@dataclass
class WhatsAppConfig:
    phone_number_id: str = ""
    access_token: str = ""
    app_secret: str = ""
    verify_token: str = ""
    graph_version: str = ""          # set to the CURRENT version from Meta docs, e.g. "vXX.0"
    template_name: str = ""          # approved utility template for out-of-window updates
    template_lang: str = "en"
    oba_verified: bool = False
    max_replay_age_s: int = 600

    @classmethod
    def from_env(cls) -> "WhatsAppConfig":
        e = os.environ.get
        return cls(e("WA_PHONE_NUMBER_ID", ""), e("WA_ACCESS_TOKEN", ""), e("WA_APP_SECRET", ""),
                   e("WA_VERIFY_TOKEN", ""), e("WA_GRAPH_VERSION", ""), e("WA_TEMPLATE_NAME", ""),
                   e("WA_TEMPLATE_LANG", "en"), e("WA_OBA_VERIFIED", "false").lower() == "true")

    @property
    def configured(self) -> bool:
        return all([self.phone_number_id, self.access_token, self.app_secret, self.verify_token, self.graph_version])


# ---------------------------------------------------------------- webhook security
def verify_challenge(params: dict, verify_token: str) -> tuple[int, str]:
    """GET webhook subscription handshake (hub.mode / hub.verify_token / hub.challenge)."""
    if not verify_token:
        return 403, "verify token not configured"
    if params.get("hub.mode") == "subscribe" and hmac.compare_digest(
            str(params.get("hub.verify_token", "")), verify_token):
        challenge = str(params.get("hub.challenge", ""))
        return (200, challenge) if challenge.isdigit() or challenge.isalnum() else (400, "bad challenge")
    return 403, "forbidden"


def verify_signature(app_secret: str, raw_body: bytes, header: str | None) -> bool:
    """X-Hub-Signature-256: 'sha256=' + HMAC-SHA256(app_secret, raw body). Constant-time compare."""
    if not app_secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[7:])


def parse_inbound(payload: dict) -> tuple[list[dict], list[dict]]:
    """Return (messages, statuses) from a Cloud API webhook payload."""
    msgs, statuses = [], []
    if payload.get("object") != "whatsapp_business_account":
        return msgs, statuses
    for entry in payload.get("entry", []) or []:
        for ch in entry.get("changes", []) or []:
            v = ch.get("value", {}) or {}
            for m in v.get("messages", []) or []:
                text = ""
                if m.get("type") == "text":
                    text = (m.get("text") or {}).get("body", "")
                elif m.get("type") == "button":
                    text = (m.get("button") or {}).get("text", "")
                elif m.get("type") == "interactive":
                    it = m.get("interactive") or {}
                    text = (it.get("button_reply") or it.get("list_reply") or {}).get("title", "")
                msgs.append({"id": m.get("id"), "from": m.get("from"), "timestamp": m.get("timestamp"),
                             "type": m.get("type"), "text": text[:1000]})
            for s in v.get("statuses", []) or []:
                statuses.append({"id": s.get("id"), "status": s.get("status"),
                                 "recipient": mask_phone(s.get("recipient_id", "")),
                                 "errors": s.get("errors")})
    return msgs, statuses


def is_replay(msg_ts: str | None, max_age_s: int, now: datetime | None = None) -> bool:
    try:
        ts = datetime.fromtimestamp(int(msg_ts), tz=(now or now_ist()).tzinfo)
    except (TypeError, ValueError):
        return True
    return abs(((now or now_ist()) - ts).total_seconds()) > max_age_s


# ---------------------------------------------------------------- Cloud API client
class WhatsAppCloudClient:
    def __init__(self, cfg: WhatsAppConfig, transport: PostTransport = urllib_post):
        self.cfg, self.transport = cfg, transport

    def _url(self) -> str:
        return f"https://graph.facebook.com/{self.cfg.graph_version}/{self.cfg.phone_number_id}/messages"

    def _post(self, payload: dict) -> tuple[bool, str]:
        status, body = self.transport(self._url(), {"Authorization": f"Bearer {self.cfg.access_token}",
                                                    "Content-Type": "application/json"},
                                      json.dumps(payload).encode(), 15.0)
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            data = {}
        if status == 200 and data.get("messages"):
            return True, data["messages"][0].get("id", "")
        err = (data.get("error") or {})
        return False, f"HTTP {status} {err.get('code', '')} {str(err.get('message', ''))[:160]}".strip()

    def build_text(self, to: str, body: str) -> dict:
        return {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to,
                "type": "text", "text": {"preview_url": False, "body": body[:4096]}}

    def build_template(self, to: str, params: list[str]) -> dict:
        return {"messaging_product": "whatsapp", "to": to, "type": "template",
                "template": {"name": self.cfg.template_name, "language": {"code": self.cfg.template_lang},
                             "components": [{"type": "body", "parameters":
                                             [{"type": "text", "text": str(p)[:1024]} for p in params]}]}}

    def send_text(self, to: str, body: str) -> tuple[bool, str]:
        return self._post(self.build_text(to, body))

    def send_template(self, to: str, params: list[str]) -> tuple[bool, str]:
        return self._post(self.build_template(to, params))


def in_session_window(last_inbound_iso: str | None, now: datetime | None = None) -> bool:
    if not last_inbound_iso:
        return False
    return (now or now_ist()) - parse_iso(last_inbound_iso) < timedelta(hours=24)


# ---------------------------------------------------------------- destinations
class WhatsAppPersonal(Destination):
    name = "whatsapp"

    def __init__(self, store, client: WhatsAppCloudClient):
        self.store, self.client = store, client

    def available(self):
        return (True, "") if self.client.cfg.configured else (False, "WhatsApp Cloud API credentials not configured")

    def send(self, user_id, message, task):
        ok, why = self.available()
        if not ok:
            return DeliveryResult(UNSUPPORTED, why, ["app", "telegram", "email"])
        link = self.store.wa_for_user(user_id)
        if not link:
            return DeliveryResult(SKIPPED, "no opted-in WhatsApp number linked to this account", ["app"])
        if self.store.consent_state(link["phone"], "whatsapp") == "opt_out":
            return DeliveryResult(SKIPPED, "user opted out", [])
        if in_session_window(link.get("last_inbound_at")):
            sent, info = self.client.send_text(link["phone"], message["text"])
            mode = "session text"
        elif self.client.cfg.template_name:
            sent, info = self.client.send_template(link["phone"], message["template_params"])
            mode = "approved template"
        else:
            return DeliveryResult(FAILED, "outside 24h window and no approved template configured "
                                          "(set WA_TEMPLATE_NAME)", ["app", "telegram", "email"])
        return DeliveryResult(SENT if sent else FAILED,
                              f"{mode} to {mask_phone(link['phone'])}: {info}", [] if sent else ["app"])


class WhatsAppGroup(Destination):
    """Only for groups created by THIS business via the official Groups API (OBA required)."""
    name = "whatsapp_group"

    def __init__(self, cfg: WhatsAppConfig):
        self.cfg = cfg

    def available(self):
        if not self.cfg.configured:
            return False, "WhatsApp Cloud API not configured"
        if not self.cfg.oba_verified:
            return False, ("WhatsApp Groups API requires an Official Business Account (OBA); this account is not "
                           "marked eligible. Use Telegram group, opt-in individual WhatsApp, email or app instead.")
        return True, ""

    def send(self, user_id, message, task):
        ok, why = self.available()
        if not ok:
            return DeliveryResult(UNSUPPORTED, why, ["telegram", "whatsapp", "email", "app"])
        # Endpoint payload intentionally not guessed: implement from Meta's Groups API reference once
        # OBA is approved, then add a contract test with a recorded sandbox response.
        return DeliveryResult(UNSUPPORTED, "OBA flagged, but Groups API send is pending implementation "
                                           "against Meta's current reference + sandbox test", ["telegram", "app"])


class ChannelCopyQueue(Destination):
    """WhatsApp Channel: no official publish API -> copy-ready post queued for a human admin."""
    name = "whatsapp_channel"

    def __init__(self, store, channel_name: str = "RAILVISION Updates"):
        self.store, self.channel_name = store, channel_name

    def send(self, user_id, message, task):
        pid = self.store.queue_channel_post(self.channel_name, message["text"].replace(
            "Reply STOP to unsubscribe · HELP for commands", "Commands: message our WhatsApp number or use the dashboard"),
            task["id"] if task else None)
        return DeliveryResult(QUEUED, f"post #{pid} queued for channel admin (manual publish; no official API)")
