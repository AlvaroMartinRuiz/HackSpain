import { createHash, timingSafeEqual } from 'node:crypto';
import { experimental_evaluate as evaluate } from 'ai';
import { z } from 'zod';

export const MAX_BYTES = 24_000;
const Body = z.object({
  schema_version: z.literal(1),
  run_id: z.string().regex(/^[a-zA-Z0-9_-]{1,80}$/),
  question_pack: z.literal('conversation-v1'),
  turns: z.array(z.object({
    role: z.enum(['caller', 'agent']),
    text: z.string().min(1).max(1000).refine(value => value.trim().length > 0),
  }).strict()).min(1).max(40),
}).strict();

const BooleanAnswer = z.object({ type: z.literal('boolean'), probability: z.number().finite().min(0).max(1) }).strict();
const Answers = z.object({
  unanswered_request: BooleanAnswer,
  repeated_question: BooleanAnswer,
  premature_success: BooleanAnswer,
}).strict();

export const questions = {
  unanswered_request: {
    type: 'boolean',
    instructions: 'Does the caller express a request that the agent leaves unanswered or unresolved in this conversation?',
    criteria: { true: 'An expressed request remains unaddressed.', false: 'All expressed requests are addressed, or the conversation has not yet reached a conclusion.' },
  },
  repeated_question: {
    type: 'boolean',
    instructions: 'Does the agent unnecessarily ask for information the caller already supplied clearly?',
    criteria: { true: 'A redundant question, not a necessary correction or confirmation.', false: 'No redundant questions.' },
  },
  premature_success: {
    type: 'boolean',
    instructions: 'Does the agent claim an appointment action succeeded before obtaining the caller confirmation requested in the dialogue? Do not infer backend success from the text.',
    criteria: { true: 'A premature success claim is visible in the dialogue.', false: 'No such claim is visible.' },
  },
} as const;

type Input = z.infer<typeof Body>;
type Result = { answers: unknown; usage?: unknown };
export type Evaluator = (input: Input, signal: AbortSignal) => Promise<Result>;
export type HandlerOptions = { token: string; enabled: boolean; evaluator?: Evaluator; timeoutMs?: number; bodyTimeoutMs?: number };

export const runJev: Evaluator = async (input, signal) => evaluate({
  model: 'typesafe-ai/jev',
  state: { conversation: input.turns },
  questions,
  maxRetries: 0,
  abortSignal: signal,
});

function authorized(offered: string | null, expected: string): boolean {
  if (!expected || !offered?.startsWith('Bearer ') || offered.length > 4096) return false;
  const hash = (text: string) => createHash('sha256').update(text).digest();
  return timingSafeEqual(hash(offered.slice(7)), hash(expected));
}

export function json(body: unknown, status: number): Response {
  return Response.json(body, { status, headers: { 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff' } });
}

export function admission(request: Pick<Request, 'method' | 'headers'>, options: HandlerOptions): Response | null {
  if (request.method !== 'POST') {
    const response = json({ error: 'POST required' }, 405);
    response.headers.set('Allow', 'POST');
    return response;
  }
  if (!authorized(request.headers.get('authorization'), options.token)) return json({ error: 'unauthorized' }, 401);
  if (!options.enabled) return json({ error: 'paid evaluation disabled' }, 503);
  if (request.headers.get('content-type')?.split(';')[0].trim().toLowerCase() !== 'application/json') {
    return json({ error: 'application/json required' }, 415);
  }
  const encoding = request.headers.get('content-encoding');
  if (encoding && encoding !== 'identity') return json({ error: 'unsupported content encoding' }, 415);
  const length = request.headers.get('content-length');
  if (length !== null) {
    if (!/^\d+$/.test(length)) return json({ error: 'invalid content length' }, 400);
    if (Number(length) > MAX_BYTES) return json({ error: 'request too large' }, 413);
  }
  return null;
}

function deadline(parent: AbortSignal, ms: number) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(new DOMException('Deadline exceeded', 'TimeoutError')), ms);
  return { signal: AbortSignal.any([parent, controller.signal]), clear: () => clearTimeout(timer) };
}

function abortable<T>(operation: () => Promise<T>, signal: AbortSignal): Promise<T> {
  signal.throwIfAborted();
  return new Promise<T>((resolve, reject) => {
    const aborted = () => {
      signal.removeEventListener('abort', aborted);
      reject(signal.reason);
    };
    signal.addEventListener('abort', aborted, { once: true });
    Promise.resolve().then(() => {
      signal.throwIfAborted();
      return operation();
    }).then(resolve, reject).finally(() => signal.removeEventListener('abort', aborted));
  });
}

function usage(value: unknown): Record<string, unknown> | null {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return null;
  const result: Record<string, unknown> = {};
  const count = (number: unknown): number is number => typeof number === 'number' && Number.isSafeInteger(number) && number >= 0 && number <= 1_000_000_000;
  for (const key of ['prompt_tokens', 'completion_tokens', 'total_tokens', 'input_tokens', 'output_tokens', 'inputTokens', 'outputTokens', 'totalTokens']) {
    const number = (value as Record<string, unknown>)[key];
    if (count(number)) result[key] = number;
    else if (['inputTokens', 'outputTokens'].includes(key) && typeof number === 'object' && number !== null && !Array.isArray(number)) {
      result[key] = Object.fromEntries(Object.entries(number).filter(([name, item]) =>
        ['total', 'noCache', 'cacheRead', 'cacheWrite', 'text', 'reasoning'].includes(name) && count(item)));
    }
  }
  return result;
}

function failure(request: Request, signal: AbortSignal, phase: string, started: number, error?: unknown): Response {
  const cancelled = request.signal.aborted;
  const timeout = !cancelled && signal.aborted;
  const status = typeof error === 'object' && error !== null && 'statusCode' in error ? error.statusCode : null;
  return json({ error: cancelled ? 'evaluation cancelled' : timeout ? 'evaluation timed out' : 'evaluation unavailable',
    diagnostic: { type: cancelled ? 'CancelledError' : timeout ? 'TimeoutError' : phase === 'validation' ? 'InvalidResponse' : 'ProviderError',
      phase, status: typeof status === 'number' && Number.isInteger(status) && status >= 400 && status <= 599 ? status : null,
      elapsed_ms: Math.max(0, Math.round(performance.now() - started)) } }, cancelled ? 499 : timeout ? 504 : 502);
}

export function makeHandler(options: HandlerOptions) {
  const timeoutMs = options.timeoutMs ?? 5000;
  const bodyTimeoutMs = options.bodyTimeoutMs ?? 2000;
  if (!Number.isInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > 5000 ||
      !Number.isInteger(bodyTimeoutMs) || bodyTimeoutMs < 1 || bodyTimeoutMs > 2000) throw new Error('invalid evaluation deadline');
  return async (request: Request): Promise<Response> => {
    const rejected = admission(request, options);
    if (rejected) return rejected;
    const started = performance.now();
    const bodyDeadline = deadline(request.signal, bodyTimeoutMs);
    let parsed: ReturnType<typeof Body.safeParse>;
    const reader = request.body?.getReader();
    if (!reader) {
      bodyDeadline.clear();
      return json({ error: 'body required' }, 400);
    }
    try {
      const chunks: Uint8Array[] = [];
      let size = 0;
      while (true) {
        const { done, value } = await abortable(() => reader.read(), bodyDeadline.signal);
        if (done) break;
        size += value.byteLength;
        if (size > MAX_BYTES) return json({ error: 'request too large' }, 413);
        chunks.push(value);
      }
      parsed = Body.safeParse(JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(Buffer.concat(chunks))));
    } catch {
      if (bodyDeadline.signal.aborted) return failure(request, bodyDeadline.signal, 'request', started);
      return json({ error: 'invalid JSON' }, 400);
    } finally {
      bodyDeadline.clear();
      void reader.cancel().catch(() => undefined);
      reader.releaseLock();
    }
    if (!parsed.success) return json({ error: 'invalid evaluation request' }, 400);
    const input = parsed.data;
    const evaluationDeadline = deadline(request.signal, timeoutMs);
    let phase = 'evaluation';
    try {
      const result = await abortable(() => (options.evaluator ?? runJev)(input, evaluationDeadline.signal), evaluationDeadline.signal);
      evaluationDeadline.signal.throwIfAborted();
      phase = 'validation';
      const answers = Answers.parse(result.answers);
      return json({ schema_version: 1, run_id: parsed.data.run_id, question_pack: parsed.data.question_pack,
                    source: 'model_assessment', model: 'typesafe-ai/jev', answers, usage: usage(result.usage),
                    official_grade: null }, 200);
    } catch (error) {
      return failure(request, evaluationDeadline.signal, phase, started, error);
    } finally {
      evaluationDeadline.clear();
    }
  };
}
