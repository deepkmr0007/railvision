import unittest
from datetime import date

from helpers import SD, T

from railvision.chatbot.engine import ChatEngine
from railvision.chatbot.llm import validate
from railvision.chatbot.nlu import parse
from railvision.delivery.channels import Dispatcher, EventBus, InApp
from railvision.providers.demo import DemoProvider
from railvision.providers.registry import ProviderRegistry
from railvision.services.store import Store
from railvision.services.tracker import TrackingEngine
from railvision import stations


class NLUTests(unittest.TestCase):
    CASES = [
        ("Where is train 12951 now?", "where", "12951"),
        ("Has it crossed Rewari?", "crossed", None),
        ("Does this train stop at Rewari?", "stops_at", None),
        ("Estimate when it will cross this station.", "estimate", None),
        ("Track this train every 15 minutes until Delhi.", "track", None),
        ("Alert me if the delay exceeds 30 minutes.", "delay_alert", None),
        ("Show chart vacancy from my boarding station.", "vacancy", None),
        ("Pause this tracking.", "pause", None),
        ("Change the interval to 30 minutes.", "change_interval", None),
        ("Stop updates for this train.", "cancel", None),
        ("Track train 12951 from Rewari to Delhi. Check every 15 minutes. Report when it passes Rewari even if it does not "
         "stop there, and continue until Delhi.", "track", "12951"),
        ("12951 kahan hai", "where", "12951"),
        ("12951 कहाँ है?", "where", "12951"),
        ("Rewari cross hui kya?", "crossed", None),
        ("12951 ko Rewari se Delhi tak har 15 min track karo", "track", "12951"),
        ("रेवाड़ी पार हुई?", "crossed", None),
        ("resume tracking", "resume", None),
        ("trains between Jaipur Junction and Alwar", "between", None),
        ("/help", "help", None),
    ]

    def test_examples(self):
        for text, intent, train in self.CASES:
            with self.subTest(text=text):
                p = parse(text, SD)
                self.assertEqual(p.intent, intent)
                self.assertEqual(p.train, train)

    def test_slots(self):
        p = parse("Track train 12951 from Rewari to Delhi every 15 minutes", SD)
        self.assertEqual(p.interval_min, 15)
        self.assertEqual([c[0].code for c in p.stations[:1]], ["RE"])
        self.assertGreater(len(p.stations[1]), 1)            # "Delhi" is ambiguous
        self.assertEqual(parse("har 2 ghante track karo 12951", SD).interval_min, 120)
        self.assertEqual(parse("alert me if delay exceeds 45 min for 12951", SD).threshold_min, 45)
        self.assertEqual(parse("vacancy 12951 3A from NDLS", SD).travel_class, "3A")
        self.assertTrue(parse("12951 kal kahan thi", SD).date_ambiguous)
        self.assertEqual(parse("where is 12951 on 13/10", SD).service_date, date(2026, 10, 13))
        self.assertEqual(parse("१२९५१ कहाँ है", SD).train, "12951")
        self.assertEqual(parse("12951 कहाँ है", SD).lang, "hi")

    def test_station_resolution(self):
        self.assertEqual([s.code for s in stations.resolve("RE")], ["RE"])
        self.assertEqual([s.code for s in stations.resolve("gurgaon")], ["GGN"])
        self.assertGreater(len(stations.resolve("Delhi")), 3)
        self.assertEqual(stations.resolve("रेवाड़ी")[0].code, "RE")
        found = stations.find_in_text("from New Delhi to Rewari")
        self.assertEqual([c[0].code for c in found], ["NDLS", "RE"])

    def test_llm_validation(self):
        self.assertIsNone(validate({"intent": "launch_missiles"}))
        self.assertIsNone(validate({"intent": "where", "train_number": "1"})["train_number"])
        self.assertEqual(validate({"intent": "track", "interval_min": 2})["interval_min"], None)


class ChatEngineTests(unittest.TestCase):
    def setUp(self):
        now = T(9, 40)
        self.prov = DemoProvider(clock=lambda: now)
        self.store = Store()
        reg = ProviderRegistry("demo", [self.prov])
        eng = TrackingEngine(self.store, reg, Dispatcher(self.store, [InApp(EventBus())]), "demo", clock=lambda: now)
        self.chat = ChatEngine(eng, reg, self.store)
        self.uid, _ = self.store.create_user("u")

    def say(self, text, sid="s1"):
        return self.chat.handle(sid, self.uid, text)

    def test_where_and_crossing_grounded_and_labelled(self):
        r = self.say("where is 90001")
        self.assertTrue(r["is_demo"])
        self.assertIn("DEMO", r["reply"])
        self.assertIn("Source: demo-synthetic", r["reply"])
        r = self.say("has it crossed Rewari?")              # context remembers the train
        self.assertIn(r["data"]["crossing"]["event_class"], ("ESTIMATED_CROSSING", "CONFIRMED_PASSAGE", "NOT_VERIFIED"))
        self.assertIn("confidence", r["reply"])

    def test_stop_question_nonstop(self):
        r = self.say("does 90001 stop at Rewari?")
        self.assertIn("No", r["reply"])
        self.assertIn("without a scheduled halt", r["reply"])
        self.assertIn("Yes", self.say("does 90001 stop at Alwar?")["reply"])

    def test_ambiguous_delhi_resolved_by_route(self):
        r = self.say("track 90001 from Rewari to Delhi every 15 min")
        self.assertIn("Please confirm", r["reply"])          # Delhi -> DLI because only DLI is on the route
        self.assertEqual(r["data"]["task"]["end_station"], "DLI")
        r = self.say("yes")
        self.assertIn("ACTIVE", r["reply"])
        self.assertEqual(self.store.list_tasks(self.uid)[0]["status"], "ACTIVE")
        self.assertIn("Paused", self.say("pause this tracking")["reply"])
        self.assertIn("Resumed", self.say("resume tracking")["reply"])
        self.assertIn("every 30 min", self.say("change the interval to 30 minutes")["reply"])
        self.assertIn("45", self.say("alert me if the delay exceeds 45 minutes")["reply"])
        self.assertIn("Stopped", self.say("stop updates for this train")["reply"])

    def test_choice_follow_up_flow(self):
        self.store.set_context("s9", self.uid, {"last_train": "90001", "awaiting": {
            "role": "s0", "options": ["DLI (Delhi Junction)", "DEC (Delhi Cantt)"], "resume_text": "has 90001 crossed Delhi"}})
        r = self.chat.handle("s9", self.uid, "2")
        self.assertEqual(r["data"]["crossing"]["station_code"], "DEC")

    def test_ambiguous_station_follow_up_question(self):
        r = self.say("trains between Delhi and Rewari")
        self.assertIn("two stations", r["reply"])

    def test_missing_train_asks(self):
        r = self.chat.handle("fresh", self.uid, "where is it?")
        self.assertIn("Which train", r["reply"])

    def test_vacancy_with_disclaimer(self):
        self.assertIn("class", self.say("show chart vacancy for 90001")["reply"].lower())
        r = self.say("chart vacancy 90001 3A from Jaipur Junction")
        self.assertIn("≠", r["reply"])
        self.assertIn("irctc.co.in", r["reply"])

    def test_unknown_train_not_faked(self):
        r = self.say("where is 12951")
        self.assertIn("isn't available", r["reply"])

    def test_hindi_reply(self):
        r = self.say("90001 Rewari pe rukti hai?")
        self.assertIn("नहीं", r["reply"])


if __name__ == "__main__":
    unittest.main()
