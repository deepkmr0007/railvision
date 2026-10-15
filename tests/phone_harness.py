"""Reference implementation of the Android app's job loop (AssistantAgent.kt) on Playwright.

The Kotlin code follows this loop step for step; the tests run it against the mock sites so the
phone driver (android/app/src/main/assets/rv_driver.js) is proven before it ships in the APK.
"""
from __future__ import annotations

import time
from pathlib import Path

DRIVER = (Path(__file__).resolve().parents[1] / "android" / "app" / "src" / "main" / "assets" / "rv_driver.js").read_text()


def run_job(page, recipe: dict, vars_: dict, handover: bool = False, job_timeout_s: float = 60, human_timeout_s: float = 30,
            on_needs_you=None, log=lambda s: None) -> dict:
    url = recipe["start_url"].replace("{url}", vars_.get("url", ""))
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    phase, after = ("capture" if recipe["kind"] == "page" else "open"), None
    deadline = time.time() + job_timeout_s
    notes = []
    for _ in range(200):
        page.add_script_tag(content=DRIVER) if not page.evaluate("() => !!(window.rvDriver && window.rvDriver.v === 3)") else None
        job = {"recipe": recipe, "vars": vars_, "phase": phase, "handover": handover, "after": after,
               "deadline": (deadline if phase != "human" else time.time() + human_timeout_s) * 1000}
        try:
            r = page.evaluate("job => rvDriver.step(job)", job)
        except Exception as e:                    # page navigated during a step: re-inject and retry the same phase
            log(f"retry {phase}: {type(e).__name__}")
            page.wait_for_load_state("domcontentloaded")
            continue
        if r.get("note"):
            notes.append(r["note"])
        st = r["status"]
        if st == "continue":
            phase = r["next"]
            if r.get("expectNav"):
                time.sleep(1.2)
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=20000)
                except Exception:
                    pass
            else:
                time.sleep(0.2)
        elif st == "waiting":
            time.sleep(1.5)
        elif st == "needs_you":
            if on_needs_you:
                on_needs_you(r["reason"])
            phase, after = "human", r.get("after")
            deadline = time.time() + human_timeout_s
        elif st == "done":
            return {"status": r["result"], "reason": r.get("note") or "", "notes": notes, **r["capture"]}
        else:
            return {"status": r.get("kind") or "recipe_failed" if st == "blocked" else "recipe_failed",
                    "reason": r.get("reason", ""), "notes": notes, "text": "", "tables": [], "links": []}
        if time.time() > deadline and phase not in ("human",):
            return {"status": "error", "reason": "job timed out", "notes": notes, "text": "", "tables": [], "links": []}
    return {"status": "error", "reason": "too many steps", "notes": notes, "text": "", "tables": [], "links": []}
