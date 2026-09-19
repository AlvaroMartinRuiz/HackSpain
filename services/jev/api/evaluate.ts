import type { IncomingMessage, ServerResponse } from 'node:http';
import { makeHandler } from '../lib/evaluate.ts';

export default async function handler(req: IncomingMessage & { body?: unknown }, res: ServerResponse) {
  const buffers: Buffer[] = [];
  let size = 0;
  if (req.body !== undefined) {
    const buffer = Buffer.from(typeof req.body === 'string' ? req.body : JSON.stringify(req.body));
    size = buffer.byteLength;
    buffers.push(buffer);
  } else {
    for await (const chunk of req) {
      const buffer = Buffer.from(chunk);
      size += buffer.byteLength;
      if (size > 24_000) break;
      buffers.push(buffer);
    }
  }
  if (size > 24_000) {
    res.writeHead(413, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
    res.end(JSON.stringify({ error: 'request too large' }));
    return;
  }
  const headers = new Headers();
  if (typeof req.headers.authorization === 'string') headers.set('authorization', req.headers.authorization);
  const method = req.method ?? 'GET';
  const request = new Request('https://internal.invalid/api/evaluate', {
    method, headers, body: method === 'GET' || method === 'HEAD' ? undefined : Buffer.concat(buffers),
  });
  const result = await makeHandler({
    token: process.env.JEV_SERVICE_TOKEN ?? '',
    enabled: process.env.JEV_ENABLE_PAID === 'true' && Boolean(process.env.AI_GATEWAY_API_KEY || process.env.VERCEL_OIDC_TOKEN),
  })(request);
  res.writeHead(result.status, Object.fromEntries(result.headers));
  res.end(await result.text());
}
