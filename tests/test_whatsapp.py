import hashlib
import hmac
import json
import time
import unittest
from datetime import timedelta

from railvision.core.timeutil import iso, now_ist
from railvision.delivery.base import FAILED, QUEUED, SENT, SKIPPED, UNSUPPORTED
from railvision.delivery.channels import Telegram
from railvision.delivery.whatsapp import (ChannelCopyQueue, WhatsAppCloudClient, WhatsAppConfig, WhatsAppGroup,
                                          WhatsAppPersonal, is_replay, parse_inbound, verify_challenge,
                                          verify_signature)
from railvision.services.messages import DEMO_BANNER, DemoInLiveModeError, compose
from railvision.services.store import Store
from railvision.services.wa_bot import WhatsAppBot

CFG = WhatsAppConfig("PNID", "TOKEN", "appsecret", "verify-me", "vTEST.0", "", "en")
PHONE = "919812345678"


class FakeWA:
    def __init__(self, status=200):
        self.posts, self.status = [], status

    def __call__(self, url, headers, body, timeout):
        self.posts.append((url, headers, json.loads(body)))
        if self.status == 200:
            return 200, json.dumps({"messages": [{"id": "wamid.X"}]}).encode()
        return self.status, json.dumps({"error": {"code": 131047, "message": "Re-engagement message"}}).encode()


def inbound(text, mid="wamid.1", ts=None, phone=PHONE):
    return {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {"messages": [
        {"id": mid, "from": phone, "timestamp": str(int(ts or time.time())), "type": "text", "text": {"body": text}}]}}]}]}


class WebhookSecurityTests(unittest.TestCase):
    def test_challenge(self):
        self.assertEqual(verify_challenge({"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "12345"},
                                          "verify-me"), (200, "12345"))
        self.assertEqual(verify_challenge({"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "1"},
                                          "verify-me")[0], 403)
        self.assertEqual(verify_challenge({"hub.mode": "subscribe", "hub.verify_token": "", "hub.challenge": "1"}, "")[0], 403)

    def test_signature(self):
        body = json.dumps(inbound("hi")).encode()
        sig = "sha256=" + hmac.new(b"appsecret", body, hashlib.sha256).hexdigest()
        self.assertTrue(verify_signature("appsecret", body, sig))
        self.assertFalse(verify_signature("appsecret", body + b" ", sig))      # tampered body
        self.assertFalse(verify_signature("appsecret", body, "sha256=00"))
        self.assertFalse(verify_signature("appsecret", body, None))
        self.assertFalse(verify_signature("", body, sig))

    def test_parse_and_replay(self):
        msgs, st = parse_inbound(inbound("where is 12951"))
        self.assertEqual(msgs[0]["text"], "where is 12951")
        self.assertEqual(parse_inbound({"object": "page"}), ([], []))
        self.assertTrue(is_replay(str(int(time.time()) - 3600), 600))
        self.assertFalse(is_replay(str(int(time.time())), 600))
        self.assertTrue(is_replay("garbage", 600))


class DestinationTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.uid, _ = self.store.create_user("u")
        self.msg = {"title": "t", "text": "hello", "short": "h", "is_demo": False, "template_params": ["a", "b"]}

    def test_unconfigured_is_unsupported_with_fallback(self):
        d = WhatsAppPersonal(self.store, WhatsAppCloudClient(WhatsAppConfig()))
        r = d.send(self.uid, self.msg, None)
        self.assertEqual(r.status, UNSUPPORTED)
        self.assertIn("app", r.fallbacks)

    def test_session_window_text_vs_template(self):
        fake = FakeWA()
        d = WhatsAppPersonal(self.store, WhatsAppCloudClient(CFG, transport=fake))
        self.assertEqual(d.send(self.uid, self.msg, None).status, SKIPPED)        # nobody linked
        self.store.wa_upsert(PHONE, opted_in=True, inbound=True, user_id=self.uid)
        r = d.send(self.uid, self.msg, None)
        self.assertEqual(r.status, SENT)
        self.assertEqual(fake.posts[-1][2]["type"], "text")
        self.assertEqual(fake.posts[-1][0], "https://graph.facebook.com/vTEST.0/PNID/messages")
        self.assertEqual(fake.posts[-1][1]["Authorization"], "Bearer TOKEN")
        # outside 24h window
        self.store.x("UPDATE wa_links SET last_inbound_at=?", (iso(now_ist() - timedelta(hours=30)),))
        r = d.send(self.uid, self.msg, None)
        self.assertEqual(r.status, FAILED)
        self.assertIn("template", r.detail)
        cfg2 = WhatsAppConfig(**{**CFG.__dict__, "template_name": "train_update"})
        d2 = WhatsAppPersonal(self.store, WhatsAppCloudClient(cfg2, transport=fake))
        self.assertEqual(d2.send(self.uid, self.msg, None).status, SENT)
        self.assertEqual(fake.posts[-1][2]["template"]["name"], "train_update")
        self.assertNotIn(PHONE, r.detail)          # phone masked in logs

    def test_opt_out_respected(self):
        d = WhatsAppPersonal(self.store, WhatsAppCloudClient(CFG, transport=FakeWA()))
        self.store.wa_upsert(PHONE, opted_in=True, inbound=True, user_id=self.uid)
        self.store.record_consent(PHONE, "whatsapp", "opt_out")
        self.store.wa_upsert(PHONE, opted_in=False)
        self.assertEqual(d.send(self.uid, self.msg, None).status, SKIPPED)

    def test_delivery_failure_reported(self):
        d = WhatsAppPersonal(self.store, WhatsAppCloudClient(CFG, transport=FakeWA(status=400)))
        self.store.wa_upsert(PHONE, opted_in=True, inbound=True, user_id=self.uid)
        r = d.send(self.uid, self.msg, None)
        self.assertEqual(r.status, FAILED)
        self.assertIn("131047", r.detail)

    def test_group_requires_oba_and_offers_fallback(self):
        r = WhatsAppGroup(CFG).send(self.uid, self.msg, None)
        self.assertEqual(r.status, UNSUPPORTED)
        self.assertIn("Official Business Account", r.detail)
        self.assertIn("telegram", r.fallbacks)

    def test_channel_queue_for_admin(self):
        r = ChannelCopyQueue(self.store).send(self.uid, {**self.msg, "text": "post\nReply STOP to unsubscribe · HELP for commands"},
                                              {"id": "t_1"})
        self.assertEqual(r.status, QUEUED)
        q = self.store.channel_queue()
        self.assertEqual(len(q), 1)
        self.assertNotIn("Reply STOP", q[0]["text"])

    def test_telegram(self):
        calls = []
        tg = Telegram(token="bot:tok", transport=lambda u, h, b, t: (calls.append((u, json.loads(b))) or (200, b'{"ok":true}')),
                      chat_lookup=lambda u, t: "-1001", webhook_secret="s3")
        self.assertEqual(tg.send(self.uid, self.msg, None).status, SENT)
        self.assertIn("/botbot:tok/sendMessage", calls[0][0])
        self.assertTrue(tg.verify_webhook("s3"))
        self.assertFalse(tg.verify_webhook("nope"))
        self.assertEqual(Telegram(token="").send(self.uid, self.msg, None).status, UNSUPPORTED)


class ComposeTests(unittest.TestCase):
    STATUS = {"train_number": "12951", "train_name": "X", "service_date": "2026-10-14", "delay_min": 7, "next_stop": "BBB",
              "stops": [{"code": "RE", "name": "Rewari"}], "is_demo": False,
              "freshness": {"source": "src", "source_time": "2026-10-14T22:33:00+05:30"}}
    CROSS = {"event_class": "CONFIRMED_PASSAGE", "verification": "VERIFIED_LIVE", "event_time": "2026-10-14T22:31:00+05:30",
             "headline": "passed", "is_demo": False}

    def test_template_fields(self):
        m = compose("CONFIRMED_PASSAGE", {"reference_station": "RE"}, self.STATUS, self.CROSS, "https://app", "live")
        for needle in ("Train: 12951 X", "Service date: 2026-10-14", "Reference station: Rewari (RE)",
                       "Event: CONFIRMED PASSAGE", "Event time: 22:31 IST", "Delay: +7 min", "Next scheduled stop: BBB",
                       "Last source update: 14 Oct 2026, 22:33 IST", "Source: src", "Open dashboard: https://app/#train/12951"):
            self.assertIn(needle, m["text"])
        self.assertNotIn(DEMO_BANNER, m["text"])

    def test_unknown_delay_never_guessed(self):
        s = {**self.STATUS, "delay_min": None}
        self.assertIn("Delay: Unknown", compose("STATUS_REPORT", {"reference_station": "RE"}, s, None, "", "live")["text"])

    def test_demo_banner_and_live_refusal(self):
        s = {**self.STATUS, "is_demo": True}
        self.assertIn(DEMO_BANNER, compose("STATUS_REPORT", {"reference_station": "RE"}, s, None, "", "demo")["text"])
        with self.assertRaises(DemoInLiveModeError):
            compose("STATUS_REPORT", {"reference_station": "RE"}, s, None, "", "live")


class BotTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.fake = FakeWA()
        self.client = WhatsAppCloudClient(CFG, transport=self.fake)

        class Chat:
            def handle(s, sid, uid, text, channel):
                return {"reply": f"echo:{text}", "intent": "x"}
        self.bot = WhatsAppBot(self.store, Chat(), self.client, logger=lambda *_: None)

    def handle(self, text, mid):
        return self.bot.handle_message(parse_inbound(inbound(text, mid))[0][0])

    def test_flow_opt_in_stop_start_delete(self):
        self.assertEqual(self.handle("hi", "m1")["action"], "replied")
        self.assertEqual(self.store.consent_state(PHONE, "whatsapp"), "opt_in")
        self.assertEqual(self.handle("hi", "m1")["reason"], "duplicate delivery")
        self.assertEqual(self.handle("STOP", "m2")["action"], "opt_out")
        self.assertEqual(self.store.consent_state(PHONE, "whatsapp"), "opt_out")
        self.assertFalse(self.store.wa_get(PHONE)["opted_in"])
        self.assertEqual(self.handle("START", "m3")["action"], "opt_in")
        self.assertTrue(self.store.wa_get(PHONE)["opted_in"])
        self.assertEqual(self.handle("delete my data", "m4")["action"], "deleted")
        self.assertIsNone(self.store.wa_get(PHONE))

    def test_link_account(self):
        uid, _ = self.store.create_user("web user")
        code = self.store.new_link_code(uid)
        self.assertEqual(self.handle(f"LINK {code}", "l1")["user_id"], uid)
        self.assertEqual(self.store.wa_for_user(uid)["phone"], PHONE)
        self.assertEqual(self.handle(f"LINK {code}", "l2")["action"], "link_failed")    # single use

    def test_replay_rejected(self):
        m = parse_inbound(inbound("hi", "old", ts=time.time() - 7200))[0][0]
        self.assertEqual(self.bot.handle_message(m)["reason"], "stale/replayed timestamp")


if __name__ == "__main__":
    unittest.main()
