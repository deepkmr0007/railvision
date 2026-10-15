"""Browser runner (Playwright, runs on YOUR computer).

Rules built into the code, not left to configuration:
  * It behaves like one person using the site: one page at a time, real browser, no stealth
    plugins, no fingerprint or user-agent spoofing, no proxy rotation.
  * It NEVER solves or bypasses a CAPTCHA, login or bot check. When one appears it stops and
    (in a visible window) hands control to you; otherwise it reports `captcha` / `blocked`.
  * It only reads what the page shows (visible text, tables, and the JSON the page itself loads).
"""
from __future__ import annotations

import base64
import time
from datetime import date, datetime
from typing import Callable

from ..core.timeutil import iso, now_ist
from .recipes import Recipe, date_variants, render

VISIBLE_JS = "el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length) && getComputedStyle(el).visibility !== 'hidden'"

DETECT_JS = """() => {
  const vis = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length)) && getComputedStyle(el).visibility !== 'hidden';
  const t = (document.body ? document.body.innerText : '').slice(0, 30000).toLowerCase();
  const capEl = [...document.querySelectorAll('iframe[src*="recaptcha"],iframe[src*="hcaptcha"],iframe[src*="turnstile"],iframe[title*="challenge" i],img[src*="captcha" i],img[id*="captcha" i],img[alt*="captcha" i],input[name*="captcha" i],input[id*="captcha" i],input[placeholder*="captcha" i]')].some(vis);
  const capTxt = /enter (the )?(characters|text) (shown|you see)|verify (that )?you are (a )?human|i'm not a robot|security check|solve the puzzle/.test(t);
  const blocked = /access denied|request (was )?blocked|you don't have permission|unusual traffic|too many requests|error 403|forbidden|rate limit/.test(t) && t.length < 4000;
  const login = [...document.querySelectorAll('input[type=password]')].some(vis);
  return {captcha: capEl || capTxt, blocked, login, textLen: t.length};
}"""

CAPTURE_JS = """() => {
  const vis = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const tables = [...document.querySelectorAll('table')].filter(vis).slice(0, 25).map(tb =>
    [...tb.rows].slice(0, 500).map(r => [...r.cells].slice(0, 16).map(c => c.innerText.trim().replace(/\\s+/g, ' '))));
  return {text: (document.body ? document.body.innerText : '').slice(0, 200000), tables, title: document.title, url: location.href};
}"""

SCAN_JS = """() => {
  const vis = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length) && getComputedStyle(el).visibility !== 'hidden' && !el.disabled;
  const lbl = el => {
    const parts = [el.id, el.name, el.getAttribute('placeholder'), el.getAttribute('aria-label'), el.title, el.getAttribute('formcontrolname')];
    if (el.id) { const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]'); if (l) parts.push(l.innerText); }
    const cl = el.closest('label'); if (cl) parts.push(cl.innerText);
    let p = el.previousElementSibling; if (p && p.innerText && p.innerText.length < 60) parts.push(p.innerText);
    const par = el.parentElement; if (par && par.innerText && par.innerText.length < 80) parts.push(par.innerText);
    return parts.filter(Boolean).join(' | ').toLowerCase().slice(0, 300);
  };
  let n = 0; const out = [];
  document.querySelectorAll('input,select,textarea,button,a,[role=button],[role=option],li,td,span,label,div').forEach(el => {
    const tag = el.tagName.toLowerCase();
    const isField = ['input','select','textarea'].includes(tag);
    const isAct = ['button','a'].includes(tag) || el.getAttribute('role') === 'button' || (tag === 'input' && ['submit','button'].includes(el.type));
    const txt = (el.innerText || el.value || '').trim();
    const leafy = !isField && !isAct && txt && txt.length <= 40 && el.children.length === 0;
    if (!(isField || isAct || leafy) || !vis(el)) return;
    if (el.closest('[data-rv-overlay]')) return;
    const id = 'rv' + (n++); el.setAttribute('data-rv-id', id);
    out.push({id, tag, type: (el.type || '').toLowerCase(), label: lbl(el), text: txt.slice(0, 80), field: isField, act: isAct,
              options: tag === 'select' ? [...el.options].map(o => o.text.trim()).slice(0, 60) : null, order: out.length});
  });
  return out;
}"""

SUBMIT_WORDS = ["get train chart", "get chart", "get status", "check status", "show status", "get", "submit", "search",
                "show", "find", "check", "go", "proceed", "view"]


class Blocked(Exception):
    def __init__(self, status: str, reason: str):
        super().__init__(reason)
        self.status = status


def _score(label: str, want: list[str], avoid: list[str]) -> int:
    s = sum(10 - i for i, w in enumerate(want) if w in label)
    return s - sum(8 for a in avoid if a in label)


class Runner:
    def __init__(self, page, *, handover: bool = False, handover_timeout_s: int = 300,
                 on_status: Callable[[str, str], None] | None = None, log: Callable[[str], None] = print):
        self.page, self.handover, self.handover_timeout_s = page, handover, handover_timeout_s
        self.on_status = on_status or (lambda s, r: None)
        self.log = log
        self.json_responses: list[dict] = []
        self.steps: list[str] = []
        page.on("response", self._on_response)

    # ------------------------------------------------------------ helpers
    def _on_response(self, resp):
        try:
            ct = (resp.headers or {}).get("content-type", "")
            if "json" not in ct or len(self.json_responses) >= 25:
                return
            body = resp.text()
            if len(body) > 400_000:
                return
            self.json_responses.append({"url": resp.url[:300], "status": resp.status, "body": body})
        except Exception:
            pass

    def _note(self, s: str):
        self.steps.append(s)
        self.log("   · " + s)

    def check_blockers(self, where: str):
        d = self.page.evaluate(DETECT_JS)
        kind = "captcha" if d["captcha"] else "login" if d["login"] else "blocked" if d["blocked"] else None
        if not kind:
            return
        reason = {"captcha": "The site is showing a CAPTCHA / human check",
                  "login": "The site is asking for a login", "blocked": "The site refused the request"}[kind] + f" ({where})."
        if not self.handover:
            raise Blocked(kind, reason + " Not bypassed. Run the assistant with a visible window to hand it over to you.")
        self.on_status("needs_you", reason)
        self.log(f"!! {reason} Please handle it in the browser window — I'll wait up to {self.handover_timeout_s // 60} min.")
        end = time.time() + self.handover_timeout_s
        while time.time() < end:
            time.sleep(2)
            d = self.page.evaluate(DETECT_JS)
            if not (d["captcha"] or d["login"] or d["blocked"]):
                self._note(f"you cleared the {kind} check")
                self.on_status("resumed", "")
                return
        raise Blocked(kind, reason + " Timed out waiting for you.")

    def _wait_results(self, recipe: Recipe, vars_: dict, timeout_s: float = 30) -> bool:
        wants = [render(t, vars_) for t in recipe.result_texts] if recipe.result_texts else []
        end = time.time() + timeout_s
        while time.time() < end:
            try:
                self.page.wait_for_load_state("networkidle", timeout=2500)
            except Exception:
                pass
            self.check_blockers("while waiting for results")
            txt = self.page.evaluate("() => document.body ? document.body.innerText : ''")
            has_time = __import__("re").search(r"\b([01]?\d|2[0-3]):[0-5]\d\b", txt)
            if (not wants or all(w and w in txt for w in wants)) and (has_time or recipe.kind != "status") and len(txt) > 80:
                return True
            if self.handover and time.time() > end - 1:
                break
            time.sleep(0.7)
        return False

    def _loc(self, rvid: str):
        return self.page.locator(f'[data-rv-id="{rvid}"]')

    # ------------------------------------------------------------ auto (smart form-filler)
    def _scan(self):
        return self.page.evaluate(SCAN_JS)

    def _fields(self, items, want, avoid=(), types=("text", "search", "number", "tel", "")):
        c = [(i, _score(i["label"], want, list(avoid))) for i in items if i["field"] and i["tag"] in ("input", "textarea")
             and i["type"] in types]
        c = [x for x in c if x[1] > 0]
        return max(c, key=lambda x: (x[1], -x[0]["order"]))[0] if c else None

    def _click_text(self, texts: list[str], exact=False) -> bool:
        items = self._scan()
        low = [t.lower() for t in texts if t]
        for it in items:
            tx = it["text"].lower()
            if any((tx == t) if exact else (tx == t or tx.startswith(t)) for t in low):
                self._loc(it["id"]).first.click(timeout=5000)
                return True
        return False

    def run_auto(self, recipe: Recipe, vars_: dict):
        items = self._scan()
        main_key = "query" if recipe.kind == "search" else "train"
        train_field = self._fields(items, ["train no", "train number", "train name", "train", "gaadi"], ["station", "from", "to"])
        if not train_field and recipe.nav_texts:
            if self._click_text(recipe.nav_texts):
                self._note("opened menu: " + "/".join(recipe.nav_texts[:2]))
                self.page.wait_for_load_state("domcontentloaded")
                time.sleep(1.0)
                self.check_blockers("after menu")
                items = self._scan()
                train_field = self._fields(items, ["train no", "train number", "train name", "train", "gaadi"], ["station"])
        if not train_field:
            raise Blocked("recipe_failed", "Could not find a train number box on the page. Run TEACH once to record the steps.")
        val = str(vars_.get(main_key) or vars_.get("train"))
        loc = self._loc(train_field["id"]).first
        loc.click()
        loc.fill("")
        loc.type(val, delay=70)
        self._note(f"typed {main_key} {val}")
        time.sleep(1.0)
        # autocomplete suggestion that starts with / contains the number
        for it in self._scan():
            if not it["field"] and it["id"] != train_field["id"] and val in it["text"] and it["text"].strip() != val[:40] \
                    and it["tag"] in ("li", "a", "div", "span", "td", "button") and recipe.kind != "search":
                try:
                    self._loc(it["id"]).first.click(timeout=3000)
                    self._note(f"picked suggestion '{it['text'][:40]}'")
                    time.sleep(0.8)
                except Exception:
                    pass
                break
        if vars_.get("date") and recipe.kind != "search":
            self._set_date(vars_["date"])
        if vars_.get("station"):
            items = self._scan()
            st = self._fields(items, ["boarding", "station", "from"], ["train"])
            if st:
                sl = self._loc(st["id"]).first
                sl.fill("")
                sl.type(str(vars_["station"]), delay=60)
                time.sleep(0.9)
                for it in self._scan():
                    if not it["field"] and it["tag"] in ("li", "a", "div", "span", "td") and str(vars_["station"]).upper() in it["text"].upper() \
                            and it["text"].strip().upper() != str(vars_["station"]).upper():
                        try:
                            self._loc(it["id"]).first.click(timeout=3000)
                        except Exception:
                            pass
                        break
                self._note(f"boarding station {vars_['station']}")
        self._submit(train_field)

    def _set_date(self, d):
        d = d if isinstance(d, date) else date.fromisoformat(str(d))
        variants = [v.lower() for v in date_variants(d)]
        items = self._scan()
        # 1) <select> containing the date
        for it in items:
            if it["tag"] == "select" and it["options"]:
                for o in it["options"]:
                    if any(o.lower().startswith(v) or v in o.lower() for v in variants):
                        self._loc(it["id"]).first.select_option(label=o)
                        self._note(f"start date '{o}' (dropdown)")
                        return
        # 2) a clickable date chip / button / list item (NTES-style start date list)
        for it in items:
            tx = it["text"].lower()
            if not it["field"] and tx and any(tx == v or tx.startswith(v) for v in variants):
                self._loc(it["id"]).first.click(timeout=4000)
                self._note(f"start date '{it['text']}' (clicked)")
                return
        # 3) input type=date / text date box
        for it in items:
            if it["tag"] == "input" and (it["type"] == "date" or _score(it["label"], ["start date", "journey date", "date", "doj"], []) > 0):
                loc = self._loc(it["id"]).first
                if it["type"] == "date":
                    loc.fill(d.isoformat())
                else:
                    ph = it["label"]
                    fmt = ("%d/%m/%Y" if "dd/mm/yyyy" in ph else "%Y-%m-%d" if "yyyy-mm-dd" in ph
                           else "%d-%b-%Y" if "mmm" in ph else "%d-%m-%Y")
                    loc.fill(d.strftime(fmt))
                    loc.press("Tab")
                self._note(f"date {d.isoformat()} (input)")
                return
        self._note("no date control found — using the site's default date")

    def _submit(self, near):
        items = self._scan()
        acts = [i for i in items if i["act"]]
        best, score = None, -1
        for i in acts:
            t = (i["text"] or i["label"]).lower().strip()
            for k, w in enumerate(SUBMIT_WORDS):
                if t == w or t.startswith(w + " ") or (len(w) > 4 and w in t):
                    s = 100 - k * 5 - abs(i["order"] - near["order"]) * 0.01 + (5 if i["order"] > near["order"] else 0)
                    if s > score:
                        best, score = i, s
                    break
        if best:
            self._loc(best["id"]).first.click(timeout=6000)
            self._note(f"pressed '{(best['text'] or best['label'])[:30]}'")
        else:
            self._loc(near["id"]).first.press("Enter")
            self._note("pressed Enter")

    # ------------------------------------------------------------ recorded steps
    def run_recorded(self, recipe: Recipe, vars_: dict):
        for n, st in enumerate(recipe.steps, 1):
            text, value, sel = render(st.text, vars_), render(st.value, vars_), st.selector
            try:
                if st.action == "goto":
                    self.page.goto(value, wait_until="domcontentloaded", timeout=st.timeout_ms)
                elif st.action == "click":
                    loc = None
                    if text and (st.text != text or not sel):      # text had a placeholder, or no selector
                        loc = self.page.get_by_text(text, exact=False).first
                    if loc is None or not loc.count():
                        loc = self.page.locator(sel).first if sel else self.page.get_by_text(text or "", exact=False).first
                    loc.click(timeout=st.timeout_ms)
                elif st.action == "fill":
                    loc = self.page.locator(sel).first
                    loc.fill("")
                    loc.type(value or "", delay=60)
                    time.sleep(0.6)
                elif st.action == "select":
                    try:
                        self.page.locator(sel).first.select_option(label=value, timeout=st.timeout_ms)
                    except Exception:
                        self.page.locator(sel).first.select_option(value=value, timeout=st.timeout_ms)
                elif st.action == "press":
                    self.page.locator(sel).first.press(value or "Enter")
                elif st.action == "wait":
                    time.sleep(min(float(value or 1), 10))
                elif st.action == "wait_text":
                    self.page.get_by_text(text, exact=False).first.wait_for(timeout=st.timeout_ms)
                self._note(f"step {n}: {st.action} {(text or sel or value or '')[:40]}")
                time.sleep(0.5)
                self.check_blockers(f"after step {n}")
            except Blocked:
                raise
            except Exception as e:
                if st.optional:
                    self._note(f"step {n} skipped ({type(e).__name__})")
                    continue
                raise Blocked("recipe_failed", f"Recorded step {n} ({st.action} {text or sel}) failed: {type(e).__name__}. "
                                               "The site may have changed — run TEACH again.")

    # ------------------------------------------------------------ public entry
    def run(self, recipe: Recipe, vars_: dict) -> dict:
        started = now_ist()
        status, reason = "ok", ""
        try:
            self._note(f"open {recipe.start_url}")
            self.page.goto(recipe.start_url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(1.0)
            self.check_blockers("on opening the site")
            if recipe.mode == "recorded" and recipe.steps:
                self.run_recorded(recipe, vars_)
            else:
                self.run_auto(recipe, vars_)
            if not self._wait_results(recipe, vars_):
                status, reason = "no_result", "The page did not show a result for this train/date."
            if status == "ok" and recipe.kind == "berth" and vars_.get("class"):
                if self._click_text([str(vars_["class"])], exact=True):
                    self._note(f"opened class {vars_['class']}")
                    time.sleep(1.5)
                    self._wait_results(recipe, vars_, 15)
        except Blocked as b:
            status, reason = b.status, str(b)
        except Exception as e:
            status, reason = "error", f"{type(e).__name__}: {str(e)[:200]}"
        cap = {"text": "", "tables": [], "title": "", "url": ""}
        shot = None
        try:
            cap = self.page.evaluate(CAPTURE_JS)
            png = self.page.screenshot(full_page=True, type="jpeg", quality=55)
            if len(png) > 900_000:
                png = self.page.screenshot(full_page=False, type="jpeg", quality=55)
            shot = base64.b64encode(png).decode()
        except Exception:
            pass
        return {"recipe": recipe.name, "kind": recipe.kind, "vars": {k: str(v) for k, v in vars_.items()},
                "status": status, "reason": reason, "text": cap["text"], "tables": cap["tables"], "title": cap["title"],
                "url": cap["url"], "json": self.json_responses, "screenshot_b64": shot, "steps": self.steps,
                "started_at": iso(started), "finished_at": iso(now_ist())}
