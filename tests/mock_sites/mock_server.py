"""LOCAL MOCK of railway enquiry pages — for automated tests only.

Every page says "MOCK" and all data is synthetic. Two deliberately different layouts check that
the assistant is not tied to one page structure:
  /ntes-a/   JS app: menu → "Spot Your Train" → autocomplete → start-date chips → "Get Status";
             results fetched as JSON and rendered as labelled text blocks (no table).
  /ntes-b/   classic form: <select> start date + "Submit"; server-rendered results TABLE.
             ?mode=captcha shows a CAPTCHA after submit; ?mode=blocked returns "Access Denied".
  /irctc/    chart form (Train / Journey Date / Boarding Station / "Get Train Chart") → class buttons
             → vacant-berth table.
"""
from __future__ import annotations

import hashlib
import json
import random
import threading
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

IST = timezone(timedelta(hours=5, minutes=30))
# code, name, km, arr_offset, dep_offset, halt
BASE = [("JP", "JAIPUR JN", 0, None, 0, True), ("GADJ", "GANDHINAGAR JAIPUR", 5, 8, 8, False),
        ("BKI", "BANDIKUI JN", 91, 75, 77, True), ("AWR", "ALWAR", 150, 125, 128, True),
        ("RE", "REWARI", 225, 185, 185, False), ("GGN", "GURUGRAM", 277, 228, 230, True),
        ("DEC", "DELHI CANTT", 290, 245, 245, False), ("DLI", "DELHI JN", 305, 270, None, True)]
TRAINS = {f"{12900 + i * 7}": f"MOCK EXPRESS {i}" for i in range(1, 13)}
TRAINS["12951"] = "MOCK RAJDHANI EXPRESS"


def _rng(*a):
    return random.Random(int(hashlib.sha256("|".join(map(str, a)).encode()).hexdigest()[:10], 16))


class MockData:
    def __init__(self, now=lambda: datetime.now(IST)):
        self.now = now

    def run(self, train: str, start: date) -> dict:
        idx = sorted(TRAINS).index(train) if train in TRAINS else 0
        now = self.now()
        dep0 = now.replace(second=0, microsecond=0) - timedelta(minutes=30 + idx * 22)
        if dep0.date() != start:                    # keep start date = today for the scenario
            dep0 = datetime.combine(start, dep0.timetz())
        r = _rng(train, start)
        delay, rows = r.randint(0, 6), []
        cur = None
        for code, name, km, a, d, halt in BASE:
            sa = dep0 + timedelta(minutes=a) if a is not None else None
            sd = dep0 + timedelta(minutes=d) if d is not None else None
            delay = max(0, delay + r.randint(-1, 4))
            aa = sa + timedelta(minutes=delay) if sa else None
            ad = sd + timedelta(minutes=delay) if sd else None
            arrived = aa is not None and aa <= now
            departed = ad is not None and ad <= now
            if code == "JP":
                arrived = departed
            if arrived or departed:
                cur = (code, name, departed, ad if departed else aa)
            rows.append({"stnCode": code, "stnName": name, "distance": km, "isStoppage": halt, "dayCnt": 1 + ((sa or sd).date() - start).days,
                         "schArrTime": sa.strftime("%H:%M") if sa else "SRC", "schDepTime": sd.strftime("%H:%M") if sd else "DSTN",
                         "actArrTime": aa.strftime("%H:%M") if aa else "SRC", "actDepTime": ad.strftime("%H:%M") if ad else "DSTN",
                         "delayArr": delay, "pfNo": (r.randint(1, 6) if halt else 0), "hasArrived": arrived, "hasDeparted": departed})
        upd = now - timedelta(minutes=2)
        return {"trainNo": train, "trainName": TRAINS.get(train, "MOCK TRAIN"), "startDate": start.isoformat(),
                "lastUpdated": upd.strftime("%d-%b-%Y %H:%M"), "current": cur and {"code": cur[0], "name": cur[1], "departed": cur[2],
                                                                                   "time": cur[3].strftime("%H:%M")},
                "stations": rows}

    def berths(self, train, jdate, frm, cls):
        r = _rng("b", train, jdate, frm, cls)
        prefix = {"3A": "B", "SL": "S", "2A": "A"}.get(cls, "B")
        halts = [b[0] for b in BASE if b[5]]
        i0 = halts.index(frm) if frm in halts else 0
        out = []
        for c in range(1, 4):
            for b in sorted(r.sample(range(1, 65), r.randint(1, 5))):
                j = r.randint(i0 + 1, len(halts) - 1)
                out.append({"coach": f"{prefix}{c}", "berth": b, "type": ["LB", "MB", "UB", "LB", "MB", "UB", "SL", "SU"][(b - 1) % 8],
                            "from": halts[r.randint(i0, j - 1)], "to": halts[j]})
        return out


PAGE_A = """<!doctype html><html><head><meta charset="utf-8"><title>MOCK National Train Enquiry</title></head><body>
<h3>MOCK National Train Enquiry System (test page — not the real site)</h3>
<nav><a href="#" id="spot">Spot Your Train</a> · <a href="#" class="ex" data-l="cancelled">Cancelled Trains</a> · <a href="#" class="ex" data-l="diverted">Diverted Trains</a> · <a href="#" class="ex" data-l="rescheduled">Rescheduled Trains</a></nav><div id="exl"></div>
<div id="form" style="display:none"><label for="trn">Train Number / Name</label> <input id="trn" placeholder="Enter Train No. or Name" autocomplete="off">
<ul id="ac"></ul><div id="dates"></div><button id="go" style="display:none">Get Status</button></div><div id="res"></div>
<script>
const T=%TRAINS%; let chosen=null, day=null;
document.getElementById('spot').onclick=e=>{e.preventDefault();document.getElementById('form').style.display='block'};
document.querySelectorAll('.ex').forEach(a=>a.onclick=async e=>{e.preventDefault();const r=await fetch('/ntes-a/api/list?l='+a.dataset.l);const j=await r.json();
  document.getElementById('exl').innerHTML='<h4>'+a.textContent+'</h4><table border=1><tr><th>Train</th><th>Name</th><th>Start Date</th><th>Details</th></tr>'+j.map(x=>'<tr><td>'+x.no+'</td><td>'+x.name+'</td><td>'+x.date+'</td><td>'+x.detail+'</td></tr>').join('')+'</table>'});
const trn=document.getElementById('trn');
trn.addEventListener('input',()=>{const q=trn.value.trim().toLowerCase();const ac=document.getElementById('ac');ac.innerHTML='';
  if(q.length<3)return;Object.entries(T).filter(([n,nm])=>n.startsWith(q)||nm.toLowerCase().includes(q)).slice(0,8).forEach(([n,nm])=>{
   const li=document.createElement('li');li.textContent=n+' - '+nm;li.style.cursor='pointer';li.onclick=()=>{chosen=n;trn.value=n+' - '+nm;ac.innerHTML='';showDates()};ac.appendChild(li)})});
function fmt(d){return d.toLocaleDateString('en-GB',{day:'2-digit',month:'short',year:'numeric',timeZone:'Asia/Kolkata'}).replace(/ /g,'-')}
function showDates(){const ds=document.getElementById('dates');ds.innerHTML='<p>Select Start Date</p>';const now=new Date();
  for(let k=-1;k<=1;k++){const d=new Date(now.getTime()+k*864e5);const b=document.createElement('span');b.textContent=fmt(d);b.style.cssText='display:inline-block;border:1px solid #888;padding:4px;margin:4px;cursor:pointer';
   b.onclick=()=>{day=d.toLocaleDateString('en-CA',{timeZone:'Asia/Kolkata'});b.style.background='#cde'};ds.appendChild(b)}
  document.getElementById('go').style.display='inline-block'}
document.getElementById('go').onclick=async()=>{if(!chosen||!day){alert('select');return}
  const r=await fetch('/ntes-a/api/run?train='+chosen+'&date='+day);const j=await r.json();const el=document.getElementById('res');
  let h='<h4>'+j.trainNo+' '+j.trainName+'</h4><p>Last Updated: '+j.lastUpdated+'</p>';
  if(j.current)h+='<p>'+(j.current.departed?'Departed from ':'Arrived at ')+j.current.name+' ('+j.current.code+') at '+j.current.time+'</p>';
  for(const s of j.stations){const ex=!(s.hasArrived||s.hasDeparted);
   h+='<div class="stn"><div>'+s.stnName+' ('+s.stnCode+')</div><div>Sch Arr: '+s.schArrTime+' &nbsp; '+(ex?'Exp':'Act')+' Arr: '+s.actArrTime+'</div><div>Sch Dep: '+s.schDepTime+' &nbsp; '+(ex?'Exp':'Act')+' Dep: '+s.actDepTime+'</div><div>Delay: '+s.delayArr+' min'+(s.pfNo?' &nbsp; PF: '+s.pfNo:'')+' &nbsp; '+s.distance+' km</div><div>'+(s.hasDeparted?'Departed':s.hasArrived?'Arrived':'Not Arrived')+(s.isStoppage?'':' (No Halt)')+'</div></div>'}
  el.innerHTML=h};
</script></body></html>"""

PAGE_B_FORM = """<!doctype html><html><head><meta charset="utf-8"><title>MOCK Train Running Status B</title></head><body>
<h2>MOCK running status (layout B, test page)</h2><form method="get" action="/ntes-b/result">
<p><label>Train No <input name="trainNo" placeholder="Train Number"></label></p>
<p><label>Start Date <select name="startDate">%OPTS%</select></label></p><input type="hidden" name="mode" value="%MODE%">
<p><button type="submit">Submit</button></p></form></body></html>"""

PAGE_PNR = """<!doctype html><html><head><meta charset="utf-8"><title>MOCK PNR Enquiry</title></head><body>
<h2>MOCK PNR status (test page)</h2><form id="f" method="get" action="/pnr/result">
<p><label>PNR No. <input name="pnr" id="pnr" placeholder="Enter 10 digit PNR"></label></p>
<p><img id="captchaImg" src="data:image/gif;base64,R0lGODlhAQABAAAAACw=" width="100" height="30" alt="captcha"> What is 3 + 4? <input name="captcha" id="captcha"></p>
<p><button type="submit">Get Status</button></p></form>
<script>if(%AUTO%){setTimeout(()=>{if(document.getElementById('pnr').value.length===10){document.getElementById('captcha').value='7';document.getElementById('f').submit()}},1500);
 setInterval(()=>{if(document.getElementById('pnr').value.length===10&&!document.getElementById('captcha').value){document.getElementById('captcha').value='7';document.getElementById('f').submit()}},1000)}</script>
</body></html>"""

PAGE_IRCTC = """<!doctype html><html><head><meta charset="utf-8"><title>MOCK Online Charts</title></head><body>
<h2>MOCK Reservation Chart / Vacant Berth (test page)</h2>
<div><label for="t">Train Name/Number</label><input id="t" placeholder="Train Name/Number"></div>
<div><label for="jd">Journey Date</label><input id="jd" type="date"></div>
<div><label for="bs">Boarding Station</label><input id="bs" placeholder="Boarding Station"></div>
<button id="gc">Get Train Chart</button><div id="cls"></div><div id="tab"></div>
<script>
document.getElementById('gc').onclick=async()=>{const t=document.getElementById('t').value.trim(),d=document.getElementById('jd').value,b=document.getElementById('bs').value.trim().toUpperCase();
 const r=await fetch('/irctc/api/summary?train='+t+'&date='+d+'&from='+b);const j=await r.json();const c=document.getElementById('cls');
 c.innerHTML='<p>Train '+t+' · Chart: First Chart prepared</p>';for(const [k,n] of Object.entries(j.classes)){const x=document.createElement('button');x.textContent=k;x.onclick=async()=>{
  const rr=await fetch('/irctc/api/berths?train='+t+'&date='+d+'&from='+b+'&cls='+k);const jj=await rr.json();
  document.getElementById('tab').innerHTML='<table><tr><th>Coach</th><th>Berth No</th><th>Berth Type</th><th>From</th><th>To</th></tr>'+jj.map(e=>'<tr><td>'+e.coach+'</td><td>'+e.berth+'</td><td>'+e.type+'</td><td>'+e.from+'</td><td>'+e.to+'</td></tr>').join('')+'</table>'};
  c.appendChild(x);c.appendChild(document.createTextNode(' '+k+' '+n+' vacant  '))}};
</script></body></html>"""


MENU = "".join(f'<li><a href="/zone/view_section.jsp?lang=0&id=0,{i}">{t}</a></li>' for i, t in enumerate(
    ["About Us", "Divisions", "Tenders and Recruitment", "Passenger Services", "Freight Services", "Right to Information",
     "Vigilance", "Contact Us", "Sitemap", "Screen Reader Access"]))


def zone_list(n_items: int) -> str:
    items = [("view_detail.jsp?lang=0&id=0,4,268&dcd=101", "Commissioning of new foot over bridge at Bandikui station"),
             ("uploads/files/notice102.pdf", "Cancellation / partial cancellation of trains due to traffic block"),
             ("view_detail.jsp?lang=0&id=0,4,268&dcd=103", "Extension of special trains for the festival season")][:n_items]
    rows = "".join(f'<tr><td>15.10.2026</td><td><a href="/zone/{h}">{t}</a></td></tr>' for h, t in items)
    return (f'<html><head><title>Press Releases - MOCK Zonal Railway</title></head><body><nav><ul class="menu">{MENU}</ul></nav>'
            f'<h2>Press Releases (MOCK zonal portal, test page)</h2><table>{rows}</table><footer><a href="/zone/disclaimer">Disclaimer and Copyright Policy</a></footer></body></html>')


ZONE_DETAIL = {
    "101": "<html><body><nav><ul class='menu'>" + MENU + "</ul></nav><h3>Commissioning of new foot over bridge at Bandikui station</h3>"
           "<p>A new foot over bridge connecting platforms 1 to 3 at Bandikui (BKI) has been opened for passengers.</p></body></html>",
    "103": "<html><body><nav><ul class='menu'>" + MENU + "</ul></nav><h3>Extension of special trains for the festival season</h3>"
           "<p>Train No. 04815/04816 Jaipur - Delhi Sarai Rohilla festival special extended till 30.11.2026.</p></body></html>",
}


def make_server(port=0, now=lambda: datetime.now(IST)):
    data = MockData(now)
    stats = {"hits": 0}
    from pdfmini import make_pdf
    zone_state = {"items": 1}
    pdf = make_pdf(["North Western Railway - Press Release (MOCK)", "Due to a traffic block between Rewari and Alwar,",
                    "Train No. 12951 will remain cancelled on 20.10.2026", "and 12956 will be short terminated at Alwar."])

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, code, body, ctype="text/html; charset=utf-8"):
            b = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            stats["hits"] += 1
            u = urlsplit(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if u.path == "/ntes-a/":
                return self.send(200, PAGE_A.replace("%TRAINS%", json.dumps(TRAINS)))
            if u.path == "/ntes-a/api/run":
                return self.send(200, json.dumps(data.run(q["train"], date.fromisoformat(q["date"]))), "application/json")
            if u.path == "/ntes-a/api/list":
                today = now().date()
                lst = {"cancelled": [("12956", "MOCK EXPRESS 8", "Fully cancelled"), ("14311", "MOCK ALA HAZRAT", "Fully cancelled")],
                       "diverted": [("12963", "MOCK EXPRESS 9", "Diverted via AWR-DVX1-DEC (skips RE, GGN)")],
                       "rescheduled": [("12415", "MOCK INTERCITY", "Rescheduled by 01:30 hrs, new departure 18:10")]}[q.get("l", "cancelled")]
                return self.send(200, json.dumps([{"no": n, "name": nm, "date": today.strftime("%d-%b-%Y"), "detail": d} for n, nm, d in lst]),
                                 "application/json")
            if u.path == "/pnr/":
                return self.send(200, PAGE_PNR.replace("%AUTO%", "1" if q.get("autosolve") else "0"))
            if u.path == "/pnr/result":
                if q.get("captcha") != "7":
                    return self.send(200, "<html><body><p>Invalid captcha. <a href='/pnr/'>Try again</a></p></body></html>")
                p = q.get("pnr", "")
                return self.send(200, f"<html><body><h3>PNR {p}</h3><p>12951 - MOCK RAJDHANI EXPRESS</p><p>Date of Journey: {now().strftime('%d-%b-%Y')}"
                                      " · From: NDLS To: MMCT · Class: 3A</p><p>Chart Status: Chart Not Prepared</p>"
                                      "<table border=1><tr><th>S.No</th><th>Booking Status</th><th>Current Status</th></tr>"
                                      "<tr><td>Passenger 1</td><td>WL/12</td><td>RAC/4</td></tr><tr><td>Passenger 2</td><td>CNF/B3/45/LB</td>"
                                      "<td>CNF/B3/45/LB</td></tr></table></body></html>")
            if u.path == "/zone/view_section.jsp":
                return self.send(200, zone_list(zone_state["items"]))
            if u.path == "/zone/view_detail.jsp":
                return self.send(200, ZONE_DETAIL.get(q.get("dcd", ""), "<html><body>not found</body></html>"))
            if u.path == "/zone/uploads/files/notice102.pdf":
                return self.send(200, pdf, "application/pdf")
            if u.path == "/ntes-b/":
                if q.get("mode") == "blocked":
                    return self.send(403, "<html><body><h1>Access Denied</h1><p>You don't have permission to access this resource.</p></body></html>")
                today = now().date()
                opts = "".join(f"<option value='{(today + timedelta(days=k)).isoformat()}'>{(today + timedelta(days=k)).strftime('%d %b %Y')}</option>"
                               for k in (-1, 0, 1))
                return self.send(200, PAGE_B_FORM.replace("%OPTS%", opts).replace("%MODE%", q.get("mode", "")))
            if u.path == "/ntes-b/result":
                if q.get("mode") == "captcha":
                    return self.send(200, "<html><body><h3>Security check</h3><img id='captchaImg' src='data:image/gif;base64,R0lGODlhAQABAAAAACw=' width=120 height=40>"
                                          "<p>Enter the characters shown</p><input name='captcha'></body></html>")
                j = data.run(q["trainNo"], date.fromisoformat(q["startDate"]))
                rows = "".join(f"<tr><td>{s['stnName']} ({s['stnCode']})</td><td>{s['schArrTime']} / {s['actArrTime'] if s['hasArrived'] else '--'}</td>"
                               f"<td>{s['schDepTime']} / {s['actDepTime'] if s['hasDeparted'] else '--'}</td><td>{s['delayArr']} min</td>"
                               f"<td>{s['pfNo'] or '-'}</td><td>{s['distance']} km</td><td>{'Departed' if s['hasDeparted'] else 'Arrived' if s['hasArrived'] else 'Yet to arrive'}"
                               f"{'' if s['isStoppage'] else ' (No halt)'}</td></tr>" for s in j["stations"])
                cur = j["current"]
                pos = f"<p>{'Departed from' if cur['departed'] else 'Arrived at'} {cur['name']} ({cur['code']}) at {cur['time']}</p>" if cur else "<p>Yet to start</p>"
                return self.send(200, f"<html><body><h3>{j['trainNo']} - {j['trainName']}</h3><p>Last updated on {j['lastUpdated']}</p>{pos}"
                                      "<table border=1><tr><th>Station</th><th>Arr (Sch/Act)</th><th>Dep (Sch/Act)</th><th>Delay</th><th>PF</th><th>Km</th><th>Status</th></tr>"
                                      f"{rows}</table></body></html>")
            if u.path == "/irctc/":
                return self.send(200, PAGE_IRCTC)
            if u.path == "/irctc/api/summary":
                classes = {c: len(data.berths(q["train"], q["date"], q["from"], c)) for c in ("3A", "SL")}
                return self.send(200, json.dumps({"classes": classes}), "application/json")
            if u.path == "/irctc/api/berths":
                return self.send(200, json.dumps(data.berths(q["train"], q["date"], q["from"], q["cls"])), "application/json")
            return self.send(404, "not found")

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    srv.data, srv.stats, srv.zone_state = data, stats, zone_state
    return srv


if __name__ == "__main__":
    s = make_server(8099)
    print("mock on http://127.0.0.1:8099/ntes-a/  /ntes-b/  /irctc/")
    threading.Event().wait()
