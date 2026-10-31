// Tacitus setup page: one install phase per screen (docs/setup-web.md).
// Text is always set as text; nothing here parses HTML.
'use strict';

(function () {
  const $ = (id) => document.getElementById(id);
  const S = { token: '', phases: [], values: {}, context: {}, finish: [], idx: 0, busy: false };

  function readToken() {
    const m = /(?:^|[#&])t=([A-Za-z0-9_-]+)/.exec(location.hash);
    if (m) {
      try { sessionStorage.setItem('setup-token', m[1]); } catch (e) { /* storage off */ }
      history.replaceState(null, '', location.pathname + location.search);
      return m[1];
    }
    try { return sessionStorage.getItem('setup-token') || ''; } catch (e) { return ''; }
  }

  async function api(method, path, body) {
    const opts = { method, cache: 'no-store', headers: { 'X-Setup-Token': S.token } };
    if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    let r;
    try {
      r = await fetch('api/' + path, opts);
    } catch (e) {
      return { ok: false, status: 0, data: { error: 'The setup server does not answer. Is it still running?' } };
    }
    let data = {};
    try { data = await r.json(); } catch (e) { data = { error: 'The server sent no answer.' }; }
    return { ok: r.ok, status: r.status, data };
  }

  function el(tag, attrs, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === undefined || v === null || v === false || v === '') continue;
      if (k === 'class') n.className = v;
      else if (k === 'text') n.textContent = v;
      else n.setAttribute(k, v === true ? '' : String(v));
    }
    for (const kid of kids) if (kid !== null && kid !== undefined) n.append(kid);
    return n;
  }

  function status(text, bad) {
    $('status').textContent = text || '';
    $('status').classList.toggle('bad', !!bad);
  }

  function busy(on) {
    S.busy = on;
    $('next').disabled = on;
    $('prev').disabled = on;
  }

  // -- loading ----------------------------------------------------------

  async function load(what) {
    const [p, a] = await Promise.all([
      what.phases ? api('GET', 'phases') : null,
      api('GET', 'answers'),
    ]);
    for (const r of [p, a]) {
      if (r && !r.ok) { status(r.data.error || 'The server refused.', true); return false; }
    }
    if (p) {
      S.phases = p.data.phases;
      S.finish = p.data.finish;
      S.context = p.data.context;
    }
    S.values = a.data.values;
    return true;
  }

  // -- a phase ----------------------------------------------------------

  function holds(cond, vals) {
    return [].concat(cond || []).every((c) => {
      const got = vals[c.key];
      if ('is' in c) return got === c.is;
      if ('in' in c) return c.in.includes(got);
      if ('not_in' in c) return !c.not_in.includes(got);
      return true;
    });
  }

  function fill(label, vals) {
    return label.replace(/\{([a-z0-9-]+)\}/g, (_, k) => String(vals[k] || '...'));
  }

  function field(q) {
    const id = 'f-' + q.key;
    const v = S.values[q.key];
    const row = el('div', { class: 'field', 'data-key': q.key });
    if (q.notice) {
      row.append(el('div', { class: 'notice', role: 'note' },
        ...q.notice.split('\n').map((p) => el('p', { text: p }))));
    }
    const help = [...(q.help || []), q.hint, q.kind === 'list' ? 'Separate them with commas.' : '']
      .filter(Boolean).map((t) => el('p', { class: 'help', text: t }));
    if (q.kind === 'yesno') {
      const box = el('input', { type: 'checkbox', id, name: id });
      box.checked = v === true;
      row.append(...help, el('label', { class: 'check' }, box, el('span', { class: 'label', text: q.label })));
    } else if (q.kind === 'choice') {
      const set = el('fieldset', { class: 'choices' }, el('legend', { class: 'label', text: q.label }));
      for (const c of q.choices) {
        const r = el('input', { type: 'radio', name: id, value: c.value });
        r.checked = v === c.value;
        set.append(el('label', { class: 'choice' }, r, el('span', { text: c.label })));
      }
      row.append(...help, set);
    } else {
      const mode = { email: 'email', emails: 'email', url: 'url' }[q.check] || (q.kind === 'int' ? 'numeric' : '');
      const input = el('input', {
        id, name: id, type: q.kind === 'secret' ? 'password' : (q.kind === 'time' ? 'time' : 'text'),
        inputmode: mode, autocomplete: q.kind === 'secret' ? 'new-password' : 'off',
        autocapitalize: 'off', spellcheck: 'false',
        placeholder: q.kind === 'secret' ? 'Not shown again; type it to set it' : q.example,
      });
      input.value = Array.isArray(v) ? v.join(', ') : (v === undefined || v === null ? '' : String(v));
      row.append(el('label', { class: 'label', for: id, text: q.label + (q.required ? ' (required)' : '') }),
        ...help, input);
    }
    row.append(el('p', { class: 'err', id: id + '-err', hidden: true }));
    return row;
  }

  function readField(q) {
    const item = $('phase').elements.namedItem('f-' + q.key);
    if (!item) return '';
    if (q.kind === 'yesno') return item.checked;
    return item.value;
  }

  // Shows the questions whose condition holds for the answers above them,
  // the way the server walks the phase; returns those answers.
  function refresh() {
    const ph = S.phases[S.idx];
    const vals = {};
    for (const q of ph.questions) {
      const row = $('fields').querySelector(`[data-key="${q.key}"]`);
      row.hidden = !holds(q.when, vals);
      const label = row.querySelector('.label');
      if (label && q.label.includes('{')) label.textContent = fill(q.label, vals);
      if (!row.hidden) vals[q.key] = readField(q);
    }
    return vals;
  }

  function showPhase() {
    const ph = S.phases[S.idx];
    $('phase').hidden = false;
    $('finish').hidden = true;
    $('title').textContent = ph.title;
    $('intro').replaceChildren(...ph.intro.map((t) => el('p', { text: t })));
    if (ph.id === 'profile' && S.context.acknowledged_on) {
      $('intro').append(el('p', { text: `You accepted the notice on ${S.context.acknowledged_on}.` }));
    }
    $('fields').replaceChildren(...ph.questions.map(field));
    $('phase-error').hidden = true;
    refresh();
    $('step').textContent = `Step ${S.idx + 1} of ${S.phases.length + 1}`;
    $('prev').hidden = S.idx === 0;
    $('next').textContent = S.idx === S.phases.length - 1 ? 'Finish' : 'Next';
    window.scrollTo(0, 0);
  }

  function showErrors(errors, message) {
    let first = null;
    const loose = [];
    for (const [key, text] of Object.entries(errors || {})) {
      const slot = $(`f-${key}-err`);
      if (!slot) { loose.push(text); continue; }
      slot.textContent = text;
      slot.hidden = false;
      slot.parentElement.classList.add('invalid');
      first = first || slot.parentElement;
    }
    if (loose.length || !first) {
      $('phase-error').textContent = [message, ...loose].filter(Boolean).join(' ');
      $('phase-error').hidden = false;
    }
    (first || $('phase-error')).scrollIntoView({ block: 'center' });
    const input = first && first.querySelector('input');
    if (input) input.focus({ preventScroll: true });
  }

  async function next() {
    if (S.busy) return;
    if (S.idx >= S.phases.length) { await done(); return; }
    const ph = S.phases[S.idx];
    const answers = refresh();
    for (const n of $('fields').querySelectorAll('.err')) n.hidden = true;
    for (const n of $('fields').querySelectorAll('.invalid')) n.classList.remove('invalid');
    $('phase-error').hidden = true;
    busy(true);
    status('Saving...');
    const r = await api('POST', 'answers', { phase: ph.id, answers });
    busy(false);
    if (!r.ok) {
      status('');
      showErrors(r.data.errors, r.data.error);
      return;
    }
    const saved = (r.data.saved || []).map((p) => p.split('/').pop());
    // The profile and the chat platform decide what the later screens ask.
    if (!(await load({ phases: ph.id === 'profile' || ph.id === 'chat' }))) return;
    status(saved.length ? `Saved ${saved.join(', ')}.` : 'Saved.');
    S.idx += 1;
    if (S.idx < S.phases.length) showPhase(); else await finish();
  }

  function prev() {
    if (S.busy || S.idx === 0) return;
    S.idx -= 1;
    status('');
    showPhase();
  }

  // -- the end ----------------------------------------------------------

  async function finish() {
    $('phase').hidden = true;
    $('finish').hidden = false;
    $('step').textContent = `Step ${S.phases.length + 1} of ${S.phases.length + 1}`;
    $('prev').hidden = false;
    $('next').textContent = 'Done';
    window.scrollTo(0, 0);
    busy(true);
    const r = await api('POST', 'answers', { phase: 'state', answers: {} });
    // The steps follow the answers (the profile, the Outlook method).
    const loaded = r.ok && await load({ phases: true });
    busy(false);
    if (!r.ok) { status(r.data.error, true); return; }
    if (!loaded) return;
    $('steps').replaceChildren(...S.finish.map((s) => el('li', {},
      el('h3', { text: s.title }), el('p', { text: s.text }),
      ...s.commands.map((c) => el('pre', { class: 'cmd' }, el('code', { text: c }))))));
    await doctor();
  }

  async function doctor() {
    if (S.busy) return;
    busy(true);
    $('again').disabled = true;
    $('rows').replaceChildren();
    $('summary').textContent = 'Running the checklist...';
    const r = await api('POST', 'doctor', {});
    busy(false);
    $('again').disabled = false;
    if (!r.ok) { $('summary').textContent = r.data.error || 'The checklist did not run.'; return; }
    const d = r.data;
    $('summary').textContent = `${d.pass} ok, ${d.warn} to look at, ${d.fail} to fix`;
    const mark = { PASS: 'ok', WARN: 'warn', FAIL: 'fail' };
    // What to fix first: the FAIL rows, then the warnings, in the doctor's order.
    const rank = { FAIL: 0, WARN: 1, PASS: 2 };
    const rows = d.results.slice().sort((a, b) => (rank[a.status] ?? 1) - (rank[b.status] ?? 1));
    $('rows').replaceChildren(...rows.map((row) => el('li', { class: 'row ' + (mark[row.status] || 'warn') },
      el('span', { class: 'mark', text: mark[row.status] || row.status }),
      el('span', { class: 'name', text: row.name }),
      row.detail ? el('span', { class: 'detail', text: row.detail }) : null,
      row.status !== 'PASS' && row.fix ? el('span', { class: 'fix', text: 'Fix: ' + row.fix }) : null)));
  }

  async function done() {
    busy(true);
    const r = await api('POST', 'done', {});
    busy(false);
    if (!r.ok) { status(r.data.error, true); return; }
    try { sessionStorage.removeItem('setup-token'); } catch (e) { /* storage off */ }
    status('');
    $('finish').hidden = true;
    $('nav').hidden = true;
    $('step').textContent = '';
    $('bye').hidden = false;
  }

  function back() {
    if (S.idx >= S.phases.length) { S.idx = S.phases.length - 1; status(''); showPhase(); } else prev();
  }

  async function start() {
    $('next').addEventListener('click', next);
    $('prev').addEventListener('click', back);
    $('again').addEventListener('click', doctor);
    $('phase').addEventListener('submit', (e) => { e.preventDefault(); next(); });
    $('phase').addEventListener('input', refresh);
    $('phase').addEventListener('change', refresh);
    S.token = readToken();
    if (!S.token) {
      status('Open the address the terminal printed, with its #t= part.', true);
      $('nav').hidden = true;
      return;
    }
    busy(true);
    status('Loading...');
    const ok = await load({ phases: true });
    busy(false);
    if (!ok) { $('nav').hidden = true; return; }
    status('');
    showPhase();
  }

  start();
})();
