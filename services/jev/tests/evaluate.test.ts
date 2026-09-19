import assert from 'node:assert/strict';
import { test } from 'node:test';
import { makeHandler, type Evaluator } from '../lib/evaluate.ts';

const body = { schema_version: 1, run_id: 'fixture', question_pack: 'conversation-v1',
               turns: [{ role: 'caller', text: 'Please book an appointment' }] };
const answers = { unanswered_request: { type: 'boolean', probability: 0.9 },
                  repeated_question: { type: 'boolean', probability: 0.1 },
                  premature_success: { type: 'boolean', probability: 0.2 } };
const request = (value: unknown = body, token = 'test-token') => new Request('https://service.test/api/evaluate', {
  method: 'POST', headers: { authorization: `Bearer ${token}` }, body: JSON.stringify(value),
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
