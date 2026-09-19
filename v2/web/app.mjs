import {OperatorAPI, ApiError, RunHistory, RecordingCache, runPath, validRunId,
  count, audioLabel, officialLabel, matchesFilter, transcriptRows, evidence, textElement} from './core.mjs';
import {BrowserVoice} from './voice.mjs';

const el = id => document.getElementById(id);
const node = (tag, text, cls) => textElement(document, tag, text, cls);
const pretty = value => JSON.stringify(value, null, 2);
const history = new RunHistory();
const reports = new Map();
let selected = null, session = null, lastSessionRun = null, sessionEpoch = 0;
let pollBusy = false, pollEpoch = 0, fixtureBusy = false, moreBusy = false;
let health = null;
const api = new OperatorAPI(globalThis.fetch.bind(globalThis), () => {
  clearWorkspace(); notice('Operator authentication was rejected. Unlock again with a valid token.', true);
});
const recordings = new RecordingCache(api);
const voiceAPI = {request(path, options) {
  if (path !== '/api/voice/ticket') throw new ApiError(0, 'Unexpected caller request.');
  return api.authenticated ? api.request(path, options)
    : api.request('/api/public/voice/ticket', {...options, body: {}, publicRequest: true});
}};
const voice = new BrowserVoice(voiceAPI, {
  onStatus: message => { el('session-status').textContent = message; },
  onLevel: value => { el('microphone-level').value = value; },
  onMuteChange: () => controls(),
  onReady: runId => {
    if (session?.kind !== 'voice') return;
    session.runId = runId; lastSessionRun = api.authenticated ? runId : null;
    if (!api.authenticated) el('talk-transcript').replaceChildren(node('p', 'Connected. Speak naturally and listen to the agent through your speakers or headphones.', 'empty'));
    controls(); refreshAuthenticated();
  },
  onStopped: message => {
    if (session?.kind !== 'voice') return;
    session = null; sessionEpoch++; el('session-status').textContent = message;
    notice(message, /failed|denied|timed out|unexpected|cannot|invalid|error|suspended|exceeded|disconnected/i.test(message));
    controls(); refreshAuthenticated();
  },
});
function notice(message = '', error = false) {
  el('notice').textContent = message; el('notice').hidden = !message;
  el('notice').className = error ? 'notice error' : 'notice';
}
function reportError(error) { notice(error instanceof ApiError ? error.message : 'The operation failed. Check readiness and inspect the run before retrying.', true); }
function date(value) { if (!value) return 'Time unknown'; const d = new Date(value); return Number.isNaN(d.valueOf()) ? 'Time unknown' : d.toLocaleString(); }
function badge(text, cls = '') { return node('span', text, `pill ${cls}`); }
function metricRow(label, value) { const row = node('div', ''); row.append(node('dt', label), node('dd', value)); return row; }
function controls() {
  const unlocked = api.authenticated, active = Boolean(session), voiceSession = session?.kind === 'voice';
  const voiceActive = voiceSession && voice.phase === 'active';
  const voiceState = voiceActive ? voice.muted ? 'Microphone muted' : 'Microphone live' : 'Connecting microphone';
  const canStart = unlocked && !active && !fixtureBusy;
  const voiceReady = unlocked ? health?.voice_ready === true : health?.public_voice_ready === true;
  el('logout').hidden = !unlocked;
  el('auth-panel').hidden = unlocked || !['overview', 'runs'].includes(location.hash.slice(1));
  el('operator-login').hidden = unlocked;
  for (const link of document.querySelectorAll('[data-operator-nav]')) link.hidden = !unlocked;
  el('call-options').hidden = !unlocked; el('inspect-session').hidden = !unlocked;
  el('start-text').disabled = !canStart || health?.text_ready !== true;
  el('start-voice').disabled = active || fixtureBusy || !voiceReady;
  el('stop-session').disabled = !active || session?.ending;
  el('global-stop').hidden = !active; el('global-stop').disabled = Boolean(session?.ending);
  el('active-session').hidden = !active;
  el('active-session').textContent = active ? voiceSession ? voiceState : 'Text chat' : '';
  el('voice-controls').hidden = !voiceSession; el('turn-form').hidden = session?.kind !== 'text';
  el('microphone-state').textContent = voiceState;
  el('mute-voice').disabled = !voiceActive;
  el('mute-voice').textContent = voice.muted ? 'Unmute microphone' : 'Mute microphone';
  el('mute-voice').setAttribute('aria-pressed', String(voice.muted));
  el('voice-access').textContent = active ? 'Your conversation is active. Hang up when you are finished.'
    : !health ? 'Connecting to the server…'
    : !voiceReady ? 'Calling is temporarily unavailable. Please try again later.'
    : 'Ready. Press Call and allow microphone access.';
  el('language').disabled = active || fixtureBusy; el('mode').disabled = active;
  el('fixture').disabled = !unlocked || active || fixtureBusy;
  const canType = unlocked && session?.kind === 'text' && Boolean(session.runId) && !session.busy && !session.ending;
  el('turn').disabled = !canType; el('send-turn').disabled = !canType;
  el('inspect-session').disabled = !lastSessionRun;
  el('load-more').disabled = !unlocked || !history.cursor || moreBusy;
}
function clearWorkspace() {
  voice.stop('Workspace locked. Microphone released.');
  session = null; lastSessionRun = null; sessionEpoch++; pollEpoch++; pollBusy = false;
  selected = null; history.clear(); reports.clear(); fixtureBusy = false; moreBusy = false;
  releaseRecordings(); recordings.clear(); el('run-detail').hidden = true;
  for (const id of ['transcript', 'detail-stats', 'trace', 'evidence-actions', 'evidence-errors', 'evidence-timings', 'evidence-grades', 'detail-title', 'detail-meta', 'transcript-count']) el(id).replaceChildren();
  el('turn').value = ''; el('token').value = '';
  el('talk-transcript').replaceChildren(node('p', 'Press Call to talk to the agent.', 'empty'));
  delete el('talk-transcript').dataset.signature; delete el('transcript').dataset.signature;
  el('session-status').textContent = 'No active call.';
  el('fixture-status').textContent = ''; renderBudget(null); renderHistory(); controls();
  el('poll-status').textContent = 'Unlock to load history.';
}
function route() {
  const page = ['overview', 'runs', 'talk'].includes(location.hash.slice(1)) ? location.hash.slice(1) : 'talk';
  for (const name of ['overview', 'runs', 'talk']) el(`view-${name}`).hidden = page !== name;
  for (const a of document.querySelectorAll('.nav a')) {
    if (a.hash === `#${page}`) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
  }
  controls();
}
async function refreshHealth() {
  try {
    health = await api.request('/health', {publicRequest: true, timeout: 10000});
    el('connection').textContent = 'Backend reachable'; el('connection').className = 'pill good';
    el('backend-status').textContent = health.status === 'ok' ? 'Reachable' : String(health.status || 'Unknown');
    el('voice-ready').textContent = health.voice_ready === true ? 'Configured' : health.voice_ready === false ? 'Needs setup' : 'Unknown';
    el('readiness-summary').textContent = health.voice_ready === true
      ? 'Voice configuration is present. This is not a live provider, language-quality or official acceptance test.'
      : 'Voice needs configuration before use. Offline fixtures do not need voice providers.';
    el('missing').replaceChildren(...(Array.isArray(health.missing) ? health.missing : []).map(item => node('li', `Missing: ${typeof item === 'string' ? item : pretty(item)}`)));
    el('health-json').textContent = pretty(health);
    el('execution-mode').textContent = health.live_cutover_enabled
      ? 'Live carrier mode is configured. Browser and text rehearsal never submit to Prosper.'
      : 'Simulation and read-only practice. Live carrier submissions are disabled.';
  } catch {
    health = null; el('connection').textContent = 'Backend unavailable'; el('connection').className = 'pill bad';
    el('backend-status').textContent = 'Unavailable'; el('voice-ready').textContent = 'Unknown';
    el('readiness-summary').textContent = 'Cannot reach /health. Start the v2 server or check your connection, then Refresh.';
    el('missing').replaceChildren(); el('health-json').textContent = 'Readiness unavailable.';
  }
  controls();
}
function runButton(run) {
  const button = node('button', run.run_id, 'run-link'); button.type = 'button';
  button.setAttribute('aria-label', `Inspect run ${run.run_id}`); button.addEventListener('click', () => selectRun(run.run_id)); return button;
}
function renderHistory() {
  const all = history.list(), filtered = all.filter(run => matchesFilter(run, el('run-filter').value));
  el('run-count').textContent = api.authenticated ? String(all.length) : '—';
  el('history-empty').hidden = filtered.length > 0;
  el('history-empty').textContent = !api.authenticated ? 'Unlock to load actual stored runs.' : all.length ? 'No loaded runs match this filter.' : 'No stored runs yet. Try the free fixture in Talk to agent.';
  el('runs-body').replaceChildren(...filtered.map(run => {
    const m = run.metrics || {}, tr = node('tr', ''); if (selected === run.run_id) tr.className = 'selected';
    const id = node('td', ''); id.append(runButton(run), node('small', date(run.created_at)));
    const mode = node('td', `${run.mode || 'Unknown'} · ${(run.language || 'unknown').toUpperCase()}`);
    const status = node('td', ''); status.append(badge(run.status || 'Unknown'));
    tr.append(id, mode, status, node('td', `${count(m.resolved)} / ${count(m.intents)}`), node('td', audioLabel(m.audio_status)), node('td', count(m.errors)), node('td', officialLabel(run.official_grade)));
    return tr;
  }));
  el('recent-runs').replaceChildren(...(all.length ? all.slice(0, 5).map(run => {
    const row = node('div', '', 'recent-row'), lead = node('div', '', 'grow'); lead.append(runButton(run), node('small', date(run.created_at)));
    row.append(lead, badge(`${run.mode || 'Unknown'} · ${run.language || 'Unknown'}`), badge(run.status || 'Unknown'), node('small', `Official: ${officialLabel(run.official_grade)}`)); return row;
  }) : [node('p', api.authenticated ? 'No stored runs yet. Run the free fixture to inspect real trace evidence.' : 'Unlock to load actual stored runs.', 'empty')]));
  controls();
}
function acceptReport(report) {
  if (!report || !validRunId(report.run_id)) throw new ApiError(0, 'The server returned an invalid run report.');
  const old = reports.get(report.run_id);
  if (old && (old.events?.at(-1)?.sequence || old.events?.length || 0) > (report.events?.at(-1)?.sequence || report.events?.length || 0)) return old;
  reports.set(report.run_id, report);
  return report;
}
async function refreshAuthenticated() {
  if (!api.authenticated || pollBusy) return;
  pollBusy = true; const epoch = pollEpoch, generation = api.generation;
  const current = () => api.authenticated && epoch === pollEpoch && generation === api.generation;
  try {
    const page = await api.request('/api/runs?limit=50');
    if (!current()) return;
    history.merge(page); renderHistory();
    el('poll-status').textContent = `Updated ${new Date().toLocaleTimeString()} · polling every 5 seconds. Status is local lifecycle, not an official score.`;
  } catch (error) { if (current()) { el('poll-status').textContent = 'History update failed; displayed data may be stale.'; reportError(error); } }
  if (!current()) return;
  await Promise.all([
    ...[...new Set([selected, session?.runId, lastSessionRun].filter(Boolean))].map(async id => {
      try {
        const response = await api.request(runPath(id)); if (!current()) return;
        const report = acceptReport(response);
        if (selected === id) renderDetail(report);
        if (lastSessionRun === id) renderTranscript(el('talk-transcript'), report);
      } catch (e) { if (current()) reportError(e); }
    }),
  ]);
  if (epoch === pollEpoch) pollBusy = false;
}
async function selectRun(id) {
  if (!api.authenticated || !validRunId(id)) return;
  selected = id; location.hash = '#runs'; el('run-detail').hidden = false;
  if (recordings.select(id)) { releaseRecordings(); buildRecordings(); }
  el('detail-title').textContent = id; el('detail-meta').textContent = 'Loading report…';
  for (const name of ['detail-stats', 'trace', 'evidence-actions', 'evidence-timings', 'evidence-errors', 'evidence-grades']) el(name).replaceChildren();
  el('transcript').replaceChildren(node('p', 'Loading transcript…', 'empty')); delete el('transcript').dataset.signature;
  renderHistory(); const generation = api.generation;
  try {
    const response = await api.request(runPath(id));
    if (api.generation !== generation || selected !== id) return;
    renderDetail(acceptReport(response));
  } catch (error) { if (api.generation === generation && selected === id) { el('detail-meta').textContent = 'Report unavailable. Refresh to retry.'; reportError(error); } }
}
function releaseRecordings() {
  for (const audio of el('recordings').querySelectorAll('audio')) { audio.pause(); audio.removeAttribute('src'); audio.load(); }
  el('recordings').replaceChildren();
}
function buildRecordings() {
  const runId = selected;
  for (const track of ['inbound', 'outbound']) {
    const box = node('section', '', 'recording'), label = node('h3', track === 'inbound' ? 'Caller / inbound' : 'Agent / outbound');
    const button = node('button', 'Load recording', 'btn ghost'); button.type = 'button';
    const status = node('p', 'Not loaded. Text fixtures normally have no audio.'); status.setAttribute('role', 'status');
    const audio = document.createElement('audio'); audio.controls = true; audio.preload = 'none'; audio.hidden = true;
    audio.setAttribute('aria-label', `${track} recording`);
    button.addEventListener('click', async () => {
      button.disabled = true; status.textContent = 'Fetching protected audio…';
      try {
        const url = await recordings.load(track); if (!url || selected !== runId || !api.authenticated) return;
        audio.src = url; audio.hidden = false; button.hidden = true; status.textContent = 'Ready. Press play to listen.';
      } catch (error) { if (selected === runId) { status.textContent = error instanceof ApiError && error.status === 404 ? 'No recording yet. The run may be text-only, still active, or have no saved audio. Retry after completion.' : error.message; button.disabled = false; } }
    });
    audio.addEventListener('error', () => { status.textContent = 'Your browser could not decode this recording. Inspect the backend audio artifact.'; });
    box.append(label, button, status, audio); el('recordings').append(box);
  }
}
function renderTranscript(container, report) {
  const rows = transcriptRows(report), signature = pretty(rows);
  if (container.dataset.signature === signature) return;
  const nearBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 70;
  container.dataset.signature = signature;
  container.replaceChildren(...(rows.length ? rows.map(row => {
    const article = node('article', '', `turn ${row.role}`), head = node('strong', row.label);
    if (row.timestamp) head.append(node('span', new Date(row.timestamp).toLocaleTimeString()));
    article.append(head, node('p', row.text)); return article;
  }) : [node('p', 'No transcript events recorded yet.', 'empty')]));
  if (nearBottom) container.scrollTop = container.scrollHeight;
}
function renderDetail(report) {
  el('detail-title').textContent = report.run_id;
  el('detail-meta').textContent = `${report.mode || report.state?.mode || 'Unknown'} · ${report.state?.language || report.language || 'Unknown'} · call ${report.call_id || 'unknown'}`;
  const m = report.metrics || {};
  el('detail-stats').replaceChildren(...[
    ['Intents resolved', `${count(m.resolved)} / ${count(m.intents)}`], ['Official score', officialLabel(report.official_grade)],
    ['HTTP accepted receipts', count(m.http_accepted)], ['Simulated receipts', count(m.simulated_receipts)],
    ['No action receipt', m.missing_submission === true ? 'Yes' : m.missing_submission === false ? 'No' : 'Unknown'],
    ['Audio output', audioLabel(m.audio_status)], ['Errors', count(m.errors)], ['Guard rejections', count(m.guard_rejections)],
  ].map(([key, value]) => metricRow(key, value)));
  renderTranscript(el('transcript'), report); el('transcript-count').textContent = `${transcriptRows(report).length} events`;
  const e = evidence(report);
  el('evidence-actions').replaceChildren(...(e.actions.length ? e.actions.map(event => {
    const p = event.payload || {}, box = node('article', '', 'evidence');
    box.append(node('h3', p.action || 'Action receipt'), badge(p.source === 'simulation' ? 'Simulated · no real submission' : p.source === 'platform_receipt' ? 'Platform HTTP receipt · not scored' : 'Unknown provenance', p.source === 'simulation' ? 'warn' : ''),
      node('p', `Accepted: ${p.accepted === true ? 'yes' : p.accepted === false ? 'no' : 'unknown'} · HTTP: ${p.http_status ?? 'not applicable / unknown'}`), node('pre', pretty(p))); return box;
  }) : [node('p', 'No action receipts recorded. This is not an official failure classification.', 'muted')]));
  el('evidence-errors').replaceChildren(...(e.errors.length ? e.errors.map(event => {
    const box = node('article', '', 'evidence'); box.append(node('h3', `${event.kind} · event ${event.sequence ?? '?'}`), node('pre', pretty(event.payload))); return box;
  }) : [node('p', 'No recorded error or guard events. Absence of telemetry is not proof of success.', 'muted')]));
  el('evidence-timings').replaceChildren(...(e.timings.length ? e.timings.map(timing => {
    const row = node('div', '', 'timing'); row.append(node('span', `${timing.stage} #${timing.sequence ?? '?'} · ${timing.key}`), node('strong', `${timing.value} ${timing.key.endsWith('_seconds') ? 's' : 'ms'}`)); return row;
  }) : [node('p', 'No stage timings recorded.', 'muted')]));
  el('evidence-grades').replaceChildren(...[
    ['Official grade', e.official, 'Unknown — no official score imported.'],
    ['Scripted fixture grade', e.fixture, 'Not recorded. An offline fixture is not live acceptance.'],
    ['Jev / model assessment', e.jev, 'Not recorded. Model assessments are not official grades.'],
  ].map(([title, value, empty]) => { const box = node('article', '', 'evidence'); box.append(node('h3', title), value === null ? node('p', empty) : node('pre', pretty(value))); return box; }));
  el('trace').textContent = pretty(report);
}
async function startText() {
  if (session || !api.authenticated || fixtureBusy) return;
  const epoch = ++sessionEpoch, generation = api.generation;
  session = {kind: 'text', runId: null, busy: true}; lastSessionRun = null; notice(); controls();
  el('session-status').textContent = 'Starting chat…';
  el('talk-transcript').replaceChildren(node('p', 'Waiting for the model…', 'empty')); delete el('talk-transcript').dataset.signature;
  try {
    const result = await api.request('/api/sessions/text', {method: 'POST', body: {language: 'auto', mode: el('mode').value}, timeout: 60000});
    if (epoch !== sessionEpoch || generation !== api.generation) return;
    const report = acceptReport(result); session.runId = report.run_id; lastSessionRun = report.run_id; session.busy = false;
    renderSessionReport(result); el('session-status').textContent = 'Chat connected. Type your next message.';
  } catch (error) { if (epoch === sessionEpoch) { session = null; el('session-status').textContent = 'Session start failed. Check history before retrying; a timed-out request may still have spent quota.'; reportError(error); } }
  controls(); refreshAuthenticated();
}
function renderSessionReport(report) {
  renderTranscript(el('talk-transcript'), report);
  if (report.reply?.text && !transcriptRows(report).some(row => row.role === 'agent' && row.text === report.reply.text)) {
    const box = node('article', '', 'turn agent'); box.append(node('strong', 'Agent · text reply'), node('p', report.reply.text)); el('talk-transcript').append(box);
  }
}
async function startVoice() {
  if (session || fixtureBusy || (!api.authenticated && health?.public_voice_ready !== true)) return;
  session = {kind: 'voice', runId: null}; lastSessionRun = null; ++sessionEpoch; notice(); controls();
  el('talk-transcript').replaceChildren(node('p', 'Connecting your microphone…', 'empty')); delete el('talk-transcript').dataset.signature;
  try { await voice.start({language: 'auto', mode: api.authenticated ? el('mode').value : 'practice'}); }
  catch (error) { reportError(error); }
  controls();
}
async function endSession() {
  if (!session || session.ending) return;
  const active = session;
  if (active.kind === 'voice') { voice.stop('Call ended. Microphone and playback released.'); return; }
  if (!active.runId) { notice('Text session is still starting. Wait for its run ID, then end it; do not start another paid session.', true); return; }
  active.ending = true; controls(); el('session-status').textContent = 'Ending text session…';
  const epoch = sessionEpoch;
  try {
    await api.request(`/api/sessions/${encodeURIComponent(active.runId)}`, {method: 'DELETE', timeout: 15000});
    if (epoch === sessionEpoch) { session = null; sessionEpoch++; el('session-status').textContent = 'Text session ended. Its evidence remains in history.'; }
  } catch (error) { if (epoch === sessionEpoch) { active.ending = false; el('session-status').textContent = 'Could not confirm session end. Retry End session; do not create another.'; reportError(error); } }
  controls(); refreshAuthenticated();
}

el('auth-form').addEventListener('submit', async event => {
  event.preventDefault(); const token = el('token').value; el('token').value = ''; api.unlock(token); notice();
  el('unlock').disabled = true;
  try { await api.request('/api/runs?limit=1'); controls(); await refreshAuthenticated(); }
  catch (error) { api.lock(); clearWorkspace(); reportError(error); }
  finally { el('unlock').disabled = false; }
});
el('logout').addEventListener('click', async () => {
  el('logout').disabled = true;
  await endSession();
  const unconfirmed = Boolean(session?.kind === 'text');
  api.lock(); clearWorkspace(); el('logout').disabled = false;
  notice(unconfirmed ? 'Workspace locked. Text session closure was not confirmed; ask the operator to check the active session before starting another.' : 'Workspace locked. Private data and recordings cleared.', unconfirmed);
});
el('refresh').addEventListener('click', () => { notice(); refreshHealth(); refreshAuthenticated(); });
el('run-filter').addEventListener('change', renderHistory);
el('load-more').addEventListener('click', async () => {
  if (!history.cursor || moreBusy) return;
  const generation = api.generation; moreBusy = true; controls();
  try { const page = await api.request(`/api/runs?limit=50&before=${encodeURIComponent(history.cursor)}`); if (generation === api.generation) { history.merge(page, true); renderHistory(); } }
  catch (error) { if (generation === api.generation) reportError(error); }
  finally { moreBusy = false; controls(); }
});
el('start-text').addEventListener('click', startText);
el('start-voice').addEventListener('click', startVoice);
el('mute-voice').addEventListener('click', () => voice.setMuted(!voice.muted));
el('stop-session').addEventListener('click', endSession);
el('global-stop').addEventListener('click', endSession);
el('inspect-session').addEventListener('click', () => selectRun(lastSessionRun));
el('turn-form').addEventListener('submit', async event => {
  event.preventDefault(); const text = el('turn').value.trim();
  if (!text || session?.kind !== 'text' || !session.runId || session.busy || session.ending) return;
  const active = session, epoch = sessionEpoch; active.busy = true; controls(); notice();
  el('session-status').textContent = 'Waiting for the agent…';
  try {
    const result = await api.request(`/api/sessions/${encodeURIComponent(active.runId)}/turn`, {method: 'POST', body: {text}, timeout: 60000});
    if (epoch !== sessionEpoch) return;
    renderSessionReport(acceptReport(result)); el('turn').value = ''; el('session-status').textContent = 'Reply received. Continue or end the session.';
  } catch (error) { if (epoch === sessionEpoch) { el('session-status').textContent = 'Turn failed or timed out. Check the transcript before resending; do not duplicate a paid action.'; reportError(error); } }
  finally { if (epoch === sessionEpoch) active.busy = false; controls(); }
  refreshAuthenticated(); if (!el('turn').disabled) el('turn').focus();
});
el('fixture').addEventListener('click', async () => {
  if (!api.authenticated || fixtureBusy || session) return;
  const generation = api.generation; fixtureBusy = true; controls(); notice(); el('fixture-status').textContent = 'Running offline fixture…';
  try {
    const fixture = await api.request(`/api/demo?language=${encodeURIComponent(el('language').value)}`);
    const report = await api.request('/api/rehearse', {method: 'POST', body: fixture});
    if (generation !== api.generation) return;
    acceptReport(report); el('fixture-status').textContent = 'Fixture finished. Official score remains unknown. No paid provider calls.';
    await selectRun(report.run_id); refreshAuthenticated();
  } catch (error) { if (generation === api.generation) { el('fixture-status').textContent = 'Fixture failed. Check authentication and backend readiness.'; reportError(error); } }
  finally { fixtureBusy = false; controls(); }
});
const tabs = [...document.querySelectorAll('[data-evidence]')];
function switchEvidence(tab) {
  for (const button of tabs) {
    const active = button === tab; button.setAttribute('aria-selected', String(active)); button.tabIndex = active ? 0 : -1;
    el(`evidence-${button.dataset.evidence}`).hidden = !active;
  }
}
for (const tab of tabs) {
  tab.addEventListener('click', () => switchEvidence(tab));
  tab.addEventListener('keydown', event => {
    const index = tabs.indexOf(tab); let target;
    if (event.key === 'ArrowRight') target = tabs[(index + 1) % tabs.length];
    if (event.key === 'ArrowLeft') target = tabs[(index + tabs.length - 1) % tabs.length];
    if (event.key === 'Home') target = tabs[0]; if (event.key === 'End') target = tabs.at(-1);
    if (target) { event.preventDefault(); switchEvidence(target); target.focus(); }
  });
}
window.addEventListener('hashchange', route);
window.addEventListener('pagehide', () => { api.lock(); clearWorkspace(); });
window.addEventListener('pageshow', event => { if (event.persisted) { controls(); refreshHealth(); } });
window.addEventListener('beforeunload', event => { if (session) { event.preventDefault(); event.returnValue = ''; } });
document.addEventListener('visibilitychange', () => { if (!document.hidden) { refreshHealth(); refreshAuthenticated(); } });
route(); controls(); renderHistory(); refreshHealth();
setInterval(() => { if (!document.hidden || session) refreshAuthenticated(); }, 5000);
setInterval(() => { if (!document.hidden) refreshHealth(); }, 30000);
