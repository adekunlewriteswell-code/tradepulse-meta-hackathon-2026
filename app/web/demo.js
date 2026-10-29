'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const state = {csrf: null, route: null, session: null, overview: null, selected: new Set(), analysis: null, busy: false, analyzing: false};
  const keys = {session: 'tradepulse.demo.session', input: 'tradepulse.demo.pending-input', analysis: 'tradepulse.demo.pending-analysis'};
  const store = (key, value) => {try {value === null ? localStorage.removeItem(key) : localStorage.setItem(key, JSON.stringify(value));} catch { /* Server state remains authoritative when storage is unavailable. */ }};
  const read = key => {try {return JSON.parse(localStorage.getItem(key));} catch {return null;}};
  function node(tag, text, className) {const el = document.createElement(tag); if (text !== undefined) el.textContent = text; if (className) el.className = className; return el;}
  function error(message) {$('global-error').textContent = message || ''; $('global-error').hidden = !message;}
  async function api(path, body) {
    const options = {credentials: 'same-origin', headers: {}};
    if (body !== undefined) {options.method = 'POST'; options.headers = {'Content-Type': 'application/json', 'X-Demo-CSRF': state.csrf}; options.body = JSON.stringify(body);}
    const response = await fetch('/demo/api/' + path, options);
    const result = await response.json();
    if (!response.ok) {const e = new Error(typeof result.detail === 'string' ? result.detail : 'The request was rejected. Check the numbered input and reload.'); e.status = response.status; throw e;}
    return result;
  }
  function view(name) {
    const manager = name === 'manager';
    $('retailer-view').hidden = manager; $('manager-view').hidden = !manager;
    for (const n of ['retailer', 'manager']) {const b = $('nav-' + n); b.classList.toggle('active', n === name); n === name ? b.setAttribute('aria-current', 'page') : b.removeAttribute('aria-current');}
    $('page-title').textContent = manager ? 'A signal is a starting point.\nEvidence makes it useful.' : 'Every inactive retailer\nhas a reason.';
    $('page-description').textContent = manager ? 'Understand retailer-stated barriers. Inspect the evidence. Review an advisory plan.' : 'A simple numbered menu. A traceable observation. A clearer starting point for the route manager.';
    $('chapter-number').textContent = manager ? '02' : '01'; $('chapter-label').textContent = manager ? 'UNDERSTAND THE SIGNAL' : 'CAPTURE THE SIGNAL';
    if (manager && state.csrf) refresh().catch(e => error(e.message));
  }
  function sessionControls() {
    const active = state.session && !state.session.terminal;
    document.querySelectorAll('[data-digit]').forEach(b => b.disabled = !active || state.busy);
    $('back').disabled = !active || state.busy; $('cancel').disabled = !active || state.busy;
    $('retailer').disabled = !state.csrf || state.busy || !!active;
    $('dial').disabled = !state.csrf || state.busy;
    $('dial').textContent = state.session ? 'Restart with a new session ↗' : 'Start a session ↗';
    $('retry-session').hidden = !read(keys.input);
  }
  function renderSession() {
    const s = state.session;
    if (s) {
      const terminal = $('terminal'); terminal.replaceChildren();
      terminal.append(node('p', 'SYNTHETIC RETAILER SESSION', 'terminal-kicker'), node('p', s.title, 'menu-title'));
      const list = node('ol', undefined, 'menu-options');
      for (const option of s.options) list.append(node('li', option.digit + '. ' + option.label));
      if (!s.terminal) list.append(node('li', '0. Cancel'));
      terminal.append(list, node('p', s.message, 'terminal-message'));
      if (s.observation_id) terminal.append(node('p', s.observation_id, 'terminal-observation'));
      $('session-status').textContent = s.retailer_name + ' · ' + s.stage.toLowerCase() + ' · saved version ' + s.version;
      $('retailer').value = s.retailer_id;
      store(keys.session, s.id);
    }
    sessionControls();
  }
  async function submitSession(pending) {
    if (state.busy) return;
    state.busy = true; store(keys.input, pending); sessionControls(); error('');
    $('session-status').textContent = 'Saving this numbered-menu transition…';
    try {
      state.session = await api(pending.path, pending.body);
      store(keys.input, null); renderSession();
      if (state.session.observation_id) {state.selected = new Set([state.session.observation_id]); await refresh();}
    } catch (e) {
      error(e.message + ' Your pending request is retained. Retry safely with the same key.');
      if (e.status === 409 && state.session) {try {state.session = await api('sessions/' + encodeURIComponent(state.session.id)); store(keys.input, null); renderSession();} catch { /* Keep the pending request for explicit reconciliation. */ }}
    } finally {state.busy = false; sessionControls();}
  }
  async function startSession() {
    if (state.session && !state.session.terminal && !window.confirm('Start a new session? Your current progression remains saved, but will no longer be the selected session.')) return;
    await submitSession({path: 'sessions', body: {request_id: crypto.randomUUID(), route_id: state.route, retailer_id: $('retailer').value}});
  }
  function send(action, digit = null) {
    if (!state.session || state.session.terminal) return;
    submitSession({path: 'sessions/' + encodeURIComponent(state.session.id) + '/input', body: {request_id: crypto.randomUUID(), expected_version: state.session.version, action, digit}});
  }
  function selectionControls() {
    $('selected-count').textContent = state.selected.size + ' / 8';
    $('analyze').disabled = !state.selected.size || state.analyzing || !state.csrf;
    $('mode').disabled = state.analyzing;
    $('retry-analysis').hidden = !read(keys.analysis) || state.analyzing;
  }
  function renderEvidence() {
    const list = $('evidence-list'); list.replaceChildren();
    const rows = state.overview.observations;
    state.selected = new Set([...state.selected].filter(id => rows.some(r => r.id === id && r.analyzable)));
    if (!rows.length) list.append(node('p', 'No observations yet. Complete a retailer session to save the first signal.', 'muted small'));
    for (const row of rows) {
      const card = node('div', undefined, 'evidence-card'); const checkbox = node('input'); checkbox.type = 'checkbox'; checkbox.checked = state.selected.has(row.id); checkbox.disabled = !row.analyzable;
      checkbox.setAttribute('aria-label', 'Select observation ' + row.id);
      checkbox.addEventListener('change', () => {if (checkbox.checked && state.selected.size >= 8) {checkbox.checked = false; error('Select at most eight observations.'); return;} checkbox.checked ? state.selected.add(row.id) : state.selected.delete(row.id); selectionControls();});
      const body = node('div'); const retailer = state.overview.retailers.find(r => r.id === row.retailer_id);
      body.append(node('span', row.category, 'category'), node('h3', retailer ? retailer.name : row.retailer_id), node('p', row.barrier), node('p', row.id, 'evidence-id'));
      const inspect = node('button', 'Inspect raw evidence ↗', 'text-button'); inspect.type = 'button'; inspect.addEventListener('click', () => inspectEvidence(row.id)); body.append(inspect);
      card.append(checkbox, body); list.append(card);
    }
    selectionControls();
  }
  async function refresh() {
    state.overview = await api('overview');
    const o = state.overview;
    if (!$('retailer').dataset.loaded) {
      $('retailer').replaceChildren(); for (const r of o.retailers) {const option = node('option', r.name + ' · ' + r.status.toLowerCase()); option.value = r.id; $('retailer').append(option);}
      $('retailer').value = o.retailers.find(r => r.status === 'INACTIVE')?.id || o.retailers[0]?.id; $('retailer').dataset.loaded = 'true';
    }
    $('observation-count').textContent = o.observations.length;
    $('baseline-revenue').textContent = o.route.revenue; $('baseline-contribution').textContent = o.route.contribution;
    $('retailer-list').replaceChildren(); for (const r of o.retailers) {const row = node('div', r.name, 'retailer-row'); row.append(node('span', r.status + ' · synthetic')); $('retailer-list').append(row);}
    const current = state.analysis?.id || $('history').value;
    $('history').replaceChildren(node('option', 'Select a saved analysis'));
    $('history').firstChild.value = '';
    for (const a of o.analyses) {const option = node('option', a.source + ' · ' + a.status + ' · ' + a.id.slice(0, 8)); option.value = a.id; $('history').append(option);}
    $('history').value = current;
    renderEvidence(); renderSession();
  }
  async function inspectEvidence(id) {
    const row = state.overview?.observations.find(r => r.id === id);
    if (!row) {error('This evidence is not in the current inbox. Refresh saved evidence.'); return;}
    const detail = $('evidence-detail'); detail.replaceChildren(node('p', 'RETAILER STATEMENT - SYNTHETIC, NOT A VERIFIED FACT', 'eyebrow'), node('p', row.barrier), node('p', 'Evidence ID: ' + row.id, 'evidence-id'), node('p', 'Products: ' + row.products.join(', ')), node('pre', row.raw_session_log));
    $('evidence-dialog').showModal();
    try {const trace = await api('evidence/' + encodeURIComponent(id) + '/trace'); detail.append(node('p', 'Full numbered-menu interaction trace'), node('pre', JSON.stringify(trace.inputs, null, 2)));} catch {detail.append(node('p', 'An interaction trace is unavailable for this observation.'));}
  }
  function list(parent, title, items) {parent.append(node('h4', title)); const ul = node('ul'); for (const item of items.length ? items : ['None supplied; manager must assess this gap.']) ul.append(node('li', item)); parent.append(ul);}
  function renderAnalysis(data) {
    state.analysis = data;
    const result = $('analysis-result'); result.replaceChildren();
    $('mode-label').textContent = data.mode.toUpperCase() + ' · OFFLINE';
    const meta = node('div', undefined, 'result-meta'); meta.append(node('span', data.status), node('span', data.model), node('span', data.run_state || 'LEGACY')); result.append(meta);
    if (!data.output) {result.append(node('div', 'Analysis failed or is incomplete. ' + (data.error_category || data.status) + '. No recommendation was fabricated. Replaying this key returns the recorded outcome.', 'notice error')); return;}
    result.append(node('p', 'ADVISORY INTERPRETATION - NOT A VERIFIED COMMERCIAL FACT', 'eyebrow'), node('p', data.output.pattern_summary, 'analysis-summary'), node('p', 'Inferred intent: ' + data.output.intent_level + '. This is not verified demand.', 'small muted'));
    result.append(node('p', 'Product signals: ' + data.output.extracted_products.join(', '), 'small muted'));
    const barriers = node('details'); barriers.append(node('summary', 'Inspect extracted barriers and model confidence'));
    for (const barrier of data.output.stated_barriers) {
      barriers.append(node('p', barrier.barrier + ' · model confidence ' + Math.round(barrier.confidence * 100) + '%', 'small muted'));
      const cite = node('button', barrier.evidence + ' ↗', 'citation'); cite.type = 'button'; cite.addEventListener('click', () => inspectEvidence(barrier.evidence)); barriers.append(cite);
    }
    result.append(barriers);
    data.output.activation_strategies.forEach((strategy, index) => {
      const article = node('article', undefined, 'strategy'); article.append(node('p', 'STRATEGY ' + (index + 1), 'eyebrow'), node('h3', strategy.strategy), node('p', 'Model confidence: ' + Math.round(strategy.confidence * 100) + '% · not a measured success probability', 'confidence'));
      for (const id of strategy.evidence_basis) {const button = node('button', id + ' ↗', 'citation'); button.type = 'button'; button.addEventListener('click', () => inspectEvidence(id)); article.append(button);}
      list(article, 'Preconditions', strategy.preconditions); list(article, 'Risks and unknowns', strategy.risks);
      article.append(node('h4', 'Local demonstration review'));
      const controls = node('div', undefined, 'review-controls');
      for (const [decision, label] of [['reviewed', 'Mark reviewed'], ['planning', 'Accept for planning'], ['rejected', 'Reject']]) {const button = node('button', label); button.type = 'button'; button.dataset.decision = decision; button.addEventListener('click', () => review(data.id, index, decision, button)); controls.append(button);}
      const latest = data.reviews.find(r => r.strategy_index === index);
      article.append(controls, node('p', latest ? 'Saved demo decision: ' + latest.decision + '. Not mission authorization.' : 'Demo-only. No authenticated manager identity or financial authorization.', 'review-status')); result.append(article);
    });
    const uncertainty = node('div', undefined, 'uncertainty'); uncertainty.append(node('strong', 'WHAT WE DO NOT KNOW'), node('p', data.output.uncertainty_statement));
    for (const flag of data.output.uncertainty_flags) uncertainty.append(node('p', flag)); result.append(uncertainty);
  }
  async function analyze(pending) {
    if (state.analyzing) return;
    pending = pending || {request_id: crypto.randomUUID(), observation_ids: [...state.selected], mode: $('mode').value};
    store(keys.analysis, pending); state.analyzing = true; selectionControls(); error('');
    $('analysis-status').textContent = 'Loading stored evidence and validating offline analysis…';
    $('analysis-result').replaceChildren(node('div', 'Analyzing saved evidence. No live provider call.', 'loading'));
    try {
      const data = await api('analyses', pending); store(keys.analysis, null); renderAnalysis(data); await refresh();
      $('analysis-status').textContent = data.output ? 'Validated analysis saved. Inspect its citations before planning.' : 'Failure recorded. This key will replay that failure; it will not silently rerun.';
    } catch (e) {error(e.message); $('analysis-status').textContent = 'Request retained. Reconcile or retry the same analysis key.'; $('analysis-result').replaceChildren(node('div', 'Analysis unavailable. No result has been invented.', 'notice error'));}
    finally {state.analyzing = false; selectionControls();}
  }
  async function review(id, index, decision, button) {
    const key = 'tradepulse.demo.review.' + id + '.' + index + '.' + decision;
    const body = read(key) || {request_id: crypto.randomUUID(), analysis_id: id, strategy_index: index, decision, demo_only: true}; store(key, body); button.disabled = true;
    try {await api('reviews', body); store(key, null); renderAnalysis(await api('analyses/' + encodeURIComponent(id))); error('');} catch (e) {error(e.message + ' The same review key is retained for retry.');} finally {button.disabled = false;}
  }
  $('nav-retailer').addEventListener('click', () => view('retailer')); $('nav-manager').addEventListener('click', () => view('manager')); $('view-manager').addEventListener('click', () => view('manager'));
  $('dial').addEventListener('click', startSession); $('back').addEventListener('click', () => send('back')); $('cancel').addEventListener('click', () => send('cancel'));
  document.querySelectorAll('[data-digit]').forEach(button => button.addEventListener('click', () => send('input', button.dataset.digit)));
  $('retry-session').addEventListener('click', () => {const pending = read(keys.input); if (pending) submitSession(pending);});
  $('refresh').addEventListener('click', () => refresh().catch(e => error(e.message)));
  $('analyze').addEventListener('click', () => analyze()); $('retry-analysis').addEventListener('click', () => analyze(read(keys.analysis)));
  $('history').addEventListener('change', async () => {if (!$('history').value) return; try {renderAnalysis(await api('analyses/' + encodeURIComponent($('history').value)));} catch(e) {error(e.message);}});
  $('close-dialog').addEventListener('click', () => $('evidence-dialog').close());
  async function boot() {
    try {const data = await api('bootstrap'); state.csrf = data.csrf_token; state.route = data.route_id; $('mode').value = data.default_mode; await refresh();
      const id = read(keys.session); if (id) {try {state.session = await api('sessions/' + encodeURIComponent(id));} catch {store(keys.session, null); error('Your prior local session is unavailable or its browser capability expired. Start a new session; saved observations remain in the evidence inbox.');}}
      $('session-status').textContent = 'Local application ready. Choose a retailer and start.'; renderSession(); selectionControls();
    } catch(e) {error('Unable to connect to the enabled local demonstration. ' + e.message + ' Reload to retry.'); $('session-status').textContent = 'Connection unavailable. No session has been registered.';}
  }
  sessionControls(); boot();
})();
