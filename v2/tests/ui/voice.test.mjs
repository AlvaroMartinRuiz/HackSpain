import test from 'node:test';
import assert from 'node:assert/strict';
import {encodeMuLaw, decodeMuLaw, StreamingResampler, MuLawFramer, bytesToBase64, base64ToSamples,
  voiceSocketURL, startMessages, PacedPlayback, BrowserVoice} from '../../web/voice.mjs';

const tick = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return {promise, resolve}; };
const tone = (rate, frequency, length = rate) => Float32Array.from({length}, (_, i) => .5 * Math.sin(2 * Math.PI * frequency * i / rate));
const rms = values => Math.sqrt(values.reduce((sum, value) => sum + value * value, 0) / values.length);

test('G.711 known vectors, both silence encodings, clipping and polarity', () => {
  assert.equal(encodeMuLaw(0), 0xff); assert.equal(decodeMuLaw(0xff), 0); assert.equal(decodeMuLaw(0x7f), 0);
  assert.equal(encodeMuLaw(1), 0x80); assert.equal(encodeMuLaw(-1), 0x00);
  assert.equal(decodeMuLaw(0x80), 32124 / 32768); assert.equal(decodeMuLaw(0x00), -32124 / 32768);
  assert.equal(encodeMuLaw(Infinity), 0xff);
  for (const sample of [-.8, -.2, -.01, .01, .2, .8]) assert.ok(Math.abs(decodeMuLaw(encodeMuLaw(sample)) - sample) < .035);
});

test('base64 media decoding preserves real mu-law samples and rejects malformed/oversize payloads', () => {
  const data = Uint8Array.from([0xff, 0x7f, 0x80, 0]);
  assert.deepEqual([...base64ToSamples(bytesToBase64(data))], [0, 0, 32124 / 32768, -32124 / 32768]);
  assert.throws(() => base64ToSamples('<script>')); assert.throws(() => base64ToSamples('A'.repeat(90001)));
});

for (const rate of [8000, 16000, 44100, 48000]) {
  test(`continuous resampling at ${rate} Hz has no block-boundary phase drift`, () => {
    const samples = tone(rate, 700);
    const whole = new StreamingResampler(rate).push(samples);
    const split = new StreamingResampler(rate), parts = [];
    for (let i = 0; i < samples.length; i += 137) parts.push(...split.push(samples.subarray(i, i + 137)));
    assert.equal(parts.length, whole.length); assert.ok(whole.length >= 7980 && whole.length <= 8000);
    let error = 0; for (let i = 0; i < whole.length; i++) error = Math.max(error, Math.abs(whole[i] - parts[i]));
    assert.ok(error < 1e-5, `max split error ${error}`);
    assert.ok(rms(whole.slice(100)) > .3);
  });
}

test('downsampling rejects out-of-band content instead of aliasing speech-band noise', () => {
  const low = new StreamingResampler(48000).push(tone(48000, 1000)).slice(100);
  const high = new StreamingResampler(48000).push(tone(48000, 10000)).slice(100);
  assert.ok(rms(high) < rms(low) * .05);
  assert.throws(() => new StreamingResampler(0)); assert.throws(() => new StreamingResampler(4000));
});

test('microphone framer produces only complete mono 8k 20ms packets, retaining remainder', () => {
  const framer = new MuLawFramer(48000), frames = [];
  for (let i = 0; i < 48000; i += 128) frames.push(...framer.push(new Float32Array(Math.min(128, 48000 - i))));
  assert.equal(frames.length, 49); assert.ok(frames.every(frame => frame.length === 160 && frame.every(value => value === 0xff)));
  assert.ok(framer.pending.length < 160);
});

test('browser ticket is a subprotocol, never an operator token URL; handshake matches frozen transport', () => {
  assert.equal(voiceSocketURL('/ws/browser', {protocol: 'http:', host: 'localhost:7861'}), 'ws://localhost:7861/ws/browser');
  assert.equal(voiceSocketURL('/ws/browser', {protocol: 'https:', host: 'test.example'}), 'wss://test.example/ws/browser');
  assert.throws(() => voiceSocketURL('/ws/browser?token=bad', {protocol: 'https:', host: 'test.example'}));
  assert.throws(() => voiceSocketURL('wss://other.example/ws/browser', {}));
  const [connected, start] = startMessages('call-1', 'stream-1');
  assert.equal(connected.event, 'connected'); assert.equal(start.start.callSid, 'call-1'); assert.equal(start.streamSid, 'stream-1');
  assert.deepEqual(start.start.mediaFormat, {encoding: 'audio/x-mulaw', sampleRate: 8000, channels: 1});
});

class FakeNode {
  constructor() { this.connections = []; this.stopped = 0; this.disconnected = 0; }
  connect(to) { this.connections.push(to); }
  disconnect() { this.disconnected++; }
  start(at) { this.at = at; }
  stop() { this.stopped++; }
}
class FakeAudioContext {
  constructor() { this.state = 'suspended'; this.sampleRate = 48000; this.currentTime = 10; this.destination = {}; this.sources = []; this.closed = 0; }
  async resume() { this.state = 'running'; }
  async close() { this.state = 'closed'; this.closed++; }
  createBuffer(channels, length, rate) { assert.equal(channels, 1); return {length, rate, copyToChannel(samples) { this.samples = samples; }}; }
  createBufferSource() { const source = new FakeNode(); this.sources.push(source); return source; }
  createMediaStreamSource() { return new FakeNode(); }
  createGain() { const node = new FakeNode(); node.gain = {value: 1}; return node; }
  createScriptProcessor() { this.processor = new FakeNode(); return this.processor; }
}

test('playback schedules decoded audio in order; clear invalidates and stops every queued source', async () => {
  const context = new FakeAudioContext(); await context.resume(); const player = new PacedPlayback(context);
  player.enqueue(new Float32Array(160)); player.enqueue(new Float32Array(160));
  assert.ok(Math.abs(context.sources[1].at - context.sources[0].at - .02) < 1e-9);
  assert.equal(context.sources[0].buffer.rate, 8000);
  const generation = player.generation; player.clear(); assert.equal(player.generation, generation + 1);
  assert.equal(player.sources.size, 0); assert.ok(context.sources.every(source => source.stopped === 1));
  player.enqueue(new Float32Array(160)); assert.equal(context.sources.at(-1).at, context.currentTime + .025);
  context.state = 'suspended'; assert.throws(() => player.enqueue(new Float32Array(160)), /suspended/); player.clear();
});

function harness({mic, ticket, worklet = false} = {}) {
  const tracks = [{stopped: 0, stop() { this.stopped++; }}], stream = {getTracks: () => tracks};
  const contexts = [], sockets = [], requests = [], statuses = [], stopped = [], ready = [];
  class Context extends FakeAudioContext {
    constructor() { super(); contexts.push(this); if (worklet) this.audioWorklet = {addModule: async path => { assert.equal(path, '/web/capture-worklet.js'); }}; }
  }
  class Socket {
    constructor(url, protocols) { this.url = url; this.protocols = protocols; this.protocol = 'v2-voice'; this.readyState = 0; this.sent = []; this.bufferedAmount = 0; sockets.push(this); }
    open() { this.readyState = 1; this.onopen?.(); }
    send(value) { this.sent.push(JSON.parse(value)); }
    message(message) { this.onmessage?.({data: JSON.stringify(message)}); }
    close(code) { this.readyState = 3; this.closeCode = code; }
  }
  class Worklet extends FakeNode {
    constructor() { super(); this.port = {close() { this.closed = true; }}; }
  }
  const env = {isSecureContext: true, location: {protocol: 'http:', host: 'localhost:7861'},
    navigator: {mediaDevices: {getUserMedia: mic || (async () => stream)}}, AudioContext: Context, WebSocket: Socket};
  if (worklet) env.AudioWorkletNode = Worklet;
  const api = {request: async (path, options) => { requests.push({path, options}); return ticket ? ticket() : {ticket: 'mock-single-use-ticket', websocket_path: '/ws/browser', call_id: 'call-mock', stream_sid: 'stream-mock'}; }};
  const voice = new BrowserVoice(api, {onStatus: value => statuses.push(value), onStopped: value => stopped.push(value), onReady: value => ready.push(value)}, env);
  return {voice, tracks, stream, contexts, sockets, requests, statuses, stopped, ready, env};
}
async function connect(h) {
  const before = h.sockets.length;
  const start = h.voice.start({language: 'es', mode: 'simulation'}); await tick();
  assert.equal(h.sockets.length, before + 1); const socket = h.sockets.at(-1); socket.open();
  socket.message({event: 'ready', run_id: 'mock-run'}); await start; return socket;
}
function capture(voice, samples = new Float32Array(2048)) {
  voice.processor.onaudioprocess({inputBuffer: {getChannelData: () => samples}, outputBuffer: {getChannelData: () => new Float32Array(samples.length)}});
}

test('mocked browser call authenticates via ticket, waits for ready, captures frames, clears playback and stops tracks/context', async () => {
  const h = harness(); const starting = h.voice.start({language: 'ca', mode: 'practice'}); await tick();
  assert.equal(h.requests[0].path, '/api/voice/ticket'); assert.deepEqual(h.requests[0].options.body, {language: 'ca', mode: 'practice'});
  const socket = h.sockets[0]; assert.deepEqual(socket.protocols, ['v2-voice', 'ticket.mock-single-use-ticket']); assert.doesNotMatch(socket.url, /ticket|token/);
  socket.open(); capture(h.voice); assert.deepEqual(socket.sent.map(event => event.event), ['connected', 'start']);
  socket.message({event: 'ready', run_id: 'mock-run'}); await starting; assert.deepEqual(h.ready, ['mock-run']);
  capture(h.voice); const frames = socket.sent.filter(event => event.event === 'media'); assert.ok(frames.length > 0);
  assert.ok(frames.every(event => atob(event.media.payload).length === 160)); assert.equal(frames[0].media.timestamp, '0'); assert.equal(frames[0].sequenceNumber, '2');
  socket.message({event: 'media', media: {payload: bytesToBase64(new Uint8Array(160).fill(0xff))}});
  const playback = h.voice.playback; assert.equal(playback.sources.size, 1); socket.message({event: 'clear'}); assert.equal(playback.sources.size, 0);
  socket.message({event: 'mark', mark: {name: 'not-acknowledged'}}); assert.equal(socket.sent.some(event => event.event === 'mark'), false);
  h.voice.stop(); assert.equal(socket.sent.at(-1).event, 'stop'); assert.equal(socket.closeCode, 1000);
  assert.equal(h.tracks[0].stopped, 1); assert.equal(h.contexts[0].closed, 1); assert.equal(h.voice.phase, 'idle'); assert.equal(h.stopped.length, 1);
  h.voice.stop(); assert.equal(h.tracks[0].stopped, 1);
});

test('a call defaults to automatic language without a manual language choice', async () => {
  const h = harness(), starting = h.voice.start({mode: 'simulation'});
  starting.catch(() => {});
  await tick();
  assert.equal(h.requests.length, 1);
  assert.deepEqual(h.requests[0].options.body, {language: 'auto', mode: 'simulation'});
  const socket = h.sockets[0]; socket.open(); socket.message({event: 'ready', run_id: 'mock-run'});
  await starting; h.voice.stop();
});

test('preferred AudioWorklet capture uses zero-gain output and closes its port on stop', async () => {
  const h = harness({worklet: true}); const socket = await connect(h);
  assert.equal(h.voice.silent.gain.value, 0);
  const processor = h.voice.processor; processor.port.onmessage({data: new Float32Array(2048)});
  assert.ok(socket.sent.some(event => event.event === 'media'));
  h.voice.stop(); assert.equal(processor.port.closed, true); assert.equal(processor.port.onmessage, null);
});

test('stop while microphone permission is pending stops late tracks and never requests a paid ticket', async () => {
  const pending = deferred(), h = harness({mic: () => pending.promise});
  const start = h.voice.start({language: 'en', mode: 'simulation'}); h.voice.stop(); pending.resolve(h.stream); await start;
  assert.equal(h.tracks[0].stopped, 1); assert.equal(h.contexts[0].closed, 1); assert.equal(h.requests.length, 0); assert.equal(h.sockets.length, 0);
});

test('stop while ticket is pending cannot create a late socket or leak devices', async () => {
  const pending = deferred(), h = harness({ticket: () => pending.promise});
  const start = h.voice.start({language: 'en', mode: 'simulation'}); await tick(); h.voice.stop();
  pending.resolve({ticket: 'late', websocket_path: '/ws/browser', call_id: 'call', stream_sid: 'stream'}); await start;
  assert.equal(h.sockets.length, 0); assert.equal(h.tracks[0].stopped, 1); assert.equal(h.contexts[0].closed, 1);
});

test('stopping during websocket ready wait settles startup without a dangling timer', async () => {
  const h = harness(), start = h.voice.start({language: 'es', mode: 'simulation'}); await tick();
  h.sockets[0].open(); h.voice.stop(); await start; assert.equal(h.voice.phase, 'idle'); assert.equal(h.contexts[0].closed, 1);
});

test('microphone denial is actionable, closes the context and consumes no ticket', async () => {
  const h = harness({mic: async () => { const error = Error('private system details'); error.name = 'NotAllowedError'; throw error; }});
  await assert.rejects(h.voice.start({language: 'en', mode: 'simulation'}), /Microphone access was denied/);
  assert.equal(h.contexts[0].closed, 1); assert.equal(h.requests.length, 0); assert.equal(h.voice.phase, 'idle');
  assert.ok(h.stopped.every(message => !message.includes('private system details')));
});

test('socket error, stop, malformed audio and backpressure release all resources without reflecting server bodies', async () => {
  for (const scenario of ['error', 'stop', 'malformed', 'backpressure', 'suspend']) {
    const h = harness(), socket = await connect(h);
    if (scenario === 'error') socket.message({event: 'error', message: 'private provider body'});
    if (scenario === 'stop') socket.message({event: 'stop'});
    if (scenario === 'malformed') socket.message({event: 'media', media: {payload: '<invalid>'}});
    if (scenario === 'backpressure') { socket.bufferedAmount = 65000; capture(h.voice); }
    if (scenario === 'suspend') { h.contexts[0].state = 'suspended'; h.contexts[0].onstatechange(); }
    assert.equal(h.voice.phase, 'idle', scenario); assert.equal(h.tracks[0].stopped, 1, scenario); assert.equal(h.contexts[0].closed, 1, scenario);
    assert.ok(h.stopped.every(message => !message.includes('private provider body')));
  }
});

test('old generation callbacks cannot send media into a replacement call', async () => {
  const h = harness(); const old = await connect(h), callback = h.voice.processor.onaudioprocess;
  h.voice.stop(); const next = await connect(h); const before = next.sent.length;
  callback({inputBuffer: {getChannelData: () => new Float32Array(2048)}, outputBuffer: {getChannelData: () => new Float32Array(2048)}});
  assert.equal(next.sent.length, before); assert.equal(old.sent.at(-1).event, 'stop'); h.voice.stop();
});

test('insecure origins reject microphone before any paid request', async () => {
  const h = harness(); h.env.isSecureContext = false;
  await assert.rejects(h.voice.start({language: 'es', mode: 'simulation'}), /localhost or HTTPS/); assert.equal(h.requests.length, 0);
});

for (const worklet of [false, true]) {
  test(`mute preserves playback and sends silence without buffered microphone leakage (${worklet ? 'worklet' : 'fallback'})`, async () => {
    const h = harness({worklet}), levels = [], muted = [];
    h.voice.callbacks.onLevel = level => levels.push(level);
    h.voice.callbacks.onMuteChange = value => muted.push(value);
    const socket = await connect(h);
    const input = samples => worklet ? h.voice.processor.port.onmessage({data: samples}) : capture(h.voice, samples);
    input(new Float32Array(2048).fill(.25));
    assert.equal(levels.at(-1), .25);
    assert.ok(socket.sent.some(message => message.event === 'media' && [...atob(message.media.payload)].some(value => value.charCodeAt(0) !== 0xff)));
    h.voice.setMuted(true);
    assert.equal(h.voice.muted, true); assert.equal(h.tracks[0].enabled, false); assert.equal(levels.at(-1), 0);
    const before = socket.sent.length;
    input(new Float32Array(2048).fill(.8));
    const silence = socket.sent.slice(before).filter(message => message.event === 'media');
    assert.ok(silence.length > 0);
    assert.ok(silence.every(message => [...atob(message.media.payload)].every(value => value.charCodeAt(0) === 0xff)));
    assert.equal(levels.at(-1), 0);
    socket.message({event: 'media', media: {payload: bytesToBase64(new Uint8Array(160).fill(0x80))}});
    assert.equal(h.voice.playback.sources.size, 1);
    h.voice.setMuted(false);
    assert.equal(h.tracks[0].enabled, true);
    input(new Float32Array(2048).fill(.5));
    assert.equal(levels.at(-1), .5);
    assert.deepEqual(muted.slice(-2), [true, false]);
    h.voice.stop();
    assert.equal(levels.at(-1), 0); assert.equal(h.voice.muted, false); assert.equal(h.tracks[0].stopped, 1);
  });
}

for (const name of ['permissionsPolicy', 'featurePolicy']) {
  test(`${name} blocks microphone before opening devices or requesting paid access`, async () => {
    const h = harness();
    h.env.document = {[name]: {allowsFeature: feature => feature !== 'microphone'}};
    await assert.rejects(h.voice.start({language: 'es', mode: 'simulation'}), /permissions policy/);
    assert.equal(h.contexts.length, 0); assert.equal(h.requests.length, 0); assert.equal(h.voice.phase, 'idle');
  });
}
