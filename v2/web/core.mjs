export class ApiError extends Error {
  constructor(status, message) { super(message); this.name = 'ApiError'; this.status = status; }
}
export const validRunId = value => typeof value === 'string' && /^[A-Za-z0-9_-]{1,80}$/.test(value);
export function runPath(id) {
  if (!validRunId(id)) throw new ApiError(0, 'The server returned an invalid run identifier.');
  return `/api/runs/${encodeURIComponent(id)}`;
}
export function apiMessage(status) {
  return ({401: 'Operator authentication expired or was rejected. Unlock with a valid token.',
    402: 'The calling allowance has been reached. Contact the operator before starting another call.',
    403: 'This operation is disabled. Ask the operator to check paid-provider and practice permissions.',
    404: 'This resource is unavailable. Refresh the history; recordings may not exist for this run.',
    409: 'A session or action is already in progress. End it before starting another.',
    422: 'The request was rejected. Check the language, mode and message length.',
    429: 'The session limit or budget allowance was reached. Wait, or reconcile the budget with the operator.',
    503: 'The backend is not ready. Check readiness, provider configuration and available session capacity.'})[status]
    || `Request failed (HTTP ${status}). Check backend diagnostics and readiness; do not blindly retry paid requests.`;
}
export class OperatorAPI {
  #token = ''; #generation = 0; #requests = new Set();
  constructor(fetcher = globalThis.fetch, onUnauthorized = () => {}) { this.fetcher = fetcher; this.onUnauthorized = onUnauthorized; }
  get authenticated() { return Boolean(this.#token); }
  get generation() { return this.#generation; }
  unlock(token) { this.lock(); this.#token = String(token).trim(); }
  lock() {
    this.#token = ''; this.#generation++;
    for (const controller of this.#requests) controller.abort();
    this.#requests.clear();
  }
  async request(path, {method = 'GET', body, publicRequest = false, blob = false, timeout = 30000} = {}) {
    if (!/^\/(?:api\/|health$)/.test(path) || path.includes('#')) throw new ApiError(0, 'Invalid API path.');
    if (!publicRequest && !this.#token) throw new ApiError(401, 'Unlock the workspace first.');
    const generation = this.#generation;
    const controller = new AbortController(); this.#requests.add(controller);
    const timer = setTimeout(() => controller.abort(), timeout);
    const headers = publicRequest ? {} : {'X-V2-Token': this.#token};
    headers['ngrok-skip-browser-warning'] = '1';
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    try {
      const response = await this.fetcher(path, {method, headers, body: body === undefined ? undefined : JSON.stringify(body),
        signal: controller.signal, cache: 'no-store', credentials: 'omit', redirect: 'error', referrerPolicy: 'no-referrer'});
      if (generation !== this.#generation) throw new ApiError(0, 'Request cancelled.');
      if (!response.ok) {
        if (response.status === 401 && !publicRequest) { this.lock(); this.onUnauthorized(); }
        throw new ApiError(response.status, apiMessage(response.status));
      }
      const contentType = response.headers?.get('content-type')?.split(';')[0].trim().toLowerCase();
      if (contentType === 'text/html' || contentType === 'application/xhtml+xml') throw new ApiError(0, 'The server returned an HTML page instead of API data. Refresh this page; if it persists, check the public tunnel URL.');
      const result = response.status === 204 ? null : await (blob ? response.blob() : response.json());
      if (generation !== this.#generation) throw new ApiError(0, 'Request cancelled.');
      return result;
    } catch (error) {
      if (error instanceof ApiError) throw error;
      throw new ApiError(0, controller.signal.aborted ? 'Request cancelled or timed out. Check the run before retrying a paid action.' :
        'Cannot reach the backend or read its response. Check the server connection and readiness.');
    } finally { clearTimeout(timer); this.#requests.delete(controller); }
  }
}
export function budgetView(value = {}) {
  const number = key => Number.isFinite(value[key]) ? value[key] : null;
  const committed = number('committed_microusd');
  const actual = number('reported_microusd');
  return {actual, reserved: committed !== null && actual !== null ? Math.max(0, committed - actual) : null,
    remaining: number('remaining_microusd'), cap: number('cap_microusd')};
}
export function money(microusd) { return Number.isFinite(microusd) ? new Intl.NumberFormat('en-US', {style: 'currency', currency: 'USD', maximumFractionDigits: 4}).format(microusd / 1e6) : 'Unknown'; }
export function count(value) { return Number.isFinite(value) ? String(value) : 'Unknown'; }
export function audioLabel(status) {
  return ({signal_sent: 'Non-silent socket output', non_silent: 'Non-silent socket output', audible: 'Non-silent socket output', sent: 'Socket output sent',
    silent: 'Silent socket output', silent_audio: 'Silent socket output', no_output: 'No socket audio',
    not_applicable: 'Not applicable (text)', not_measured: 'Unknown / not measured', unknown: 'Unknown / not measured'})[status] || 'Unknown / not measured';
}
export function officialLabel(value) {
  if (value === null || value === undefined) return 'Unknown';
  if (typeof value === 'object') return 'Imported result — inspect evidence';
  return String(value);
}
export function matchesFilter(run, filter) {
  const m = run.metrics || {};
  if (filter === 'errors') return Number(m.errors) > 0;
  if (filter === 'silent') return ['silent', 'silent_audio', 'no_output'].includes(m.audio_status);
  if (filter === 'unknown') return audioLabel(m.audio_status) === 'Unknown / not measured';
  if (filter === 'missing') return m.missing_submission === true;
  return true;
}
export function transcriptRows(report = {}) {
  return (report.events || []).filter(e => ['caller_turn', 'response_planned'].includes(e.kind)).map(e => ({
    key: e.event_id || `${e.sequence}:${e.kind}`, role: e.kind === 'caller_turn' ? 'caller' : 'agent',
    label: e.kind === 'caller_turn' ? 'Caller' : 'Agent · planned response', text: String(e.payload?.text ?? ''), timestamp: e.timestamp || '',
  }));
}
export function evidence(report = {}) {
  const events = report.events || [];
  const latest = kind => events.filter(e => e.kind === kind).at(-1)?.payload;
  return {
    actions: events.filter(e => e.kind === 'action_receipt'),
    errors: events.filter(e => ['error', 'provider_error', 'pipeline_error', 'guard_rejected'].includes(e.kind)),
    timings: events.flatMap(e => {
      const payload = e.payload || {};
      const rows = Object.entries(payload).filter(([key, value]) => /(?:_ms|_seconds)$/.test(key) && Number.isFinite(value))
        .map(([key, value]) => ({stage: e.kind, sequence: e.sequence, key, value}));
      if (e.kind === 'pipeline_metric' && ['TTFBMetricsData', 'ProcessingMetricsData'].includes(payload.metric) && Number.isFinite(payload.value)) {
        rows.push({stage: `${payload.processor || 'pipeline'} · ${payload.metric}`, sequence: e.sequence, key: 'duration_seconds', value: payload.value});
      }
      return rows;
    }),
    fixture: report.fixture_grade ?? latest('fixture_grade') ?? null,
    jev: report.model_assessment ?? report.jev_assessment ?? latest('model_assessment') ?? latest('jev_assessment') ?? latest('jev_evaluation') ?? null,
    official: report.official_grade ?? null,
  };
}
export class RunHistory {
  constructor() { this.clear(); }
  clear() { this.runs = new Map(); this.cursor = null; this.loadedOlder = false; this.generation = (this.generation || 0) + 1; }
  merge(page, older = false) {
    if (!page || !Array.isArray(page.runs)) throw new ApiError(0, 'History response has an unexpected format.');
    for (const run of page.runs) if (validRunId(run.run_id)) this.runs.set(run.run_id, run);
    if ((!older && !this.loadedOlder) || older) this.cursor = typeof page.next_cursor === 'string' ? page.next_cursor : null;
    if (older) this.loadedOlder = true;
  }
  list() { return [...this.runs.values()].sort((a, b) => String(b.created_at || '').localeCompare(String(a.created_at || '')) || b.run_id.localeCompare(a.run_id)); }
}
export class RecordingCache {
  constructor(api, urls = globalThis.URL) { this.api = api; this.urls = urls; this.runId = null; this.generation = 0; this.entries = new Map(); }
  select(runId) {
    if (this.runId === runId) return false;
    this.clear(); this.runId = runId; return true;
  }
  clear() {
    this.generation++;
    for (const entry of this.entries.values()) if (entry.url) this.urls.revokeObjectURL(entry.url);
    this.entries.clear(); this.runId = null;
  }
  async load(track) {
    if (!['inbound', 'outbound'].includes(track) || !this.runId) throw new ApiError(0, 'Select a run and audio track first.');
    if (this.entries.has(track)) return this.entries.get(track).promise;
    const generation = this.generation;
    const entry = {};
    entry.promise = (async () => {
      const blob = await this.api.request(`${runPath(this.runId)}/audio/${track}`, {blob: true});
      if (generation !== this.generation) return null;
      if (!blob.size || !/^audio\//i.test(blob.type)) throw new ApiError(0, 'No playable audio was returned for this track.');
      entry.url = this.urls.createObjectURL(blob);
      return entry.url;
    })().catch(error => { if (generation === this.generation) this.entries.delete(track); throw error; });
    this.entries.set(track, entry); return entry.promise;
  }
}
export function textElement(doc, tag, text, className = '') {
  const node = doc.createElement(tag); node.textContent = String(text ?? '');
  if (className) node.className = className;
  return node;
}
