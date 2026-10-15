"""Emulator smoke test for the APK (run by .github/workflows/android.yml inside an Android emulator job).

Installs the release APK, starts it, waits for the in-app engine, points the app's browser assistant at the
local mock NTES/IRCTC sites (adb reverse → 127.0.0.1 inside the phone), queues checks, and records:
screenshots, the captures the app produced, Android notifications and the app's log.
Exit code 1 if the engine never starts or the in-app assistant can't read the mock running-status page.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "mock_sites"))
from mock_server import IST, make_server  # noqa: E402

PKG = "com.deepkmr0007.railvision"
OUT = ROOT / "smoke"
OUT.mkdir(exist_ok=True)
APK = ROOT / "out" / "RailVision.apk"
R: dict = {"steps": []}


def log(s):
    print(s, flush=True)
    R["steps"].append(s)


def adb(*a, timeout=180):
    p = subprocess.run(["adb", *a], capture_output=True, text=True, timeout=timeout)
    return (p.stdout + p.stderr).strip()


def shot(name):
    adb("shell", "screencap", "-p", "/sdcard/rv.png")
    adb("pull", "/sdcard/rv.png", str(OUT / f"{name}.png"))
    log(f"screenshot {name}")


def api(method, path, body=None, token=""):
    req = urllib.request.Request("http://127.0.0.1:8765" + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"{}")


def finish(ok: bool):
    full = adb("logcat", "-d", "-v", "time", timeout=60)
    (OUT / "logcat_full.txt").write_text(full)
    keep = [ln for ln in full.splitlines() if any(k in ln for k in
            ("RVEngine", "RVAgent", "python.std", "chaquopy", "AndroidRuntime", "FATAL", PKG, "[http]"))]
    (OUT / "logcat_app.txt").write_text("\n".join(keep))
    notes = adb("shell", "dumpsys", "notification", "--noredact")
    R["notifications"] = [ln.strip() for ln in notes.splitlines() if "android.title=" in ln or "android.text=" in ln][:40]
    R["ok"] = ok
    (OUT / "results.json").write_text(json.dumps(R, indent=2, default=str))
    print(json.dumps({k: v for k, v in R.items() if k != "steps"}, indent=2, default=str)[:6000])
    # one-line summaries as workflow annotations (readable through the GitHub API)
    print(f"::notice title=Engine::started in {R.get('engine_up_s')} s")
    for c in R.get("captures", []):
        print(f"::notice title=In-app assistant {c['kind']}::{c['status']} · confidence {c['confidence']} · "
              f"{(c.get('headline') or c.get('reason') or '')[:150]} · vacant {c.get('vacant')}")
    for n in R.get("notifications", [])[:6]:
        print(f"::notice title=Android notification::{n[:180]}")
    crash = [ln for ln in (OUT / "logcat_app.txt").read_text().splitlines() if "FATAL" in ln or "AndroidRuntime: E" in ln][:3]
    for ln in crash:
        print(f"::error title=Crash::{ln[:200]}")
    sys.exit(0 if ok else 1)


def main():
    mock = make_server(9000)
    log(f"mock sites on host :{mock.server_address[1]}")
    log("adb root: " + adb("root"))
    time.sleep(4)
    adb("wait-for-device")
    log("install: " + adb("install", "-r", "-g", str(APK), timeout=300))
    adb("reverse", "tcp:9000", "tcp:9000")
    adb("logcat", "-c")
    t0 = time.time()
    log("start: " + adb("shell", "am", "start", "-W", "-n", f"{PKG}/.MainActivity"))
    adb("forward", "tcp:8765", "tcp:8765")
    up = False
    for _ in range(120):
        try:
            with urllib.request.urlopen("http://127.0.0.1:8765/", timeout=5) as r:
                up = r.status == 200
                if up:
                    break
        except Exception:
            pass
        time.sleep(2)
    R["engine_up_s"] = round(time.time() - t0, 1) if up else None
    log(f"engine up: {up} after {R['engine_up_s']} s")
    time.sleep(6)
    shot("1_dashboard")
    if not up:
        finish(False)
    tok = adb("shell", "cat", f"/data/data/{PKG}/files/railvision/token.txt")
    if not tok or " " in tok or "denied" in tok.lower():
        log("token read failed: " + tok[:200])
        finish(False)

    recipes = api("GET", "/api/assist/recipes", token=tok)
    for name, path in (("ntes_status", "/ntes-a/"), ("irctc_berth", "/irctc/")):
        rc = dict(recipes[name])
        rc["start_url"] = "http://127.0.0.1:9000" + path
        api("PUT", f"/api/assist/recipes/{name}", rc, token=tok)
    log("recipes pointed at the mock sites")
    today = datetime.now(IST).date().isoformat()
    api("POST", "/api/assist/jobs", {"kind": "status", "vars": {"train": "12951", "date": today}}, token=tok)
    api("POST", "/api/assist/jobs", {"kind": "berth", "vars": {"train": "12951", "date": today, "station": "AWR", "class": "3A"}}, token=tok)
    log("queued: running status 12951 + berths 3A from AWR")

    adb("shell", "am", "start", "-n", f"{PKG}/.MainActivity", "--ez", "open_assistant", "true")
    time.sleep(12)
    shot("2_assistant_panel")
    t1 = time.time()
    jobs = []
    while time.time() - t1 < 300:
        jobs = api("GET", "/api/assist/jobs", token=tok)["items"]
        if jobs and all(j["status"] not in ("queued", "running") for j in jobs):
            break
        time.sleep(5)
    R["jobs"] = [{k: j.get(k) for k in ("kind", "status", "summary")} for j in jobs]
    R["jobs_s"] = round(time.time() - t1, 1)
    caps = api("GET", "/api/assist/captures?limit=10", token=tok)["items"]
    R["captures"] = [{"kind": c.get("kind"), "status": c.get("status"), "confidence": c.get("confidence"),
                      "headline": (c.get("parse") or {}).get("headline"), "reason": c.get("reason"),
                      "stations": (c.get("parse") or {}).get("stations_found"),
                      "vacant": ((c.get("parse") or {}).get("vacancy") or {}).get("total_vacant")} for c in caps]
    R["agent"] = api("GET", "/api/assist/status", token=tok).get("agent")
    R["mock_hits"] = dict(getattr(mock, "stats", {}) or {})
    shot("3_after_checks")
    adb("shell", "input", "keyevent", "KEYCODE_BACK")
    time.sleep(2)
    adb("shell", "am", "start", "-n", f"{PKG}/.MainActivity")
    time.sleep(3)
    shot("4_dashboard_after")
    ok = any(c["kind"] == "status" and c["status"] == "ok" and (c["confidence"] or 0) >= 0.6 for c in R["captures"])
    finish(ok)


if __name__ == "__main__":
    main()
