import { createHash, timingSafeEqual } from 'node:crypto';
import { experimental_evaluate as evaluate } from 'ai';
import { z } from 'zod';

const MAX_BYTES = 24_000;
const Body = z.object({
  schema_version: z.literal(1),
  run_id: z.string().regex(/^[a-zA-Z0-9_-]{1,80}$/),
  question_pack: z.literal('conversation-v1'),
  turns: z.array(z.object({
    role: z.enum(['caller', 'agent']),
    text: z.string().min(1).max(1000),
  }).strict()).min(1).max(40),
}).strict();

const BooleanAnswer = z.object({ type: z.literal('boolean'), probability: z.number().min(0).max(1) });
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

export const runJev: Evaluator = async (input, signal) => evaluate({
  model: 'typesafe-ai/jev',
  state: { conversation: input.turns },
  questions,
  maxRetries: 0,
  abortSignal: signal,
});

function authorized(offered: string | null, expected: string): boolean {
  if (!expected || !offered?.startsWith('Bearer ')) return false;
  const hash = (text: string) => createHash('sha256').update(text).digest();
  return timingSafeEqual(hash(offered.slice(7)), hash(expected));
}

function json(body: unknown, status: number): Response {
  return Response.json(body, { status, headers: { 'Cache-Control': 'no-store' } });
}

export function makeHandler(options: { token: string; enabled: boolean; evaluator?: Evaluator }) {
  return async (request: Request): Promise<Response> => {
    if (request.method !== 'POST') return json({ error: 'POST required' }, 405);
    if (!authorized(request.headers.get('authorization'), options.token)) return json({ error: 'unauthorized' }, 401);
    if (!options.enabled) return json({ error: 'paid evaluation disabled' }, 503);
    let parsed: ReturnType<typeof Body.safeParse>;
    try {
      const reader = request.body?.getReader();
      if (!reader) return json({ error: 'body required' }, 400);
      const chunks: Uint8Array[] = [];
      let size = 0;
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        size += value.byteLength;
        if (size > MAX_BYTES) {
          await reader.cancel();
          return json({ error: 'request too large' }, 413);
        }
        chunks.push(value);
      }
      parsed = Body.safeParse(JSON.parse(Buffer.concat(chunks).toString('utf8')));
    } catch {
      return json({ error: 'invalid JSON' }, 400);
    }
    if (!parsed.success) return json({ error: 'invalid evaluation request' }, 400);
    try {
      const signal = AbortSignal.any([request.signal, AbortSignal.timeout(5000)]);
      const result = await (options.evaluator ?? runJev)(parsed.data, signal);
      const answers = Answers.parse(result.answers);
      return json({ schema_version: 1, run_id: parsed.data.run_id, question_pack: parsed.data.question_pack,
                    source: 'model_assessment', model: 'typesafe-ai/jev', answers, usage: result.usage ?? null,
                    official_grade: null }, 200);
    } catch {
      return json({ error: 'evaluation unavailable' }, 502);
    }
  };
}
