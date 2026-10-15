"""WhatsApp (and Telegram) conversational front-end on top of ChatEngine.

Inbound flow: signature verified by the HTTP layer -> parse -> replay/duplicate guard ->
consent commands (STOP / START / DELETE MY DATA) -> account LINK -> ChatEngine -> reply in the
open 24 h session window (user just messaged, so free-form text is allowed).
"""
from __future__ import annotations

import re

from ..delivery.base import mask_phone
from ..delivery.whatsapp import is_replay
from .messages import HELP_TEXT

STOP_RE = re.compile(r"^\s*(stop|unsubscribe|band|बंद)\s*$", re.I)
START_RE = re.compile(r"^\s*(start|subscribe|shuru|शुरू)\s*$", re.I)
DELETE_RE = re.compile(r"^\s*delete my data\s*$", re.I)
LINK_RE = re.compile(r"^\s*link\s+(\d{6})\s*$", re.I)


class WhatsAppBot:
    def __init__(self, store, chat, client, max_age_s: int = 600, logger=print):
        self.store, self.chat, self.client = store, chat, client
        self.max_age_s, self.log = max_age_s, logger

    def handle_message(self, m: dict) -> dict:
        phone, text = m.get("from") or "", (m.get("text") or "").strip()
        if not phone or not m.get("id"):
            return {"action": "ignored", "reason": "malformed"}
        if self.store.seen_message(m["id"]):
            return {"action": "ignored", "reason": "duplicate delivery"}
        if is_replay(m.get("timestamp"), self.max_age_s):
            return {"action": "ignored", "reason": "stale/replayed timestamp"}

        if STOP_RE.match(text):
            self.store.record_consent(phone, "whatsapp", "opt_out", "STOP keyword")
            self.store.wa_upsert(phone, opted_in=False, inbound=True)
            self.client.send_text(phone, "You're unsubscribed from RAILVISION updates. Reply START to subscribe again.")
            return {"action": "opt_out"}
        if DELETE_RE.match(text):
            self.store.record_consent(phone, "whatsapp", "opt_out", "DELETE MY DATA")
            self.client.send_text(phone, "Your number and chat link have been deleted from RAILVISION.")
            self.store.wa_forget(phone)
            return {"action": "deleted"}

        link = self.store.wa_get(phone)
        if START_RE.match(text) or link is None:
            self.store.record_consent(phone, "whatsapp", "opt_in", "user-initiated message" if link is None else "START keyword")
            link = self.store.wa_upsert(phone, opted_in=True, inbound=True)
        else:
            if not link["opted_in"]:
                # user writes again after STOP: answer this conversation, but no proactive updates until START
                self.store.wa_upsert(phone, inbound=True)
            else:
                self.store.wa_upsert(phone, inbound=True)
        if START_RE.match(text):
            self.client.send_text(phone, "✅ Subscribed. " + HELP_TEXT)
            return {"action": "opt_in"}

        lm = LINK_RE.match(text)
        if lm:
            uid = self.store.consume_link_code(lm.group(1))
            if not uid:
                self.client.send_text(phone, "That link code is invalid or expired. Generate a new one in the dashboard (Settings → Link WhatsApp).")
                return {"action": "link_failed"}
            self.store.wa_upsert(phone, user_id=uid)
            self.store.audit(uid, "wa.link", mask_phone(phone))
            self.client.send_text(phone, "🔗 WhatsApp linked to your RAILVISION account. Tracking tasks you create here will appear on your dashboard.")
            return {"action": "linked", "user_id": uid}

        user_id = link.get("user_id")
        if not user_id:  # chat-only account keyed to this phone (can be linked later)
            user_id, _token = self.store.create_user("whatsapp " + mask_phone(phone))
            self.store.wa_upsert(phone, user_id=user_id)
        res = self.chat.handle(f"wa:{self.store.hash_subject(phone)[:24]}", user_id, text, channel="whatsapp")
        ok, info = self.client.send_text(phone, res["reply"])
        self.log(f"[wa] reply to {mask_phone(phone)} intent={res['intent']} sent={ok}")
        return {"action": "replied", "intent": res["intent"], "sent": ok}
