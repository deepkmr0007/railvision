"""Conversational assistant: intent -> backend tools -> grounded reply.

Every live fact in a reply comes from a provider call made during this turn, with source and
timestamp. Missing slots trigger one short follow-up question; persistent tasks are only
activated after the user confirms a summary.
"""
from __future__ import annotations

from datetime import date, timedelta

from ..core.crossing import evaluate_crossing
from ..core.status import build_status
from ..core.timeutil import fmt_hm, now_ist, parse_iso
from ..providers.base import NotAuthorized, NotSupported, ProviderError
from ..services.messages import DEMO_BANNER, HELP_TEXT
from ..services.tracker import TaskError
from . import nlu


def _L(lang: str, en: str, hi: str) -> str:
    return hi if lang in ("hi", "hinglish") else en


class ChatEngine:
    def __init__(self, tracker, registry, store, llm=None, assist=None):
        self.tracker, self.registry, self.store, self.llm = tracker, registry, store, llm
        self.assist = assist          # AssistService: lets the bot ask the browser assistant to open official pages

    # ------------------------------------------------------------------ helpers
    def _today(self) -> date:
        return self.tracker.clock().date()

    def _default_date(self, train: str) -> date:
        p = self.registry.primary()
        if hasattr(p, "default_service_date"):
            try:
                return p.default_service_date(train)
            except Exception:
                pass
        return self._today()

    def _resolve_station(self, cands: list, route) -> tuple[str | None, list]:
        """Return (code, options). Route membership disambiguates e.g. 'Delhi' -> DLI for this train."""
        if not cands:
            return None, []
        if route:
            on = [s for s in cands if route.index_of(s.code) is not None]
            if len(on) == 1:
                return on[0].code, []
            if on:
                cands = on
                halts = [s for s in on if route.stop(s.code) and route.stop(s.code).halt]
                if len(halts) == 1:          # "Delhi" for a train that halts only at DLI
                    return halts[0].code, []
        if len(cands) == 1:
            return cands[0].code, []
        return None, [f"{s.code} ({s.name})" for s in cands]

    def _reply(self, text, intent, data=None, ctx=None, session=None, user_id=None, is_demo=False, suggestions=None):
        if ctx is not None and session:
            self.store.set_context(session, user_id, ctx)
        if is_demo and not text.startswith(DEMO_BANNER):
            text = DEMO_BANNER + "\n" + text
        return {"reply": text, "intent": intent, "data": data or {}, "is_demo": is_demo,
                "suggestions": suggestions or []}

    def _src_line(self, status: dict) -> str:
        f = status["freshness"]
        if not f.get("source_time"):
            return f"Source: {f['source']} · no update yet"
        age = f.get("age_min")
        return (f"Source: {f['source']} · updated {fmt_hm(parse_iso(f['source_time']))} IST"
                + (f" ({age:.0f} min ago)" if age is not None else "") + (" · ⚠️ STALE" if f.get("stale") else ""))

    # ------------------------------------------------------------------ main entry
    def handle(self, session_id: str, user_id: str | None, text: str, channel: str = "web") -> dict:
        ctx = self.store.get_context(session_id)
        p = nlu.parse(text, self._today())
        lang = p.lang
        rep = lambda t, i, **k: self._reply(t, i, ctx=ctx, session=session_id, user_id=user_id, **k)  # noqa: E731

        # answer to a pending choice question ("which Delhi?")
        aw = ctx.get("awaiting")
        if aw and p.intent in ("unknown", "where") and not p.train:
            pick = text.strip().upper()
            codes = [o.split()[0] for o in aw.get("options", [])]
            if pick.isdigit() and 1 <= int(pick) <= len(codes):
                pick = codes[int(pick) - 1]
            if pick in codes:
                ctx.pop("awaiting")
                ctx.setdefault("picks", {})[aw["role"]] = pick
                self.store.set_context(session_id, user_id, ctx)
                return self.handle(session_id, user_id, aw["resume_text"], channel)
        if aw and p.intent not in ("confirm", "decline"):
            ctx.pop("awaiting", None)

        # confirmation of a drafted task
        if p.intent in ("confirm", "decline") and ctx.get("pending_task"):
            tid = ctx.pop("pending_task")
            if not user_id:
                return rep("Please sign in to create tracking tasks.", "confirm")
            if p.intent == "decline":
                self.tracker.set_state(tid, user_id, "cancel")
                return rep(_L(lang, "OK, tracking not started.", "ठीक है, tracking शुरू नहीं की गई।"), "decline")
            t = self.tracker.confirm(tid, user_id)
            ctx["last_task_id"] = tid
            return rep(_L(lang, f"✅ Tracking ACTIVE for {t['train_number']} at {t['reference_station']} every "
                                f"{t['interval_min']} min. Say 'pause', 'change interval to 30', or 'stop updates' anytime.",
                          f"✅ {t['train_number']} की tracking चालू — {t['reference_station']}, हर {t['interval_min']} मिनट। "
                          "'pause', 'interval 30 karo' या 'stop updates' कभी भी बोलें।"),
                       "confirm", data={"task": t}, is_demo=t["is_demo"])

        if p.intent == "help":
            return rep(HELP_TEXT, "help")

        if p.intent == "unknown" and self.llm:
            structured = self.llm.parse(text)
            if structured:
                p.intent = structured.get("intent", "unknown")
                p.train = p.train or structured.get("train_number")
        if p.intent == "unknown":
            return rep(_L(lang, "I didn't catch that. Try: 'where is 12951', 'has 12951 crossed Rewari', "
                                "'track 12951 from Rewari to Delhi every 15 min'. Type HELP for all commands.",
                          "समझ नहीं आया। जैसे लिखें: '12951 kahan hai', 'Rewari cross hui?', "
                          "'12951 Rewari se Delhi tak har 15 min track karo'. HELP लिखें।"), "unknown")

        if p.intent == "search":
            return self._search(p, user_id, rep, lang)
        if p.intent == "helpline":
            from ..services.extras import HELPLINES
            return rep("\n".join(["📞 Railway helplines (free):"] + [f"• {h['number']} — {h['name'] if lang == 'en' else h['hi']}: {h['use']}"
                                                                    for h in HELPLINES]) +
                       "\nComplaints online: https://railmadad.indianrailways.gov.in/", "helpline")
        if p.intent in ("trips", "news", "pnr"):
            return self._personal(p, user_id, rep, lang)

        # task-control intents can work without a train number
        if p.intent in ("pause", "resume", "cancel", "change_interval", "delay_alert"):
            return self._task_control(p, ctx, user_id, rep, lang)

        train = p.train or ctx.get("last_train")
        if not train and p.intent not in ("between", "board"):
            return rep(_L(lang, "Which train number? (5 digits, e.g. 12951)", "कौन सी ट्रेन? (5 अंकों का नंबर, जैसे 12951)"),
                       p.intent)
        if p.date_ambiguous:
            return rep(_L(lang, "Did you mean yesterday's or tomorrow's departure? Reply 'yesterday' or 'tomorrow'.",
                          "'कल' से मतलब — बीता हुआ कल (yesterday) या आने वाला कल (tomorrow)?"), p.intent)
        sd = p.service_date or (date.fromisoformat(ctx["service_date"]) if ctx.get("service_date") and ctx.get("last_train") == train
                                else (self._default_date(train) if train else self._today()))
        if train:
            ctx["last_train"], ctx["service_date"] = train, sd.isoformat()

        try:
            if p.intent in ("between", "board"):
                return self._discovery(p, ctx, rep, lang, sd)
            route = self.tracker.route(train, sd)
            stations_resolved = []
            picks = ctx.get("picks") or {}
            for i, cands in enumerate(p.stations):
                role = "s%d" % i
                if picks.get(role) in [c.code for c in cands]:      # user's explicit choice wins
                    code, opts = picks[role], []
                else:
                    code, opts = self._resolve_station(cands, route)
                if opts:
                    ctx["awaiting"] = {"role": role, "options": opts, "resume_text": p.raw}
                    return rep(_L(lang, "Which station did you mean?\n", "कौन सा स्टेशन?\n") +
                               "\n".join(f"{n + 1}. {o}" for n, o in enumerate(opts)) + "\nReply with the number or code.",
                               p.intent, data={"options": opts})
                if code:
                    stations_resolved.append(code)
            ctx.pop("picks", None)
            if stations_resolved:
                ctx["last_station"] = stations_resolved[0]
            return self._train_intent(p, ctx, user_id, rep, lang, train, sd, route, stations_resolved, channel)
        except NotSupported as e:
            link = f"\nOfficial page: {e.official_url}" if getattr(e, "official_url", None) else ""
            return rep(_L(lang, f"That data isn't available from an authorized source right now: {e}{link}",
                          f"यह जानकारी अभी किसी अधिकृत स्रोत से उपलब्ध नहीं: {e}{link}"), p.intent,
                       data={"official_url": getattr(e, "official_url", None)})
        except ProviderError as e:
            if self.assist and user_id and getattr(e, "train", None):
                job = self.assist.queue_job(user_id, "status", {"train": e.train, "date": e.sd.isoformat()})
                return rep(_L(lang, f"🔎 I've asked your browser assistant to open the official NTES page for {e.train} "
                                    f"(start date {e.sd:%d %b}). The result arrives as an alert — and on WhatsApp if linked — "
                                    "within a minute or two while your computer is on. For a train that started yesterday, "
                                    "add 'yesterday' to your question.",
                              f"🔎 आपके ब्राउज़र असिस्टेंट को {e.train} (शुरू होने की तारीख {e.sd:%d %b}) के लिए NTES का आधिकारिक "
                                    "पेज खोलने को कहा है। नतीजा एक-दो मिनट में अलर्ट (और WhatsApp, अगर जुड़ा है) पर आएगा।"),
                           p.intent, data={"job": job})
            return rep(_L(lang, f"Data temporarily unavailable ({e}). I won't guess — please retry shortly.",
                          f"डेटा अभी उपलब्ध नहीं ({e})। अंदाज़ा नहीं लगाऊँगा — थोड़ी देर में फिर पूछें।"), p.intent)
        except NotAuthorized as e:
            return rep(_L(lang, f"Data temporarily unavailable ({e}). I won't guess — please retry shortly.",
                          f"डेटा अभी उपलब्ध नहीं ({e})। अंदाज़ा नहीं लगाऊँगा — थोड़ी देर में फिर पूछें।"), p.intent)
        except TaskError as e:
            return rep(f"⚠️ {e}", p.intent)

    # ------------------------------------------------------------------ intents needing a train
    def _train_intent(self, p, ctx, user_id, rep, lang, train, sd, route, codes, channel):
        now = self.tracker.clock()
        demo = route.is_demo
        if p.intent == "stops_at":
            code = codes[0] if codes else ctx.get("last_station")
            if not code:
                return rep(_L(lang, "Which station?", "कौन सा स्टेशन?"), p.intent)
            s = route.stop(code)
            if not s:
                return rep(f"{code} is not on the scheduled route of {train} {route.train_name}.", p.intent, is_demo=demo)
            if s.halt:
                return rep(_L(lang, f"Yes — {train} has a scheduled halt at {s.name} ({code}): arr {s.arr or '—'}, dep {s.dep or '—'}"
                                    f"{', day ' + str(s.day) if s.day > 1 else ''}. (Timetable from {route.source_id})",
                              f"हाँ — {train} {s.name} ({code}) पर रुकती है: आगमन {s.arr or '—'}, प्रस्थान {s.dep or '—'}।"),
                           p.intent, data={"stop": s.__dict__}, is_demo=demo)
            return rep(_L(lang, f"No — {train} runs THROUGH {s.name} ({code}) without a scheduled halt "
                                f"(timetable passing time ~{s.pass_time}). I can still report when it passes — "
                                f"say 'track {train} from {code}'.",
                          f"नहीं — {train} {s.name} पर नहीं रुकती (बिना रुके निकलती है, लगभग {s.pass_time})। "
                          f"पार होने पर सूचना चाहिए तो लिखें 'track {train} from {code}'।"),
                       p.intent, data={"stop": s.__dict__}, is_demo=demo)

        if p.intent == "where":
            obs = self.tracker.observations(train, sd)
            st = build_status(route, obs, sd, now)
            le = st["last_event"]
            if not le:
                body = f"{train} {route.train_name} — {st['state'].replace('_', ' ')}. No position report from the source yet."
            else:
                where = (f"{le['km_after']} km after {le['station']}" if le["kind"] == "POSITION"
                         else f"{le['kind'].lower()} {le['station']}")
                body = (f"{train} {route.train_name} — {st['state'].replace('_', ' ')}\n"
                        f"Last reported: {where} at {fmt_hm(parse_iso(le['time']))} IST"
                        f"{' (' + format(st['delay_min'], '+d') + ' min)' if st['delay_min'] is not None else ''}\n"
                        f"Next scheduled stop: {st['next_stop'] or '—'} · Progress {st['progress_pct']}%")
            for a in st["alerts"]:
                body += f"\n⚠️ {a['type']} {a.get('station') or ''} {a.get('detail') or ''}".rstrip()
            return rep(body + "\n" + self._src_line(st), p.intent, data={"status": st}, is_demo=demo)

        if p.intent in ("crossed", "estimate"):
            code = codes[0] if codes else ctx.get("last_station")
            if not code:
                return rep(_L(lang, "Which station?", "कौन सा स्टेशन?"), p.intent)
            obs = self.tracker.observations(train, sd)
            c = evaluate_crossing(route, obs, code, now, sd)
            st = build_status(route, obs, sd, now)
            extra = f"\nClass: {c.event_class.value} · {c.verification.value} · confidence {c.confidence:.0%}\nMethod: {c.method}"
            if c.warnings:
                extra += "\n" + "\n".join("⚠️ " + w for w in c.warnings[:3])
            return rep(c.headline + extra + "\n" + self._src_line(st), p.intent, data={"crossing": c.to_dict()}, is_demo=demo)

        if p.intent == "track":
            if not user_id:
                return rep("Please sign in (or link WhatsApp) to create persistent tracking.", p.intent)
            ref = codes[0] if codes else ctx.get("last_station")
            if not ref:
                return rep(_L(lang, f"From which station should I watch {train}? (e.g. 'track {train} from Rewari')",
                              f"किस स्टेशन से {train} पर नज़र रखूँ? (जैसे 'track {train} from Rewari')"), p.intent)
            end = codes[1] if len(codes) > 1 else None
            low = p.raw.lower()
            dests = ["app"]
            for d, kw in (("whatsapp", "whatsapp"), ("telegram", "telegram"), ("email", "email"),
                          ("whatsapp_group", "group"), ("whatsapp_channel", "channel"), ("browser", "browser")):
                if kw in low and d not in dests:
                    dests.append(d)
            if channel == "whatsapp" and "whatsapp" not in dests:
                dests.append("whatsapp")
            if channel == "telegram" and "telegram" not in dests:
                dests.append("telegram")
            interval = p.interval_min or 15
            t = self.tracker.draft_task(user_id, train, sd, ref, end, interval, dests)
            ctx["pending_task"] = t["id"]
            s = t["summary"]
            lines = [_L(lang, "Please confirm this tracking task:", "कृपया इस tracking की पुष्टि करें:"),
                     f"• Train: {s['train']}", f"• Service date: {s['service_date']}",
                     f"• Watch station: {s['reference_station']}", f"• Until: {s['end_station']}",
                     f"• Every: {s['interval_min']} min" + ("" if p.interval_min else " (default — say e.g. 'every 30 min' to change)"),
                     f"• Send to: {', '.join(s['destinations'])}", f"• Data: {s['data_availability']}"]
            lines += [f"• Note: {n}" for n in s["destination_notes"]]
            lines.append(_L(lang, "Reply YES to start or NO to cancel.", "शुरू करने के लिए हाँ / YES, रद्द करने के लिए नहीं / NO।"))
            return rep("\n".join(lines), p.intent, data={"task": t}, is_demo=demo)

        if p.intent == "vacancy":
            cls = p.travel_class or ctx.get("travel_class")
            board = codes[0] if codes else ctx.get("boarding")
            if not cls:
                ctx["last_train"] = train
                return rep(_L(lang, "Which class? (1A, 2A, 3A, SL, CC…)", "कौन सी श्रेणी? (1A, 2A, 3A, SL, CC…)"), p.intent)
            if not board:
                return rep(_L(lang, "From which boarding station?", "किस स्टेशन से चढ़ना है?"), p.intent)
            ctx["travel_class"], ctx["boarding"] = cls, board
            _, snap = self.registry.call("get_chart_vacancy", train, sd, board, cls)
            summary = summarize_vacancy(snap.to_dict())
            text = (f"Chart vacancy (post-chart, chart #{snap.chart_no}) for {train} {cls} from {board}, "
                    f"journey {sd:%d %b}: {summary['vacant_full']} berth(s) vacant for the whole remaining journey, "
                    f"{summary['vacant_partial']} vacant for part of it.\n"
                    f"Chart time: {fmt_hm(snap.source_time)} IST · Source: {snap.source_id}\n"
                    "ℹ️ Chart vacancy ≠ bookable seat ≠ guaranteed empty berth onboard. Book only via official IRCTC channels.\n"
                    f"Verify: {snap.official_url}")
            return rep(text, p.intent, data={"vacancy": snap.to_dict(), "summary": summary}, is_demo=snap.is_demo)

        return rep("Not sure how to do that yet. Type HELP.", p.intent)

    def _discovery(self, p, ctx, rep, lang, sd):
        flat = [c[0].code if len(c) == 1 else None for c in p.stations]
        if p.intent == "between":
            if len(p.stations) < 2 or None in flat[:2]:
                opts = [", ".join(s.code for s in c) for c in p.stations if len(c) > 1]
                return rep("Please give two stations (codes help with duplicates" + (f": {'; '.join(opts)}" if opts else "") + ").",
                           p.intent)
            day = p.service_date or self._today()
            prov, rows = self.registry.call("find_trains_between", flat[0], flat[1], day)
            if not rows:
                return rep(f"No trains found {flat[0]}→{flat[1]} on {day:%d %b} from {prov.source_id}.", p.intent)
            lines = [f"{r['number']} {r['name']} dep {fmt_hm(parse_iso(r['departure']))} arr {fmt_hm(parse_iso(r['arrival']))} "
                     f"({r['duration_min'] // 60}h{r['duration_min'] % 60:02d})" for r in rows]
            return rep(f"Trains {flat[0]}→{flat[1]} on {day:%d %b} (source {prov.source_id}):\n" + "\n".join(lines),
                       p.intent, data={"trains": rows}, is_demo=prov.is_demo)
        if not p.stations:
            return rep("Which station?", p.intent)
        if flat[0] is None:
            opts = [f"{s.code} ({s.name})" for s in p.stations[0]]
            return rep("Which station?\n" + "\n".join(opts), p.intent)
        now = self.tracker.clock()
        prov, rows = self.registry.call("get_station_board", flat[0], now - timedelta(hours=1), now + timedelta(hours=4))
        if not rows:
            return rep(f"No trains in the next 4 h at {flat[0]} from {prov.source_id}.", p.intent, is_demo=prov.is_demo)
        lines = [f"{r['number']} {r['name']} arr {r['sched_arr'] or '—'} dep {r['sched_dep'] or '—'}"
                 f"{' PF ' + r['platform'] if r.get('platform') else ''}{'' if r['halt'] else ' (non-stop)'}" for r in rows]
        return rep(f"Scheduled at {flat[0]} (source {prov.source_id}):\n" + "\n".join(lines), p.intent,
                   data={"board": rows}, is_demo=prov.is_demo)

    def _personal(self, p, user_id, rep, lang):
        jr, nw = getattr(self, "journeys", None), getattr(self, "news", None)
        if not user_id:
            return rep("Please sign in first.", p.intent)
        if p.intent == "trips":
            if not jr:
                return rep("My Journeys is not available.", "trips")
            js = jr.list(user_id, include_done=False)
            if not js:
                return rep(_L(lang, "No upcoming trips. Add one in My Journeys (train, start date, from, to, PNR).",
                              "कोई आने वाली यात्रा नहीं। My Journeys में जोड़ें।"), "trips")
            lines = ["🧳 Your upcoming trips:"]
            for j in js[:8]:
                ev = j.get("last_eval") or {}
                when = f"dep {fmt_hm(parse_iso(ev['dep']))} ({ev.get('dep_basis')})" if ev.get("dep") else f"start {j['start_date']}"
                lines.append(f"• {j['train']} {j['board']}→{j['dest']} · {j['start_date']} · {when}"
                             + (f" · {j['coach']} {j['berth']}" if j.get("coach") else ""))
            return rep("\n".join(lines), "trips", data={"journeys": js})
        if p.intent == "news":
            if not nw:
                return rep("Announcements are not available.", "news")
            items = nw.items(user_id, relevant_only=not p.train, limit=40)
            if p.train:
                items = [i for i in items if p.train in i["trains"]]
            if not items:
                return rep(_L(lang, "No announcements that concern your trains right now." + (f" (none mention {p.train})" if p.train else ""),
                              "अभी आपकी ट्रेनों से जुड़ी कोई सूचना नहीं।"), "news")
            return rep("📢 Latest announcements for you:\n" + "\n".join(
                f"• {i['title'][:150]} — {i['source_name']}" + (f" ({i['why']})" if i.get("why") else "") for i in items[:6]),
                "news", data={"items": items[:6]})
        # PNR
        if not p.pnr:
            return rep(_L(lang, "Send the 10-digit PNR, e.g. 'PNR 4512345678'.", "10 अंकों का PNR भेजें।"), "pnr")
        if not self.assist:
            return rep("PNR checks need the browser assistant on your computer (start.bat / start.sh). "
                       "Or check on https://www.indianrail.gov.in/ yourself.", "pnr")
        job = self.assist.queue_job(user_id, "pnr", {"pnr": p.pnr})
        return rep(_L(lang, f"🎫 Opening the official PNR page for …{p.pnr[-3:]} on your computer. The page has a CAPTCHA: "
                            "please type it in the browser window when it asks. The status comes back here and on WhatsApp.",
                      f"🎫 PNR …{p.pnr[-3:]} का आधिकारिक पेज आपके कंप्यूटर पर खुल रहा है। ब्राउज़र विंडो में CAPTCHA आप भरें।"),
                   "pnr", data={"job": job})

    def _search(self, p, user_id, rep, lang):
        q = (p.query or "").strip()
        if len(q) < 3:
            return rep(_L(lang, "Which train name should I search for? e.g. 'search train Rajdhani'",
                          "किस नाम की ट्रेन खोजूँ? जैसे 'Rajdhani train khojo'"), "search")
        try:
            prov, rows = self.registry.call("search_trains", q)
            if rows:
                return rep(f"Trains matching '{q}' (source {prov.source_id}):\n" +
                           "\n".join(f"{r['number']} {r['name']}" for r in rows[:15]), "search",
                           data={"results": rows}, is_demo=prov.is_demo)
        except (NotSupported, ProviderError):
            pass
        if self.assist and user_id:
            job = self.assist.queue_job(user_id, "search", {"query": q})
            return rep(_L(lang, f"🔎 Searching the official site for '{q}' with your browser assistant. "
                                "The list arrives as an alert shortly.",
                          f"🔎 '{q}' नाम की ट्रेनें आधिकारिक साइट पर खोजी जा रही हैं — सूची जल्द अलर्ट में आएगी।"),
                       "search", data={"job": job})
        return rep(f"No train-name search source is connected. Open NTES: https://enquiry.indianrail.gov.in/mntes/", "search")

    # ------------------------------------------------------------------ task control
    def _target_task(self, p, ctx, user_id):
        tasks = [t for t in self.store.list_tasks(user_id, include_closed=False)]
        if p.train:
            tasks = [t for t in tasks if t["train_number"] == p.train]
        elif ctx.get("last_task_id"):
            t = self.store.get_task(ctx["last_task_id"], user_id)
            if t and t["status"] in ("ACTIVE", "PAUSED"):
                return t, None
        if len(tasks) == 1:
            return tasks[0], None
        if not tasks:
            return None, "You have no active tracking tasks."
        return None, "Which one? " + ", ".join(f"{t['train_number']}@{t['reference_station']}" for t in tasks)

    def _task_control(self, p, ctx, user_id, rep, lang):
        if not user_id:
            return rep("Please sign in or link WhatsApp first.", p.intent)
        t, err = self._target_task(p, ctx, user_id)
        if not t:
            return rep(err, p.intent)
        ctx["last_task_id"] = t["id"]
        try:
            if p.intent in ("pause", "resume", "cancel"):
                t2 = self.tracker.set_state(t["id"], user_id, p.intent)
                verb = {"pause": "⏸️ Paused", "resume": "▶️ Resumed", "cancel": "⏹️ Stopped"}[p.intent]
                return rep(f"{verb} tracking of {t2['train_number']} at {t2['reference_station']}.", p.intent, data={"task": t2})
            if p.intent == "change_interval":
                if not p.interval_min:
                    return rep("New interval in minutes? (5–240)", p.intent)
                t2 = self.tracker.update(t["id"], user_id, interval_min=p.interval_min)
                return rep(f"🔁 Interval for {t2['train_number']} is now every {t2['interval_min']} min.", p.intent, data={"task": t2})
            if p.intent == "delay_alert":
                if p.threshold_min is None:
                    return rep("Alert above how many minutes of delay?", p.intent)
                t2 = self.tracker.update(t["id"], user_id, delay_threshold_min=p.threshold_min)
                return rep(f"🔔 I'll alert you if {t2['train_number']}'s reported delay reaches {p.threshold_min} min.",
                           p.intent, data={"task": t2})
        except TaskError as e:
            return rep(f"⚠️ {e}", p.intent)
        return rep("Done.", p.intent)


def summarize_vacancy(snap: dict) -> dict:
    """Count berths free on every segment from the boarding station onward (source-supplied only)."""
    board = snap["boarding_station"]
    full = partial = 0
    for c in snap["coaches"]:
        for b in c["berths"]:
            segs = b["segments"]
            idx = next((i for i, s in enumerate(segs) if s["from"] == board), None)
            if idx is None:
                continue
            rest = segs[idx:]
            free = [not s["occupied"] for s in rest]
            if all(free):
                full += 1
            elif any(free):
                partial += 1
    return {"vacant_full": full, "vacant_partial": partial}
