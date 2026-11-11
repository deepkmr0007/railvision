"""One-off diagnostic: what do the real official pages look like to a phone browser, and how far does the
app's driver (rv_driver.js) get on them?  A handful of page loads, paced; nothing is solved or bypassed."""
import json, sys, time, traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
from playwright.sync_api import sync_playwright
from phone_harness import run_job
from railvision.assist.recipes import DEFAULT_RECIPES
from railvision.parsers import irctc, ntes

OUT = ROOT / "probe_out"; OUT.mkdir(exist_ok=True)
IST = timezone(timedelta(hours=5, minutes=30))
today = datetime.now(IST).date()
TRAIN = sys.argv[1] if len(sys.argv) > 1 else "12951"
R = {"when": datetime.now(IST).isoformat(), "train": TRAIN}

DUMP = """() => ({url: location.href, title: document.title, text: document.body.innerText.slice(0, 6000),
  inputs: [...document.querySelectorAll('input,select,textarea,button,a,[role=button],[role=combobox],[role=option],mat-select,li')].slice(0, 400).map(e => ({
    tag: e.tagName, id: e.id, name: e.getAttribute('name'), type: e.getAttribute('type'), ph: e.getAttribute('placeholder'),
    cls: (e.className && e.className.toString().slice(0, 80)) || '', aria: e.getAttribute('aria-label'), fc: e.getAttribute('formcontrolname'),
    href: e.getAttribute('href'), oc: (e.getAttribute('onclick') || '').slice(0, 120), txt: (e.innerText || e.value || '').trim().slice(0, 60),
    vis: !!(e.offsetWidth || e.offsetHeight)})),
  frames: [...document.querySelectorAll('iframe')].map(f => f.src), html_len: document.documentElement.outerHTML.length})"""

def watch(page, bucket):
    def on_resp(resp):
        try:
            rq = resp.request
            if rq.resource_type not in ("xhr", "fetch", "document"):
                return
            item = {"m": rq.method, "u": resp.url[:300], "s": resp.status, "t": rq.resource_type, "post": (rq.post_data or "")[:600],
                    "ct": resp.headers.get("content-type", "")}
            hk = {k: v[:80] for k, v in rq.headers.items() if k.lower() not in ("user-agent", "accept", "accept-language", "accept-encoding", "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform", "referer", "cookie")}
            item["hdr"] = hk
            try:
                if "json" in item["ct"] or rq.resource_type != "document":
                    item["body"] = resp.text()[:6000]
            except Exception as e:
                item["body_err"] = str(e)[:80]
            bucket.append(item)
        except Exception:
            pass
    page.on("response", on_resp)

def site(ctx, key, url, recipe_name, vars_, parse):
    r = R[key] = {"net": []}
    pg = ctx.new_page(); watch(pg, r["net"])
    try:
        resp = pg.goto(url, wait_until="domcontentloaded", timeout=60000)
        r["http"] = resp.status if resp else None
        pg.wait_for_timeout(6000)
        r["landing"] = pg.evaluate(DUMP)
        pg.screenshot(path=str(OUT / f"{key}_1_landing.png"))
        (OUT / f"{key}_landing.html").write_text(pg.content()[:800000])
    except Exception as e:
        r["landing_error"] = repr(e)[:400]
    pg.close()
    time.sleep(20)
    pg = ctx.new_page(); r["net2"] = []; watch(pg, r["net2"])
    try:
        rc = DEFAULT_RECIPES[recipe_name].to_dict()
        logs = []
        cap = run_job(pg, rc, vars_, handover=False, job_timeout_s=90, log=logs.append)
        r["driver"] = {"status": cap["status"], "reason": cap.get("reason"), "notes": cap.get("notes"), "url": cap.get("url"),
                       "text": (cap.get("text") or "")[:5000], "tables": (cap.get("tables") or [])[:6], "logs": logs}
        pg.screenshot(path=str(OUT / f"{key}_2_after_driver.png"), full_page=True)
        r["after"] = pg.evaluate(DUMP)
        (OUT / f"{key}_after.html").write_text(pg.content()[:800000])
        try:
            r["parse"] = parse(cap)
        except Exception as e:
            r["parse_error"] = repr(e)[:300]
    except Exception as e:
        r["driver_error"] = traceback.format_exc()[-800:]
        try: pg.screenshot(path=str(OUT / f"{key}_2_error.png"))
        except Exception: pass
    pg.close()

def p_status(cap):
    p = ntes.parse_status(cap, TRAIN)
    return {"confidence": p.confidence, "issues": p.issues, "stations": len(p.stations), "train": p.train_number}

with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context(**p.devices["Pixel 7"], locale="en-IN", timezone_id="Asia/Kolkata")
    R["ip"] = {}
    try:
        pg = ctx.new_page(); pg.goto("https://api.github.com/meta", timeout=20000); pg.close()
    except Exception: pass
    site(ctx, "ntes", "https://enquiry.indianrail.gov.in/mntes/", "ntes_status", {"train": TRAIN, "date": today.isoformat()}, p_status)
    time.sleep(20)
    site(ctx, "irctc", "https://www.irctc.co.in/online-charts/", "irctc_berth",
         {"train": TRAIN, "date": today.isoformat(), "station": sys.argv[2] if len(sys.argv) > 2 else "BRC", "class": "3A"},
         lambda cap: irctc.parse_vacancy(cap, sys.argv[2] if len(sys.argv) > 2 else "BRC", "3A"))
    b.close()
(OUT / "probe.json").write_text(json.dumps(R, indent=1, default=str))
for k in ("ntes", "irctc"):
    s = R.get(k, {})
    print(f"::notice title=probe {k}::http {s.get('http')} · landing {('landing' in s)} {s.get('landing_error','')[:120]} · driver {(s.get('driver') or {}).get('status')} {((s.get('driver') or {}).get('reason') or '')[:120]} · parse {json.dumps(s.get('parse', s.get('parse_error')), default=str)[:200]}")
