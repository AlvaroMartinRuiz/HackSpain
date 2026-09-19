import type { IncomingMessage, ServerResponse } from 'node:http';
import { admission, json, makeHandler, MAX_BYTES, type HandlerOptions } from '../lib/evaluate.ts';

export function makeNodeHandler(options: HandlerOptions) {
  const evaluate = makeHandler(options);
  return async (req: IncomingMessage & { body?: unknown }, res: ServerResponse) => {
    const controller = new AbortController();
    const aborted = () => controller.abort();
    const closed = () => { if (!res.writableEnded) controller.abort(); };
    req.once('aborted', aborted);
    req.once('error', aborted);
    res.once('close', closed);
    if (req.aborted || res.destroyed) controller.abort();
    const send = async (response: Response) => {
      if (res.destroyed || res.writableEnded) return;
      res.writeHead(response.status, { ...Object.fromEntries(response.headers), Connection: 'close' });
      res.end(await response.text());
    };
    try {
      const headers = new Headers();
      for (const name of ['authorization', 'content-type', 'content-length', 'content-encoding']) {
        const value = req.headers[name];
        if (typeof value === 'string') headers.set(name, value);
      }
      const method = req.method ?? 'GET';
      const rejected = admission({ method, headers }, options);
      if (rejected) {
        await send(rejected);
        return;
      }
      let body: Buffer | ReadableStream<Uint8Array>;
      if (req.body !== undefined) {
        body = Buffer.isBuffer(req.body) ? req.body : Buffer.from(typeof req.body === 'string' ? req.body : JSON.stringify(req.body));
        if (body.byteLength > MAX_BYTES) {
          await send(json({ error: 'request too large' }, 413));
          return;
        }
      } else {
        const iterator = req.iterator({ destroyOnReturn: false });
        body = new ReadableStream<Uint8Array>({
          async pull(stream) {
            try {
              const { done, value } = await iterator.next();
              if (done) stream.close();
              else stream.enqueue(Buffer.from(value));
            } catch (error) {
              stream.error(error);
            }
          },
          cancel() {
            req.pause();
            void iterator.return?.().catch(() => undefined);
          },
        }, { highWaterMark: 0 });
      }
      const init: RequestInit & { duplex: 'half' } = { method, headers,
        body: body instanceof ReadableStream ? body : Uint8Array.from(body).buffer,
        signal: controller.signal, duplex: 'half' };
      await send(await evaluate(new Request('https://internal.invalid/api/evaluate', init)));
    } catch {
      await send(json({ error: 'invalid evaluation request' }, 400));
    } finally {
      req.off('aborted', aborted);
      req.off('error', aborted);
      res.off('close', closed);
    }
  };
}

export default async function handler(req: IncomingMessage & { body?: unknown }, res: ServerResponse) {
  return makeNodeHandler({
    token: process.env.JEV_SERVICE_TOKEN ?? '',
    enabled: process.env.JEV_ENABLE_PAID === 'true' && Boolean(process.env.AI_GATEWAY_API_KEY || process.env.VERCEL_OIDC_TOKEN),
  })(req, res);
}
