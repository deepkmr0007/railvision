/* RAILVISION phone browser driver (Android WebView).
 * Same rules as the desktop assistant: one page at a time, reads only what the page shows,
 * NEVER solves CAPTCHAs / logins / bot checks — it stops and asks the person (or reports "blocked").
 *
 * Protocol: the app calls  rvDriver.step(job)  -> Promise<result>
 *   job    = {recipe, vars, phase, handover, deadline, after}
 *   result = {status:"continue", next, expectNav?, note?} | {status:"waiting", note?}
 *          | {status:"needs_you", reason, next, after?} | {status:"done", result:"ok"|"no_result", capture}
 *          | {status:"blocked", kind, reason} | {status:"failed", reason}
 * Clicks that may load a new page are scheduled 60 ms AFTER the result is returned (expectNav:true),
 * so a navigation never cuts a step in half. The app re-injects this file after every page load.
 */
(function () {
  if (window.rvDriver && window.rvDriver.v === 3) return;
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const vis = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length)) &&
    getComputedStyle(el).visibility !== 'hidden' && !el.disabled;
  const bodyText = () => (document.body ? document.body.innerText : '');
  const TIME = /\b([01]?\d|2[0-3]):[0-5]\d\b/;
  const MON = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  const MONL = ['January','February','March','April','May','June','July','August','September','October','November','December'];
  const DAY = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
  const FORMATS = ['%d-%b-%Y','%d-%m-%Y','%d/%m/%Y','%d %b %Y','%d-%b-%y','%d %b','%d-%b','%Y-%m-%d','%a, %d %b','%d %B %Y','%d.%m.%Y','%d%m%Y'];
  const SUBMIT = ['get train chart','get chart','get status','check status','show status','get','submit','search','show','find','check','go','proceed','view'];

  function pad(n) { return (n < 10 ? '0' : '') + n; }
  function parseDate(s) { const [y, m, d] = String(s).split('-').map(Number); return {y, m, d, dow: new Date(Date.UTC(y, m - 1, d)).getUTCDay()}; }
  function fmt(dt, f) {
    return f.replace(/%-d/g, String(dt.d)).replace(/%-m/g, String(dt.m)).replace(/%d/g, pad(dt.d)).replace(/%m/g, pad(dt.m))
      .replace(/%Y/g, String(dt.y)).replace(/%y/g, String(dt.y).slice(2)).replace(/%b/g, MON[dt.m - 1])
      .replace(/%B/g, MONL[dt.m - 1]).replace(/%a/g, DAY[dt.dow]);
  }
  function dateVariants(s) {
    const dt = parseDate(s), out = [];
    FORMATS.forEach(f => { const v = fmt(dt, f); out.push(v); const np = v.replace(/\b0(\d)/g, '$1'); if (np !== v) out.push(np); });
    return [...new Set(out)];
  }
  function render(t, vars) {
    if (t == null) return t;
    return String(t).replace(/\{(train|query|station|class|date|pnr|url)(?::([^}]*))?\}/g, (m, k, f) => {
      const v = vars[k]; if (v == null) return m;
      return k === 'date' ? fmt(parseDate(v), f || '%d-%m-%Y') : String(v);
    });
  }

  function detect() {
    const t = bodyText().slice(0, 30000).toLowerCase();
    const capEl = [...document.querySelectorAll('iframe[src*="recaptcha"],iframe[src*="hcaptcha"],iframe[src*="turnstile"],iframe[title*="challenge" i],img[src*="captcha" i],img[id*="captcha" i],img[alt*="captcha" i],input[name*="captcha" i],input[id*="captcha" i],input[placeholder*="captcha" i]')].some(vis);
    const capTxt = /enter (the )?(characters|text) (shown|you see)|verify (that )?you are (a )?human|i'm not a robot|security check|solve the puzzle/.test(t);
    const blocked = /access denied|request (was )?blocked|you don't have permission|unusual traffic|too many requests|error 403|forbidden|rate limit/.test(t) && t.length < 4000;
    const login = [...document.querySelectorAll('input[type=password]')].some(vis);
    return capEl || capTxt ? 'captcha' : login ? 'login' : blocked ? 'blocked' : null;
  }
  const BLOCK_MSG = {captcha: 'The site is showing a CAPTCHA / human check', login: 'The site is asking for a login', blocked: 'The site refused the request'};

  function label(el) {
    const parts = [el.id, el.name, el.getAttribute('placeholder'), el.getAttribute('aria-label'), el.title, el.getAttribute('formcontrolname')];
    if (el.id) { const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]'); if (l) parts.push(l.innerText); }
    const cl = el.closest('label'); if (cl) parts.push(cl.innerText);
    const p = el.previousElementSibling; if (p && p.innerText && p.innerText.length < 60) parts.push(p.innerText);
    const par = el.parentElement; if (par && par.innerText && par.innerText.length < 80) parts.push(par.innerText);
    return parts.filter(Boolean).join(' | ').toLowerCase().slice(0, 300);
  }
  function scan() {
    const out = [];
    document.querySelectorAll('input,select,textarea,button,a,[role=button],[role=option],li,td,span,label,div').forEach(el => {
      const tag = el.tagName.toLowerCase();
      const isField = ['input', 'select', 'textarea'].includes(tag);
      const isAct = ['button', 'a'].includes(tag) || el.getAttribute('role') === 'button' || (tag === 'input' && ['submit', 'button'].includes(el.type));
      const txt = (el.innerText || el.value || '').trim();
      const leafy = !isField && !isAct && txt && txt.length <= 40 && el.children.length === 0;
      if (!(isField || isAct || leafy) || !vis(el) || el.closest('[data-rv-overlay]')) return;
      out.push({el, tag, type: (el.type || '').toLowerCase(), label: label(el), text: txt.slice(0, 80), field: isField, act: isAct,
                options: tag === 'select' ? [...el.options].map(o => o.text.trim()) : null, order: out.length});
    });
    return out;
  }
  function score(lbl, want, avoid) {
    let s = 0; want.forEach((w, i) => { if (lbl.includes(w)) s += 10 - i; });
    (avoid || []).forEach(a => { if (lbl.includes(a)) s -= 8; }); return s;
  }
  function field(items, want, avoid, types) {
    types = types || ['text', 'search', 'number', 'tel', ''];
    let best = null, bs = 0;
    items.forEach(i => {
      if (!i.field || !['input', 'textarea'].includes(i.tag) || !types.includes(i.type)) return;
      const s = score(i.label, want, avoid);
      if (s > bs || (s === bs && best && i.order < best.order && s > 0)) { best = i; bs = s; }
    });
    return bs > 0 ? best : null;
  }
  const trainField = items => field(items, ['train no', 'train number', 'train name', 'train', 'gaadi'], ['station', 'from', 'to']);

  function setNative(el, v) {
    const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const d = Object.getOwnPropertyDescriptor(proto, 'value'); d && d.set ? d.set.call(el, v) : (el.value = v);
  }
  async function typeInto(el, text) {
    el.scrollIntoView({block: 'center'}); el.focus();
    setNative(el, ''); el.dispatchEvent(new Event('input', {bubbles: true}));
    let n = 0;                      // few chained timers per step: an off-screen WebView may throttle them
    for (const ch of String(text)) {
      const ko = {key: ch, bubbles: true};
      el.dispatchEvent(new KeyboardEvent('keydown', ko)); el.dispatchEvent(new KeyboardEvent('keypress', ko));
      setNative(el, el.value + ch);
      el.dispatchEvent(new InputEvent('input', {bubbles: true, data: ch, inputType: 'insertText'}));
      el.dispatchEvent(new KeyboardEvent('keyup', ko));
      if (++n % 3 === 0) await sleep(60);
    }
    await sleep(60);
  }
  function clickEl(el) {
    try { el.scrollIntoView({block: 'center'}); } catch (e) {}
    ['pointerdown', 'mousedown', 'pointerup', 'mouseup'].forEach(t => el.dispatchEvent(new MouseEvent(t, {bubbles: true, cancelable: true, view: window})));
    el.click();
  }
  const later = el => setTimeout(() => clickEl(el), 60);
  function clickText(texts, exact) {
    const low = texts.filter(Boolean).map(t => t.toLowerCase());
    const it = scan().find(i => { const tx = i.text.toLowerCase(); return low.some(t => exact ? tx === t : (tx === t || tx.startsWith(t))); });
    return it ? it.el : null;
  }
  function selectByLabel(sel, labelText) {
    const o = [...sel.options].find(o => o.text.trim() === labelText || o.value === labelText);
    if (!o) return false;
    sel.value = o.value; sel.dispatchEvent(new Event('input', {bubbles: true})); sel.dispatchEvent(new Event('change', {bubbles: true}));
    return true;
  }
  function setDate(d) {
    const variants = dateVariants(d).map(v => v.toLowerCase());
    const items = scan();
    for (const it of items) if (it.tag === 'select' && it.options)
      for (const o of it.options) if (variants.some(v => o.toLowerCase().startsWith(v) || o.toLowerCase().includes(v))) {
        selectByLabel(it.el, o); return `start date '${o}' (dropdown)`; }
    for (const it of items) { const tx = it.text.toLowerCase();
      if (!it.field && tx && variants.some(v => tx === v || tx.startsWith(v))) { clickEl(it.el); return `start date '${it.text}' (clicked)`; } }
    for (const it of items) if (it.tag === 'input' && (it.type === 'date' || score(it.label, ['start date', 'journey date', 'date', 'doj'], []) > 0)) {
      const dt = parseDate(d);
      if (it.type === 'date') { setNative(it.el, d); }
      else { const ph = it.label; const f = ph.includes('dd/mm/yyyy') ? '%d/%m/%Y' : ph.includes('yyyy-mm-dd') ? '%Y-%m-%d' : ph.includes('mmm') ? '%d-%b-%Y' : '%d-%m-%Y';
             setNative(it.el, fmt(dt, f)); }
      it.el.dispatchEvent(new Event('input', {bubbles: true})); it.el.dispatchEvent(new Event('change', {bubbles: true}));
      return `date ${d} (input)`;
    }
    return 'no date control found — using the site default';
  }
  function submitButton(nearOrder) {
    let best = null, bs = -1;
    scan().filter(i => i.act).forEach(i => {
      const t = (i.text || i.label).toLowerCase().trim();
      for (let k = 0; k < SUBMIT.length; k++) { const w = SUBMIT[k];
        if (t === w || t.startsWith(w + ' ') || (w.length > 4 && t.includes(w))) {
          const s = 100 - k * 5 - Math.abs(i.order - nearOrder) * 0.01 + (i.order > nearOrder ? 5 : 0);
          if (s > bs) { best = i; bs = s; } break; } }
    });
    return best;
  }
  function resultsReady(recipe, vars) {
    const txt = bodyText();
    if (recipe.kind === 'list') return /(^|\D)\d{5}(\D|$)/.test(txt) && txt.length > 80;
    const wants = (recipe.result_texts || []).map(t => render(t, vars));
    return (!wants.length || wants.every(w => w && txt.includes(w))) && (TIME.test(txt) || recipe.kind !== 'status') && txt.length > 80;
  }
  function capture() {
    const tables = [...document.querySelectorAll('table')].filter(vis).slice(0, 25).map(tb =>
      [...tb.rows].slice(0, 500).map(r => [...r.cells].slice(0, 16).map(c => c.innerText.trim().replace(/\s+/g, ' '))));
    const links = [...document.querySelectorAll('a[href]')].filter(vis).slice(0, 800).map(a => ({t: (a.innerText || a.title || '').trim().slice(0, 300), h: a.href}));
    return {text: bodyText().slice(0, 200000), tables, links, title: document.title, url: location.href};
  }
  const done = (result, note) => ({status: 'done', result, capture: capture(), note});
  const next = (n, extra) => Object.assign({status: 'continue', next: n}, extra || {});
  function blocker(job, here) {
    const k = detect(); if (!k) return null;
    const reason = `${BLOCK_MSG[k]} (${here}).`;
    if (job.handover) return {status: 'needs_you', reason: reason + ' Please handle it in the assistant browser.', next: 'human', after: job.phase};
    return {status: 'blocked', kind: k, reason: reason + ' Not bypassed — open the assistant browser to hand it over to you.'};
  }

  async function stepAuto(job) {
    const r = job.recipe, v = job.vars, ph = job.phase;
    const key = r.kind === 'search' ? 'query' : 'train';
    const val = String(v[key] || v.train || '');
    if (ph === 'open') {
      if (r.kind !== 'pnr') { const b = blocker(job, 'on opening the site'); if (b) return b; }
      if (r.kind === 'list') {
        for (const t of r.nav_texts || []) { const el = clickText([t]); if (el) { later(el); return next('wait', {expectNav: true, note: `opened '${t}'`}); } }
        return {status: 'failed', reason: `No menu entry like '${(r.nav_texts || [''])[0]}'. Teach this page once.`};
      }
      if (r.kind === 'pnr') {
        const f = field(scan(), ['pnr'], [], ['text', 'number', 'tel', '']);
        if (!f) return {status: 'failed', reason: 'No PNR box found on the page. Teach pnr_status once.'};
        await typeInto(f.el, String(v.pnr || '')); f.el.dispatchEvent(new Event('change', {bubbles: true}));
        if (detect() === 'captcha') {
          if (!job.handover) return {status: 'blocked', kind: 'captcha', reason: 'The PNR page needs a CAPTCHA typed by you.'};
          return {status: 'needs_you', reason: `Type the CAPTCHA for PNR …${String(v.pnr).slice(-3)} and press Submit in the assistant browser.`, next: 'human', after: 'capture'};
        }
        const sb = submitButton(0); if (sb) { later(sb.el); return next('wait', {expectNav: true}); }
        return next('wait');
      }
      if (!trainField(scan()) && (r.nav_texts || []).length) {
        const el = clickText(r.nav_texts); if (el) { later(el); return next('fill', {expectNav: true, note: 'opened menu'}); }
      }
      return next('fill');
    }
    if (ph === 'fill') {
      const f = trainField(scan());
      if (!f) return {status: 'failed', reason: 'Could not find a train number box on the page. Teach this page once.'};
      await typeInto(f.el, val); await sleep(1000);
      return next(r.kind === 'search' ? 'submit' : 'suggest', {note: `typed ${key} ${val}`});
    }
    if (ph === 'suggest') {
      const f = trainField(scan());
      const it = scan().find(i => !i.field && f && i.el !== f.el && i.text.includes(val) && i.text.trim() !== val &&
        ['li', 'a', 'div', 'span', 'td', 'button'].includes(i.tag));
      if (it) { clickEl(it.el); await sleep(800); }
      return next('date', {note: it ? `picked '${it.text.slice(0, 40)}'` : 'no suggestion list'});
    }
    if (ph === 'date') { const n = v.date ? setDate(v.date) : 'no date'; await sleep(500); return next('station', {note: n}); }
    if (ph === 'station') {
      if (v.station) {
        const st = field(scan(), ['boarding', 'station', 'from'], ['train']);
        if (st) {
          await typeInto(st.el, String(v.station)); await sleep(900);
          const up = String(v.station).toUpperCase();
          const it = scan().find(i => !i.field && ['li', 'a', 'div', 'span', 'td'].includes(i.tag) && i.text.toUpperCase().includes(up) && i.text.trim().toUpperCase() !== up);
          if (it) clickEl(it.el);
        }
      }
      return next('submit');
    }
    if (ph === 'submit') {
      const f = trainField(scan());
      const sb = submitButton(f ? f.order : 0);
      if (sb) { later(sb.el); return next('wait', {expectNav: true, note: `pressed '${(sb.text || sb.label).slice(0, 30)}'`}); }
      if (f) { f.el.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', keyCode: 13, bubbles: true}));
               if (f.el.form) setTimeout(() => f.el.form.requestSubmit ? f.el.form.requestSubmit() : f.el.form.submit(), 60); }
      return next('wait', {expectNav: true, note: 'pressed Enter'});
    }
    return null;
  }

  async function stepRecorded(job) {
    const r = job.recipe, v = job.vars, n = parseInt(job.phase.split(':')[1], 10);
    const st = (r.steps || [])[n];
    if (!st) return next('wait');
    const prev = n > 0 ? r.steps[n - 1] : null;
    if (st.action !== 'human' && !(prev && prev.action === 'human') && r.kind !== 'pnr') { const b = blocker(job, `before step ${n + 1}`); if (b) return b; }
    const text = render(st.text, v), value = render(st.value, v), sel = st.selector;
    const q = s => { try { return s ? document.querySelector(s) : null; } catch (e) { return null; } };
    const byText = t => {            // never a typed-in field; exact text first, then the shortest element containing it
      const c = scan().filter(i => !i.field && i.text && i.text.includes(t));
      c.sort((a, b) => (b.text === t) - (a.text === t) || a.text.length - b.text.length || a.order - b.order);
      return c.length ? c[0].el : null;
    };
    const nx = `step:${n + 1}`;
    try {
      if (st.action === 'click') {
        let el = (text && (st.text !== text || !sel)) ? byText(text) : null;
        el = el || q(sel) || (text ? byText(text) : null);
        if (!el) throw new Error('element not found');
        later(el); return next(nx, {expectNav: true, note: `step ${n + 1}: click ${(text || sel || '').slice(0, 40)}`});
      }
      if (st.action === 'fill') { const el = q(sel); if (!el) throw new Error('field not found'); await typeInto(el, value || ''); el.dispatchEvent(new Event('change', {bubbles: true})); await sleep(600); return next(nx); }
      if (st.action === 'select') { const el = q(sel); if (!el || !selectByLabel(el, value)) throw new Error('option not found'); return next(nx); }
      if (st.action === 'press') { const el = q(sel); if (!el) throw new Error('field not found');
        el.dispatchEvent(new KeyboardEvent('keydown', {key: value || 'Enter', keyCode: 13, bubbles: true}));
        if ((value || 'Enter') === 'Enter' && el.form) setTimeout(() => el.form.requestSubmit ? el.form.requestSubmit() : el.form.submit(), 60);
        return next(nx, {expectNav: true}); }
      if (st.action === 'wait') { await sleep(Math.min(parseFloat(value || '1'), 10) * 1000); return next(nx); }
      if (st.action === 'wait_text') { if (bodyText().includes(text)) return next(nx); return Date.now() > job.deadline ? {status: 'failed', reason: `text '${text}' never appeared`} : {status: 'waiting'}; }
      if (st.action === 'human') {
        if (!job.handover) return {status: 'blocked', kind: 'captcha', reason: 'This page needs you to type a CAPTCHA. Open the assistant browser.'};
        return {status: 'needs_you', reason: 'Please complete the CAPTCHA and press Submit in the assistant browser.', next: 'human', after: nx};
      }
    } catch (e) {
      if (st.optional) return next(nx);
      return {status: 'failed', reason: `Recorded step ${n + 1} (${st.action} ${text || sel || ''}) failed: ${e.message}. The site may have changed — teach it again.`};
    }
    return next(nx);
  }

  async function step(job) {
    try {
      const r = job.recipe, v = job.vars, ph = job.phase;
      if (ph === 'human') {          // a person is solving a CAPTCHA / finishing a form
        const want = v.pnr ? String(v.pnr) : null;
        const txt = bodyText();
        const ok = detect() !== 'captcha' && (!want || (txt.includes(want) && /booking status|current status|chart/i.test(txt)));
        if (ok) return next(job.after || 'capture', {note: 'you finished the check'});
        return Date.now() > job.deadline ? {status: 'blocked', kind: 'captcha', reason: 'Timed out waiting for you.'} : {status: 'waiting'};
      }
      if (ph === 'wait' || ph === 'wait2') {
        const b = r.kind === 'pnr' ? null : blocker(job, 'while waiting for results'); if (b) return b;
        const t0 = Date.now();
        while (Date.now() - t0 < 1500) {          // short: the app does the longer waiting between steps
          if (resultsReady(r, v)) return next(ph === 'wait' && r.kind === 'berth' && v['class'] ? 'class' : 'capture');
          await sleep(500);
        }
        return Date.now() > job.deadline ? done('no_result', 'The page did not show a result for this train/date.') : {status: 'waiting'};
      }
      if (ph === 'class') {
        const el = clickText([String(v['class'])], true);
        if (el) { later(el); return next('wait2', {expectNav: true, note: `opened class ${v['class']}`}); }
        return next('capture');
      }
      if (ph === 'capture') {
        if (r.kind !== 'pnr') { const b = blocker(job, 'on the result page'); if (b && b.status === 'blocked') return b; }
        return done('ok');
      }
      if (ph.startsWith('step:')) return await stepRecorded(job);
      if (ph === 'open' && r.mode === 'recorded' && (r.steps || []).length) return next('step:0');
      const a = await stepAuto(job);
      return a || {status: 'failed', reason: 'unknown phase ' + ph};
    } catch (e) {
      return {status: 'failed', reason: 'driver error: ' + (e && e.message || e)};
    }
  }
  window.rvDriver = {v: 3, step, detect, capture, dateVariants, render};
})();
