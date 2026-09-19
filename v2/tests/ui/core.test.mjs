// Offline only. Mocked HTTP is not provider or official acceptance evidence.
import test from 'node:test';
import assert from 'node:assert/strict';
import {OperatorAPI, ApiError, apiMessage, RunHistory, RecordingCache, validRunId, runPath, budgetView,
  money, audioLabel, officialLabel, matchesFilter, evidence, transcriptRows, textElement} from '../../web/core.mjs';

const response = value => ({ok: true, status: 200, json: async () => value});
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return {promise, resolve}; };

test('operator credential exists only in an authenticated request header; health is public', async () => {
  const seen = [], api = new OperatorAPI(async (path, options) => { seen.push({path, options}); return response({}); });
  await assert.rejects(api.request('/api/budget'), {status: 401});
  api.unlock('test-memory-only');
  await api.request('/health', {publicRequest: true}); await api.request('/api/budget');
  assert.equal(seen[0].options.headers['X-V2-Token'], undefined);
  assert.equal(seen[1].options.headers['X-V2-Token'], 'test-memory-only');
  assert.equal(seen[1].path, '/api/budget');
  assert.equal(seen[1].options.cache, 'no-store'); assert.equal(seen[1].options.redirect, 'error');
  assert.equal(seen[1].options.credentials, 'omit');
  assert.doesNotMatch(JSON.stringify(api), /test-memory-only/);
  api.lock(); assert.equal(api.authenticated, false);
  await assert.rejects(api.request('/api/budget'), {status: 401});
});

test('401 locks and invalidates in-flight requests without reflecting server bodies', async () => {
  let locked = 0;
  const api = new OperatorAPI(async () => ({ok: false, status: 401, json: () => { throw Error('must not read secrets'); }}), () => locked++);
  api.unlock('private');
  await assert.rejects(api.request('/api/runs?limit=50'), error => error instanceof ApiError && error.status === 401 && !error.message.includes('private'));
  assert.equal(locked, 1); assert.equal(api.authenticated, false);
});

test('locking aborts pending fetch and rejects late results even if fetch ignores abort', async () => {
  const pending = deferred(); let signal;
  const api = new OperatorAPI(async (_path, options) => { signal = options.signal; return pending.promise; });
  api.unlock('private'); const request = api.request('/api/budget'); api.lock();
  assert.equal(signal.aborted, true); pending.resolve(response({private: 'data'}));
  await assert.rejects(request, /cancelled/i);
});

test('late response body cannot restore data after logout', async () => {
  const body = deferred(); const api = new OperatorAPI(async () => ({ok: true, status: 200, json: () => body.promise}));
  api.unlock('private'); const result = api.request('/api/runs/one');
  await Promise.resolve(); api.lock(); body.resolve({run_id: 'one'});
  await assert.rejects(result, /cancelled/i);
});

test('write requests serialize JSON without tokens in the URL or body; DELETE supports 204', async () => {
  const api = new OperatorAPI(async (path, options) => {
    assert.equal(path, '/api/sessions/text'); assert.equal(options.method, 'POST');
    assert.deepEqual(JSON.parse(options.body), {language: 'ca', mode: 'practice'});
    assert.equal(options.headers['Content-Type'], 'application/json'); return response({run_id: 'mock'});
  });
  api.unlock('credential'); await api.request('/api/sessions/text', {method: 'POST', body: {language: 'ca', mode: 'practice'}});
  const ending = new OperatorAPI(async () => ({ok: true, status: 204})); ending.unlock('credential');
  assert.equal(await ending.request('/api/sessions/mock', {method: 'DELETE'}), null);
});

test('API path validation and safe actionable error messages', async () => {
  const api = new OperatorAPI(() => { throw Error('must not fetch'); }); api.unlock('private');
  for (const path of ['https://other.example/api', '//other.example/api', '/api/runs#token']) await assert.rejects(api.request(path), /Invalid API path/);
  for (const status of [401, 403, 404, 409, 422, 429, 503, 500]) assert.ok(apiMessage(status).length > 25);
  assert.equal(validRunId('../secrets'), false); assert.equal(validRunId('a?token=b'), false); assert.equal(validRunId('run_1-ab'), true);
  assert.throws(() => runPath('../bad')); assert.equal(runPath('run_1'), '/api/runs/run_1');
});

test('budget separates outstanding reservations from settled actual and leaves unknown unknown', () => {
  assert.deepEqual(budgetView({cap_microusd: 30000000, committed_microusd: 5000000, reported_microusd: 1200000, remaining_microusd: 25000000}),
    {cap: 30000000, reserved: 3800000, actual: 1200000, remaining: 25000000});
  assert.equal(budgetView({committed_microusd: 10}).reserved, null);
  assert.equal(budgetView({reported_microusd: 0}).actual, 0); assert.equal(money(null), 'Unknown'); assert.equal(money(0), '$0.00');
});

test('official, fixture, model, HTTP receipt and audio evidence remain distinct', () => {
  const report = {official_grade: null, events: [
    {kind: 'fixture_grade', payload: {passed: true}}, {kind: 'model_assessment', payload: {source: 'jev', answers: [true]}},
    {kind: 'action_receipt', payload: {source: 'simulation', accepted: true}},
    {kind: 'action_receipt', payload: {source: 'platform_receipt', accepted: true, http_status: 200}},
    {kind: 'audio_output', payload: {frames: 1}}, {kind: 'audio_output', payload: {frames: 100}},
  ]};
  const view = evidence(report);
  assert.equal(view.official, null); assert.equal(officialLabel(view.official), 'Unknown');
  assert.equal(view.fixture.passed, true); assert.equal(view.jev.source, 'jev'); assert.equal(view.actions.length, 2);
  assert.equal(audioLabel('signal_sent'), 'Non-silent socket output');
  assert.match(audioLabel(undefined), /Unknown/); assert.match(audioLabel('not_applicable'), /Not applicable/);
  assert.match(audioLabel('silent'), /Silent/); assert.equal(officialLabel(0), '0');
});

test('filters distinguish no receipt, errors, silent audio and unknown telemetry', () => {
  assert.equal(matchesFilter({metrics: {}}, 'missing'), false);
  assert.equal(matchesFilter({metrics: {missing_submission: true}}, 'missing'), true);
  assert.equal(matchesFilter({metrics: {}}, 'silent'), false);
  assert.equal(matchesFilter({metrics: {}}, 'unknown'), true);
  assert.equal(matchesFilter({metrics: {audio_status: 'not_applicable'}}, 'unknown'), false);
  assert.equal(matchesFilter({metrics: {audio_status: 'silent'}}, 'silent'), true);
  assert.equal(matchesFilter({metrics: {errors: 2}}, 'errors'), true);
});

test('transcript strings use textContent, never markup, and response text is labelled planned', () => {
  const hostile = '<img src=x onerror=alert(1)> & "patient"';
  const report = {events: [{event_id: 'a', kind: 'caller_turn', payload: {text: hostile}}, {event_id: 'b', kind: 'response_planned', payload: {text: '<script>bad</script>'}}, {kind: 'action_receipt', payload: {text: 'not a turn'}}]};
  const rows = transcriptRows(report); assert.equal(rows.length, 2); assert.equal(rows[0].text, hostile); assert.match(rows[1].label, /planned/);
  let assigned = '';
  const doc = {createElement: tag => ({tag, set textContent(value) { assigned = value; }, set innerHTML(_) { throw Error('unsafe'); }})};
  assert.equal(textElement(doc, 'p', rows[0].text).tag, 'p'); assert.equal(assigned, hostile);
});

test('timings and errors reflect actual events and preserve measured zero', () => {
  const e = evidence({events: [{kind: 'response_planned', sequence: 1, payload: {elapsed_ms: 0, text: 'ok'}}, {kind: 'error', payload: {type: 'TimeoutError'}}, {kind: 'guard_rejected', payload: {}}, {kind: 'provider_error', payload: {elapsed_ms: 123}}, {kind: 'pipeline_error', payload: {}}]});
  assert.equal(e.errors.length, 4); assert.equal(e.timings.length, 2); assert.equal(e.timings[0].value, 0); assert.equal(e.fixture, null); assert.equal(e.jev, null);
  const pipeline = evidence({events: [{kind: 'pipeline_metric', payload: {metric: 'TTFBMetricsData', processor: 'TTS', value: .12}}]});
  assert.equal(pipeline.timings[0].key, 'duration_seconds'); assert.equal(pipeline.timings[0].value, .12);
});

test('history polling merges IDs without losing loaded older pages or pagination cursor', () => {
  const h = new RunHistory();
  h.merge({runs: [{run_id: 'new', created_at: '2026-01-03', status: 'active'}], next_cursor: 'page2'});
  h.merge({runs: [{run_id: 'old', created_at: '2026-01-01'}], next_cursor: 'page3'}, true);
  h.merge({runs: [{run_id: 'new', created_at: '2026-01-03', status: 'completed'}], next_cursor: 'page2'});
  assert.equal(h.cursor, 'page3'); assert.deepEqual(h.list().map(r => r.run_id), ['new', 'old']); assert.equal(h.list()[0].status, 'completed');
  h.clear(); h.merge({runs: [{run_id: 'fresh'}], next_cursor: 'new-page2'}); assert.equal(h.cursor, 'new-page2'); assert.equal(h.list().length, 1);
  assert.throws(() => h.merge({}), /unexpected format/);
});

test('authorized recordings are fetched once and survive polling; selection/logout revoke blobs', async () => {
  let calls = 0; const revoked = [];
  const api = {request: async (path, options) => { calls++; assert.equal(path, '/api/runs/a/audio/outbound'); assert.equal(options.blob, true); return new Blob(['wav'], {type: 'audio/wav'}); }};
  const cache = new RecordingCache(api, {createObjectURL: () => 'blob:one', revokeObjectURL: value => revoked.push(value)});
  cache.select('a'); assert.equal(await cache.load('outbound'), 'blob:one');
  assert.equal(cache.select('a'), false); assert.equal(await cache.load('outbound'), 'blob:one'); assert.equal(calls, 1); assert.deepEqual(revoked, []);
  cache.select('b'); assert.deepEqual(revoked, ['blob:one']); cache.clear();
});

test('late recording fetch cannot attach a blob after selection or logout', async () => {
  const pending = deferred(); let created = 0;
  const cache = new RecordingCache({request: () => pending.promise}, {createObjectURL: () => { created++; return 'blob:late'; }, revokeObjectURL: () => {}});
  cache.select('a'); const result = cache.load('inbound'); cache.clear(); pending.resolve(new Blob(['wav'], {type: 'audio/wav'}));
  assert.equal(await result, null); assert.equal(created, 0);
});

test('missing recordings are retryable and non-audio responses are not played', async () => {
  let calls = 0;
  const cache = new RecordingCache({request: async () => { calls++; if (calls === 1) throw new ApiError(404, 'missing'); return new Blob(['wrong'], {type: 'text/html'}); }});
  cache.select('a'); await assert.rejects(cache.load('inbound'), {status: 404}); await assert.rejects(cache.load('inbound'), /No playable audio/); assert.equal(calls, 2);
});
