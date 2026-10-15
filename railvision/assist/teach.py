"""TEACH mode: you do one search yourself in a visible browser; RAILVISION records the steps.

The sample values you give (train number, start date, station, class, query) are turned into
placeholders, so the same recipe works for every train in your watch list.
"""
from __future__ import annotations

import time

from .recipes import Recipe, Step, templatize

RECORDER_JS = r"""
(() => {
  if (window.__rvRec) return; window.__rvRec = true;
  const sel = el => {
    if (el.id && !/\d{5,}|^ng-|^mat-|^react/.test(el.id)) return '#' + CSS.escape(el.id);
    for (const a of ['name', 'data-testid', 'formcontrolname', 'aria-label', 'placeholder']) {
      const v = el.getAttribute(a); if (v && v.length < 60) return el.tagName.toLowerCase() + '[' + a + '="' + v.replace(/"/g, '\\"') + '"]';
    }
    const path = []; let e = el;
    while (e && e.nodeType === 1 && path.length < 5 && e !== document.body) {
      let i = 1, s = e; while ((s = s.previousElementSibling)) if (s.tagName === e.tagName) i++;
      path.unshift(e.tagName.toLowerCase() + ':nth-of-type(' + i + ')'); e = e.parentElement;
    }
    return path.join(' > ');
  };
  const send = st => { try { window.rvRecord(st); } catch (e) {} };
  document.addEventListener('click', ev => {
    const el = ev.target.closest('button,a,[role=button],[role=option],li,td,span,label,div,input[type=submit],input[type=button],input[type=radio],input[type=checkbox]');
    if (!el || el.closest('[data-rv-overlay]')) return;
    if (['INPUT', 'SELECT', 'TEXTAREA'].includes(el.tagName) && !['submit', 'button', 'radio', 'checkbox'].includes(el.type)) return;
    const text = (el.innerText || el.value || '').trim().replace(/\s+/g, ' ').slice(0, 80);
    send({action: 'click', selector: sel(el), text});
  }, true);
  document.addEventListener('change', ev => {
    const el = ev.target; if (!el || el.closest('[data-rv-overlay]')) return;
    if (el.tagName === 'SELECT') send({action: 'select', selector: sel(el), value: el.options[el.selectedIndex] ? el.options[el.selectedIndex].text.trim() : el.value});
    else if (['INPUT', 'TEXTAREA'].includes(el.tagName) && !['submit', 'button', 'radio', 'checkbox'].includes(el.type)) send({action: 'fill', selector: sel(el), value: el.value});
  }, true);
  document.addEventListener('keydown', ev => {
    if (ev.key === 'Enter' && ev.target && ev.target.tagName === 'INPUT') {
      send({action: 'fill', selector: sel(ev.target), value: ev.target.value});
      send({action: 'press', selector: sel(ev.target), value: 'Enter'});
    }
  }, true);
  const mk = () => {
    if (document.querySelector('[data-rv-overlay]') || !document.body) return;
    const d = document.createElement('div'); d.setAttribute('data-rv-overlay', '1');
    d.style.cssText = 'position:fixed;z-index:2147483647;right:12px;bottom:12px;background:#0A111E;color:#fff;font:14px system-ui;padding:10px 12px;border-radius:10px;box-shadow:0 4px 18px rgba(0,0,0,.4);max-width:300px';
    d.innerHTML = '<b>RAILVISION · teach mode</b><br>Do the search once, wait for the result to appear, then press Save.<br>';
    const b = document.createElement('button'); b.textContent = 'Save recipe'; b.style.cssText = 'margin-top:8px;padding:6px 12px;border:0;border-radius:6px;background:#78A2FF;color:#06142E;font-weight:700;cursor:pointer';
    b.onclick = () => { try { window.rvDone(); } catch (e) {} };
    d.appendChild(b); document.body.appendChild(d);
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mk); else mk();
})();
"""


def clean_steps(raw: list[dict]) -> list[dict]:
    """Collapse noise: repeated fills on the same field keep the last; drop clicks on the overlay."""
    out: list[dict] = []
    for st in raw:
        if st.get("action") == "fill" and out and out[-1].get("action") == "fill" and out[-1].get("selector") == st.get("selector"):
            out[-1] = st
            continue
        if st.get("action") == "click" and out and out[-1] == st:
            continue
        out.append(st)
    # a fill immediately followed by a click on the same input is redundant
    return [s for s in out if s.get("action") != "click" or s.get("text") or s.get("selector")]


def build_recipe(name: str, kind: str, start_url: str, raw_steps: list[dict], sample: dict) -> Recipe:
    steps: list[Step] = []
    skip_next_click = False
    for st in clean_steps(raw_steps):
        sel_l = (st.get("selector") or "").lower()
        if "captcha" in sel_l or "captcha" in (st.get("text") or "").lower():
            if st["action"] == "fill" and not (steps and steps[-1].action == "human"):
                steps.append(Step(action="human", timeout_ms=300000))     # never record a CAPTCHA answer
                skip_next_click = True
            continue
        if skip_next_click and st["action"] in ("click", "press"):
            skip_next_click = False          # the person pressed Submit as part of the human step
            continue
        text = templatize(st.get("text") or "", sample) or None
        value = templatize(st.get("value") or "", sample) or None
        # an autocomplete row like "12951 - MUMBAI RAJDHANI" must match by the number only
        for ph in ("{train}", "{query}", "{station}"):
            if text and ph in text and text != ph:
                text = ph
        if st["action"] == "fill" and value and value != "{train}" and "{train}" in value and \
                any(s.action == "fill" and s.selector == st.get("selector") and s.value == "{train}" for s in steps):
            continue        # the site rewrote the box after a suggestion was picked; keep the clean fill
        steps.append(Step(action=st["action"], selector=st.get("selector"), text=text, value=value))
    result = ["{pnr}"] if sample.get("pnr") else ["{train}"] if sample.get("train") else (["{query}"] if sample.get("query") else [])
    return Recipe(name=name, kind=kind, start_url=start_url, mode="recorded", steps=steps, result_texts=result,
                  verified=False, notes=f"Recorded in TEACH mode with sample values {sorted(sample)}; becomes "
                                        "verified after its first successful automatic run.")


def teach(page, name: str, kind: str, start_url: str, sample: dict, timeout_s: int = 900, log=print,
          auto_done=None) -> Recipe:
    raw: list[dict] = []
    done = {"v": False}
    page.expose_binding("rvRecord", lambda src, st: raw.append(st))
    page.expose_binding("rvDone", lambda src: done.__setitem__("v", True))
    page.add_init_script(RECORDER_JS)
    page.goto(start_url, wait_until="domcontentloaded")
    log("Teach mode: do the search in the browser window, then press 'Save recipe' (bottom-right).")
    end = time.time() + timeout_s
    while not done["v"] and time.time() < end:
        if auto_done and auto_done(page, raw):      # used by tests to drive the page programmatically
            break
        page.wait_for_timeout(300)
    if not raw:
        raise RuntimeError("Nothing was recorded. Do the search in the window, then press Save.")
    return build_recipe(name, kind, start_url, raw, sample)
