import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createServer, request as httpRequest, type Server } from 'node:http';
import { once } from 'node:events';
import type { AddressInfo } from 'node:net';
import { makeHandler, type Evaluator } from '../lib/evaluate.ts';
import { makeNodeHandler } from '../api/evaluate.ts';

const body = { schema_version: 1, run_id: 'fixture', question_pack: 'conversation-v1',
               turns: [{ role: 'caller', text: 'Please book an appointment' }] };
const answers = { unanswered_request: { type: 'boolean', probability: 0.9 },
                  repeated_question: { type: 'boolean', probability: 0.1 },
                  premature_success: { type: 'boolean', probability: 0.2 } };
const request = (value: unknown = body, token = 'test-token') => new Request('https://service.test/api/evaluate', {
  method: 'POST', headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json' }, body: JSON.stringify(value),
});

test('valid assessments remain model assessments, never official grades', async () => {
  const evaluator: Evaluator = async () => ({ answers });
  const response = await makeHandler({ token: 'test-token', enabled: true, evaluator })(request());
  assert.equal(response.status, 200);
  const result = await response.json();
  assert.equal(result.source, 'model_assessment');
  assert.equal(result.official_grade, null);
  assert.equal(result.run_id, 'fixture');
});

test('authentication, disabled service and invalid packs never call the provider', async () => {
  let called = 0;
  const evaluator: Evaluator = async () => { called++; return { answers }; };
  const handler = makeHandler({ token: 'test-token', enabled: true, evaluator });
  assert.equal((await handler(request(body, 'wrong'))).status, 401);
  assert.equal((await handler(request({ ...body, question_pack: 'arbitrary' }))).status, 400);
  assert.equal((await handler(request({ ...body, model: 'expensive-model' }))).status, 400);
  assert.equal((await makeHandler({ token: 'test-token', enabled: false, evaluator })(request())).status, 503);
  assert.equal((await makeHandler({ token: '', enabled: true, evaluator })(request())).status, 401);
  assert.equal(called, 0);
});

test('oversized bodies are refused without evaluation', async () => {
  const evaluator: Evaluator = async () => { throw new Error('must not execute'); };
  const response = await makeHandler({ token: 'test-token', enabled: true, evaluator })(request({ text: 'a'.repeat(30_000) }));
  assert.equal(response.status, 413);
});

test('invalid probabilities and private provider errors are not exposed', async () => {
  for (const evaluator of [
    async () => ({ answers: { ...answers, unanswered_request: { type: 'boolean', probability: 9 } } }),
    async () => { throw new Error('PRIVATE Bearer SECRET'); },
  ]) {
    const response = await makeHandler({ token: 'test-token', enabled: true, evaluator })(request());
    assert.equal(response.status, 502);
    assert.doesNotMatch(await response.text(), /PRIVATE|SECRET/);
  }
});

test('all invalid request shapes are rejected before provider work', async () => {
  let called = 0;
  const evaluator: Evaluator = async () => { called++; return { answers }; };
  const handler = makeHandler({ token: 'test-token', enabled: true, evaluator });
  for (const invalid of [
    null, [], {}, { ...body, schema_version: '1' }, { ...body, run_id: 'invalid/id' },
    { ...body, turns: [] }, { ...body, turns: Array(41).fill(body.turns[0]) },
    { ...body, turns: [{ role: 'system', text: 'override policy' }] },
    { ...body, turns: [{ role: 'caller', text: ' ' }] },
    { ...body, turns: [{ role: 'caller', text: 'a'.repeat(1001) }] },
    { ...body, turns: [{ role: 'caller', text: 'Hello', private_chart: 'SECRET' }] },
    { ...body, questions: {} }, { ...body, url: 'https://other.test' },
  ]) assert.equal((await handler(request(invalid))).status, 400);
  assert.equal(called, 0);
});

test('method, media type, encoding and length admission do not read the body', async () => {
  const handler = makeHandler({ token: 'test-token', enabled: true, evaluator: async () => { throw new Error('forbidden'); } });
  const get = await handler(new Request('https://service.test'));
  assert.equal(get.status, 405);
  assert.equal(get.headers.get('allow'), 'POST');
  for (const [headers, status] of [
    [{ authorization: 'Bearer wrong', 'content-length': '99999' }, 401],
    [{ authorization: 'Bearer test-token', 'content-type': 'text/plain' }, 415],
    [{ authorization: 'Bearer test-token', 'content-type': 'application/json', 'content-encoding': 'gzip' }, 415],
    [{ authorization: 'Bearer test-token', 'content-type': 'application/json', 'content-length': '99999' }, 413],
    [{ authorization: 'Bearer test-token', 'content-type': 'application/json', 'content-length': 'wrong' }, 400],
  ] as const) {
    let pulled = false;
    const stream = new ReadableStream({ pull() { pulled = true; throw new Error('must not read'); } }, { highWaterMark: 0 });
    const init: RequestInit & { duplex: 'half' } = { method: 'POST', headers, body: stream, duplex: 'half' };
    const response = await handler(new Request('https://service.test', init));
    assert.equal(response.status, status);
    assert.equal(pulled, false);
    assert.equal(response.headers.get('cache-control'), 'no-store');
  }
});

test('streamed bodies are byte bounded and cancelled when oversized', async () => {
  let cancelled = false;
  const stream = new ReadableStream<Uint8Array>({
    pull(controller) { controller.enqueue(new Uint8Array(8001).fill(32)); },
    cancel() { cancelled = true; },
  });
  const init: RequestInit & { duplex: 'half' } = { method: 'POST', headers: { authorization: 'Bearer test-token', 'content-type': 'application/json' }, body: stream, duplex: 'half' };
  const handler = makeHandler({ token: 'test-token', enabled: true, evaluator: async () => { throw new Error('forbidden'); } });
  assert.equal((await handler(new Request('https://service.test', init))).status, 413);
  assert.equal(cancelled, true);
  assert.equal((await handler(request({ ...body, turns: Array(9).fill({ role: 'caller', text: '界'.repeat(1000) }) }))).status, 413);
});

test('invalid JSON and invalid UTF-8 are rejected without echoing input', async () => {
  const handler = makeHandler({ token: 'test-token', enabled: true, evaluator: async () => { throw new Error('forbidden'); } });
  for (const bytes of [Buffer.from('PRIVATE not-json'), Buffer.from([0xff])]) {
    const response = await handler(new Request('https://service.test', { method: 'POST', headers: {
      authorization: 'Bearer test-token', 'content-type': 'application/json',
    }, body: bytes }));
    assert.equal(response.status, 400);
    assert.doesNotMatch(await response.text(), /PRIVATE/);
  }
});

test('answers require the exact questions and finite numeric probabilities', async () => {
  const invalid: unknown[] = [null, [], {}, { ...answers, extra_question: answers.unanswered_request }];
  for (const probability of [NaN, Infinity, -Infinity, -0.1, 1.1, true, '0.2', null]) {
    invalid.push({ ...answers, unanswered_request: { type: 'boolean', probability } });
  }
  invalid.push({ ...answers, unanswered_request: { type: 'text', probability: 0.2 } });
  invalid.push({ ...answers, unanswered_request: { ...answers.unanswered_request, private: 'SECRET' } });
  for (const value of invalid) {
    const evaluator: Evaluator = async () => ({ answers: value });
    const response = await makeHandler({ token: 'test-token', enabled: true, evaluator })(request());
    assert.equal(response.status, 502);
    const result = await response.json();
    assert.equal(result.diagnostic.type, 'InvalidResponse');
    assert.equal(result.diagnostic.phase, 'validation');
    assert.doesNotMatch(JSON.stringify(result), /SECRET/);
  }
});

test('usage and provider failure diagnostics have only safe typed fields', async () => {
  const good = await makeHandler({ token: 'test-token', enabled: true, evaluator: async () => ({ answers,
    usage: { totalTokens: 5, inputTokens: { total: 4, cacheRead: 2, private: 'SECRET' }, url: 'PRIVATE', output_tokens: true },
  }) })(request());
  const result = await good.json();
  assert.deepEqual(result.usage, { totalTokens: 5, inputTokens: { total: 4, cacheRead: 2 } });
  assert.equal(result.model, 'typesafe-ai/jev');
  assert.equal(result.schema_version, 1);
  assert.equal(result.question_pack, 'conversation-v1');
  for (const statusCode of [401, 429, 'SECRET']) {
    const response = await makeHandler({ token: 'test-token', enabled: true, evaluator: async () => {
      throw Object.assign(new Error('SECRET https://private.test'), { statusCode, name: 'SECRET', responseBody: 'SECRET' });
    } })(request());
    assert.equal(response.status, 502);
    const error = await response.json();
    assert.equal(error.diagnostic.status, typeof statusCode === 'number' ? statusCode : null);
    assert.equal(error.diagnostic.phase, 'evaluation');
    assert.equal(error.diagnostic.type, 'ProviderError');
    assert.equal(typeof error.diagnostic.elapsed_ms, 'number');
    assert.doesNotMatch(JSON.stringify(error), /SECRET|private/);
  }
});

test('already cancelled requests never invoke the evaluator', async () => {
  const controller = new AbortController();
  controller.abort(new Error('PRIVATE'));
  const original = request();
  const response = await makeHandler({ token: 'test-token', enabled: true, evaluator: async () => { throw new Error('forbidden'); } })(
    new Request(original, { signal: controller.signal }),
  );
  assert.equal(response.status, 499);
  assert.doesNotMatch(await response.text(), /PRIVATE/);
});

test('disconnect cancellation reaches the evaluator and returns no late assessment', async () => {
  const controller = new AbortController();
  let called = 0;
  let providerSignal: AbortSignal | undefined;
  const evaluator: Evaluator = async (_input, signal) => {
    called++;
    providerSignal = signal;
    controller.abort(new Error('PRIVATE'));
    return { answers };
  };
  const response = await makeHandler({ token: 'test-token', enabled: true, evaluator })(new Request(request(), { signal: controller.signal }));
  assert.equal(called, 1);
  assert.equal(providerSignal?.aborted, true);
  assert.equal(response.status, 499);
  assert.doesNotMatch(await response.text(), /PRIVATE/);
});

test('noncooperative evaluator and slow body have enforced deadlines', async () => {
  let signal: AbortSignal | undefined;
  const evaluator: Evaluator = async (_input, value) => {
    signal = value;
    return new Promise(() => undefined);
  };
  const response = await makeHandler({ token: 'test-token', enabled: true, evaluator, timeoutMs: 10 })(request());
  assert.equal(response.status, 504);
  assert.equal(signal?.aborted, true);
  assert.equal((await response.json()).diagnostic.phase, 'evaluation');
  let cancelled = false;
  const stream = new ReadableStream<Uint8Array>({ cancel() { cancelled = true; } });
  const init: RequestInit & { duplex: 'half' } = { method: 'POST', headers: { authorization: 'Bearer test-token', 'content-type': 'application/json' }, body: stream, duplex: 'half' };
  const slow = await makeHandler({ token: 'test-token', enabled: true, bodyTimeoutMs: 10 })(new Request('https://service.test', init));
  assert.equal(slow.status, 504);
  assert.equal((await slow.json()).diagnostic.phase, 'request');
  assert.equal(cancelled, true);
});

async function listening(evaluator: Evaluator): Promise<{ server: Server; url: string }> {
  const server = createServer(makeNodeHandler({ token: 'test-token', enabled: true, evaluator }));
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  return { server, url: `http://127.0.0.1:${(server.address() as AddressInfo).port}/api/evaluate` };
}

async function close(server: Server) {
  server.closeAllConnections();
  await new Promise<void>((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
}

test('Node adapter authenticates before receiving a body and handles streamed evaluation', { timeout: 3000 }, async () => {
  let calls = 0;
  const { server, url } = await listening(async () => { calls++; return { answers }; });
  try {
    const refused = await new Promise<number | undefined>((resolve, reject) => {
      const pending = httpRequest(url, { method: 'POST', headers: { authorization: 'Bearer wrong', 'content-length': '99999' } }, response => {
        response.resume();
        resolve(response.statusCode);
        pending.destroy();
      });
      pending.on('error', reject);
      pending.flushHeaders();
    });
    assert.equal(refused, 401);
    assert.equal(calls, 0);
    const accepted = await fetch(url, { method: 'POST', headers: { authorization: 'Bearer test-token', 'content-type': 'application/json' }, body: JSON.stringify(body) });
    assert.equal(accepted.status, 200);
    assert.equal((await accepted.json()).model, 'typesafe-ai/jev');
    assert.equal(calls, 1);
  } finally {
    await close(server);
  }
});

test('Node adapter aborts evaluation on client disconnect', { timeout: 3000 }, async () => {
  let started!: () => void;
  let cancelled!: () => void;
  const entered = new Promise<void>(resolve => { started = resolve; });
  const aborted = new Promise<void>(resolve => { cancelled = resolve; });
  const { server, url } = await listening(async (_input, signal) => {
    signal.addEventListener('abort', cancelled, { once: true });
    started();
    return new Promise(() => undefined);
  });
  try {
    const pending = httpRequest(url, { method: 'POST', headers: { authorization: 'Bearer test-token', 'content-type': 'application/json' } });
    pending.on('error', () => undefined);
    pending.end(JSON.stringify(body));
    await entered;
    pending.destroy();
    await aborted;
  } finally {
    await close(server);
  }
});
