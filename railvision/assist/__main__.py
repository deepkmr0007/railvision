"""Browser assistant command line.

  python -m railvision.assist agent --server http://127.0.0.1:8000 --token rv_...      # the loop
  python -m railvision.assist teach ntes_status --train 12951 --date 2026-10-15         # record steps once
  python -m railvision.assist teach irctc_berth --train 12951 --date 2026-10-15 --station NDLS --cls 3A
  python -m railvision.assist teach ntes_search --query rajdhani
  python -m railvision.assist run ntes_status --train 12951 --date 2026-10-15           # one check, print result

Needs:  pip install playwright   then   python -m playwright install chromium
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path

from .agent import Agent, Client, launch_context
from .recipes import DEFAULT_RECIPES, Recipe

DATA = Path(os.environ.get("RAILVISION_ASSIST_DIR", "assist_data"))


def _pw():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("Playwright is not installed. Run:  pip install playwright  &&  python -m playwright install chromium")
    return sync_playwright


def _vars(a) -> dict:
    v = {}
    for k in ("train", "date", "station", "query"):
        if getattr(a, k, None):
            v[k] = getattr(a, k)
    if getattr(a, "cls", None):
        v["class"] = a.cls
    return v


def _recipe(name: str) -> Recipe:
    f = DATA / "recipes" / f"{name}.json"
    if f.exists():
        return Recipe.load(f)
    if name in DEFAULT_RECIPES:
        return DEFAULT_RECIPES[name]
    sys.exit(f"Unknown recipe {name}. Built-in: {', '.join(DEFAULT_RECIPES)}")


def cmd_teach(a):
    from .teach import teach
    base = DEFAULT_RECIPES.get(a.recipe)
    kind = a.kind or (base.kind if base else "status")
    url = a.url or (base.start_url if base else None)
    if not url:
        sys.exit("--url is required for a custom recipe")
    with _pw()() as pw:
        ctx = launch_context(pw, DATA / "profile", headed=True)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        rc = teach(page, a.recipe, kind, url, _vars(a))
        ctx.close()
    (DATA / "recipes").mkdir(parents=True, exist_ok=True)
    rc.save(DATA / "recipes" / f"{a.recipe}.json")
    print(f"Saved {len(rc.steps)} steps to {DATA / 'recipes' / (a.recipe + '.json')}")
    for i, s in enumerate(rc.steps, 1):
        print(f"  {i}. {s.action} {s.text or s.selector or ''} {s.value or ''}")
    if a.server and a.token:
        Client(a.server, a.token).call("PUT", f"/api/assist/recipes/{a.recipe}", rc.to_dict())
        print("Uploaded to the RAILVISION server.")


def cmd_run(a):
    from ..parsers import irctc, ntes
    from .browser import Runner
    rc = _recipe(a.recipe)
    with _pw()() as pw:
        ctx = launch_context(pw, DATA / "profile", headed=not a.headless)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        cap = Runner(page, handover=not a.headless).run(rc, _vars(a))
        ctx.close()
    out = DATA / "captures"
    out.mkdir(parents=True, exist_ok=True)
    stem = f"{a.recipe}-{(a.train or a.query or 'x')}-{cap['finished_at'][:19].replace(':', '')}"
    if cap.get("screenshot_b64"):
        (out / f"{stem}.jpg").write_bytes(base64.b64decode(cap["screenshot_b64"]))
    (out / f"{stem}.json").write_text(json.dumps({k: v for k, v in cap.items() if k != "screenshot_b64"}, ensure_ascii=False, indent=1))
    print(f"status: {cap['status']} {cap['reason']}\ncapture saved: {out / stem}.json (+ .jpg) — share it to calibrate the parser")
    if cap["status"] != "ok":
        return
    if rc.kind == "status":
        p = ntes.parse_status(cap, a.train)
        print(f"read confidence {p.confidence:.0%} · {len(p.stations)} stations · last updated {p.last_updated} · {p.headline}")
        for s in p.stations[:60]:
            print(f"  {s.code:6} {s.name[:22]:22} sch {s.sch_arr or '--'}/{s.sch_dep or '--'}  act {s.act_arr or '--'}/{s.act_dep or '--'}"
                  f"  {'reached' if s.reached else 'not reached' if s.reached is False else ''} {'' if s.halt else '(non-stop)'}")
        for i in p.issues:
            print("  ! " + i)
    elif rc.kind == "berth":
        v = irctc.parse_vacancy(cap, a.station, a.cls)
        print(json.dumps({k: v[k] for k in ("total_vacant", "from_boarding", "by_coach", "classes", "chart_prepared", "confidence", "issues")}, indent=1))
    else:
        print(json.dumps(irctc.parse_search(cap, a.query or ""), indent=1, ensure_ascii=False)[:3000])


def cmd_agent(a):
    token = a.token or os.environ.get("RAILVISION_TOKEN") or ((DATA / "token.txt").read_text().strip() if (DATA / "token.txt").exists() else "")
    if not token:
        sys.exit("No token. Pass --token, set RAILVISION_TOKEN, or start with  python -m railvision.launch")
    with _pw()() as pw:
        ctx = launch_context(pw, DATA / "profile", headed=not a.headless)
        try:
            Agent(Client(a.server, token), ctx, headed=not a.headless, recipes_dir=DATA / "recipes").loop()
        except KeyboardInterrupt:
            print("stopped")
        finally:
            ctx.close()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m railvision.assist", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("teach", "run"):
        s = sub.add_parser(name)
        s.add_argument("recipe", help="ntes_status | ntes_search | irctc_berth | your own name")
        s.add_argument("--train"), s.add_argument("--date", help="train START date YYYY-MM-DD")
        s.add_argument("--station"), s.add_argument("--cls", help="class e.g. 3A"), s.add_argument("--query")
        s.add_argument("--url"), s.add_argument("--kind", choices=["status", "berth", "search"])
        s.add_argument("--server"), s.add_argument("--token"), s.add_argument("--headless", action="store_true")
    g = sub.add_parser("agent")
    g.add_argument("--server", default="http://127.0.0.1:8000"), g.add_argument("--token"), g.add_argument("--headless", action="store_true")
    a = ap.parse_args(argv)
    {"teach": cmd_teach, "run": cmd_run, "agent": cmd_agent}[a.cmd](a)


if __name__ == "__main__":
    main()
