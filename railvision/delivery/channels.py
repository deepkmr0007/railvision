"""Other destinations: in-app (+ SSE live stream / browser notifications), Telegram, email, Android push (FCM)."""
from __future__ import annotations

import hmac
import json
import os
import queue
import smtplib
import ssl
import threading
from email.message import EmailMessage

from ..services.resilience import KeyedRateLimiter
from .base import FAILED, SENT, SKIPPED, UNSUPPORTED, DeliveryResult, Destination, PostTransport, urllib_post


class EventBus:
    """Per-user fan-out for Server-Sent Events. Browser notifications are raised client-side from this stream."""

    def __init__(self):
        self._subs: dict[str, list[queue.Queue]] = {}
        self._lock = threading.Lock()

    def subscribe(self, user_id: str) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=200)
        with self._lock:
            self._subs.setdefault(user_id, []).append(q)
        return q

    def unsubscribe(self, user_id: str, q: queue.Queue):
        with self._lock:
            if q in self._subs.get(user_id, []):
                self._subs[user_id].remove(q)

    def publish(self, user_id: str, event: dict) -> int:
        n = 0
        with self._lock:
            subs = list(self._subs.get(user_id, []))
        for q in subs:
            try:
                q.put_nowait(event)
                n += 1
            except queue.Full:
                pass
        return n


class InApp(Destination):
    """Notification row already persisted by the tracker; this pushes it live to open dashboards."""
    name = "app"

    def __init__(self, bus: EventBus):
        self.bus = bus

    def send(self, user_id, message, task):
        n = self.bus.publish(user_id, {"type": "notification", "title": message["title"], "body": message["text"],
                                       "short": message["short"], "is_demo": message["is_demo"],
                                       "task_id": task["id"] if task else None})
        return DeliveryResult(SENT, f"stored; pushed to {n} open session(s)")


class BrowserNotify(InApp):
    name = "browser"   # same stream; the page calls the Notification API (permission asked in-page)


class Telegram(Destination):
    """Official Telegram Bot API. Works for private chats, groups and channels the bot was added to."""
    name = "telegram"

    def __init__(self, token: str | None = None, transport: PostTransport = urllib_post, chat_lookup=None,
                 webhook_secret: str | None = None):
        self.token = token if token is not None else os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.webhook_secret = webhook_secret if webhook_secret is not None else os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
        self.transport = transport
        self.chat_lookup = chat_lookup or (lambda user_id, task: (task or {}).get("telegram_chat_id")
                                           or os.environ.get("TELEGRAM_DEFAULT_CHAT_ID"))
        self.per_chat = KeyedRateLimiter(rate=18, per=60)   # below Telegram's ~20/min per group

    def available(self):
        return (True, "") if self.token else (False, "TELEGRAM_BOT_TOKEN not configured")

    def verify_webhook(self, header_value: str | None) -> bool:
        return bool(self.webhook_secret) and hmac.compare_digest(header_value or "", self.webhook_secret)

    def build(self, chat_id: str, text: str) -> dict:
        return {"chat_id": chat_id, "text": text[:4096], "disable_web_page_preview": True}

    def send(self, user_id, message, task):
        ok, why = self.available()
        if not ok:
            return DeliveryResult(UNSUPPORTED, why, ["app"])
        chat = self.chat_lookup(user_id, task)
        if not chat:
            return DeliveryResult(SKIPPED, "no Telegram chat linked", ["app"])
        if not self.per_chat.allow(str(chat)):
            return DeliveryResult(FAILED, "per-chat rate limit; will retry next cycle", ["app"])
        status, body = self.transport(f"https://api.telegram.org/bot{self.token}/sendMessage",
                                      {"Content-Type": "application/json"},
                                      json.dumps(self.build(str(chat), message["text"])).encode(), 10.0)
        try:
            ok_ = json.loads(body or b"{}").get("ok")
        except ValueError:
            ok_ = False
        return DeliveryResult(SENT if status == 200 and ok_ else FAILED, f"HTTP {status}")


class Email(Destination):
    name = "email"

    def __init__(self, addr_lookup=None, smtp_factory=None):
        e = os.environ.get
        self.host, self.port = e("SMTP_HOST", ""), int(e("SMTP_PORT", "587"))
        self.user, self.password, self.sender = e("SMTP_USER", ""), e("SMTP_PASSWORD", ""), e("SMTP_FROM", "")
        self.addr_lookup = addr_lookup or (lambda user_id, task: (task or {}).get("email"))
        self.smtp_factory = smtp_factory or (lambda: smtplib.SMTP(self.host, self.port, timeout=15))

    def available(self):
        return (True, "") if self.host and self.sender else (False, "SMTP not configured")

    def send(self, user_id, message, task):
        ok, why = self.available()
        if not ok:
            return DeliveryResult(UNSUPPORTED, why, ["app"])
        to = self.addr_lookup(user_id, task)
        if not to:
            return DeliveryResult(SKIPPED, "no email address on file", ["app"])
        m = EmailMessage()
        m["Subject"], m["From"], m["To"] = message["title"], self.sender, to
        m.set_content(message["text"])
        try:
            with self.smtp_factory() as s:
                s.starttls(context=ssl.create_default_context())
                if self.user:
                    s.login(self.user, self.password)
                s.send_message(m)
            return DeliveryResult(SENT, "smtp accepted")
        except (smtplib.SMTPException, OSError) as e:
            return DeliveryResult(FAILED, type(e).__name__, ["app"])


class AndroidPush(Destination):
    """Firebase Cloud Messaging HTTP v1. Requires FCM_PROJECT_ID + a short-lived OAuth access token
    (FCM_ACCESS_TOKEN, minted from a service account by your deploy tooling) + device tokens."""
    name = "android_push"

    def __init__(self, transport: PostTransport = urllib_post, token_lookup=None):
        self.project = os.environ.get("FCM_PROJECT_ID", "")
        self.access = os.environ.get("FCM_ACCESS_TOKEN", "")
        self.transport = transport
        self.token_lookup = token_lookup or (lambda user_id, task: (task or {}).get("fcm_tokens", []))

    def available(self):
        return (True, "") if self.project and self.access else (False, "FCM not configured")

    def send(self, user_id, message, task):
        ok, why = self.available()
        if not ok:
            return DeliveryResult(UNSUPPORTED, why, ["app", "browser"])
        toks = self.token_lookup(user_id, task)
        if not toks:
            return DeliveryResult(SKIPPED, "no registered Android device", ["app"])
        sent = 0
        for t in toks:
            body = {"message": {"token": t, "notification": {"title": message["title"][:100], "body": message["short"][:240]},
                                "android": {"priority": "high"}}}
            st, _ = self.transport(f"https://fcm.googleapis.com/v1/projects/{self.project}/messages:send",
                                   {"Authorization": f"Bearer {self.access}", "Content-Type": "application/json"},
                                   json.dumps(body).encode(), 10.0)
            sent += st == 200
        return DeliveryResult(SENT if sent else FAILED, f"{sent}/{len(toks)} devices")


class Dispatcher:
    """Routes a notification to each requested destination; records every attempt; never raises."""

    def __init__(self, store, destinations: list[Destination]):
        self.store = store
        self.dest = {d.name: d for d in destinations}

    def catalogue(self) -> list[dict]:
        out = []
        for name, d in self.dest.items():
            ok, why = d.available()
            out.append({"name": name, "available": ok, "reason": why})
        return out

    def deliver(self, notification_id: int, user_id: str, message: dict, task: dict | None,
                destinations: list[str]) -> list[dict]:
        results = []
        wanted = list(dict.fromkeys(["app"] + list(destinations)))  # app inbox always receives a copy
        for name in wanted:
            d = self.dest.get(name)
            if d is None:
                r = DeliveryResult(UNSUPPORTED, f"unknown destination {name}")
            else:
                try:
                    r = d.send(user_id, message, task)
                except Exception as e:  # adapter bug must not stop other channels
                    r = DeliveryResult(FAILED, f"adapter error {type(e).__name__}")
            self.store.add_delivery(notification_id, name, r.status, r.detail)
            results.append({"destination": name, "status": r.status, "detail": r.detail, "fallbacks": r.fallbacks})
        return results
