import {ApiError, validRunId} from './core.mjs';

export function encodeMuLaw(sample) {
  let pcm = Math.round(Math.max(-1, Math.min(1, Number.isFinite(sample) ? sample : 0)) * 32768);
  const sign = pcm < 0 ? 0x80 : 0;
  pcm = Math.min(32635, Math.abs(pcm)) + 0x84;
  let exponent = 7;
  for (let mask = 0x4000; exponent > 0 && !(pcm & mask); mask >>= 1) exponent--;
  return (~(sign | exponent << 4 | (pcm >> (exponent + 3) & 0x0f))) & 0xff;
}
export function decodeMuLaw(byte) {
  const value = (~byte) & 0xff;
  const magnitude = (((value & 0x0f) << 3) + 0x84) << ((value >> 4) & 7);
  return ((value & 0x80) ? 0x84 - magnitude : magnitude - 0x84) / 32768;
}
export class StreamingResampler {
  constructor(inputRate, outputRate = 8000) {
    if (!Number.isFinite(inputRate) || inputRate < outputRate || inputRate > 192000 || outputRate !== 8000) throw new Error('Unsupported microphone sample rate.');
    this.step = inputRate / outputRate; this.radius = Math.ceil(8 * this.step);
    this.cutoff = 0.9 / this.step; this.samples = Array(this.radius).fill(0); this.position = this.radius;
  }
  push(input) {
    for (const value of input) this.samples.push(Number.isFinite(value) ? value : 0);
    const output = [];
    while (this.position + this.radius < this.samples.length) {
      const center = Math.floor(this.position); let value = 0, weight = 0;
      for (let i = center - this.radius + 1; i <= center + this.radius; i++) {
        const distance = i - this.position;
        const sinc = Math.abs(distance) < 1e-9 ? this.cutoff : Math.sin(Math.PI * this.cutoff * distance) / (Math.PI * distance);
        const window = 0.5 + 0.5 * Math.cos(Math.PI * distance / this.radius);
        const w = sinc * window; value += (this.samples[i] || 0) * w; weight += w;
      }
      output.push(weight ? value / weight : 0); this.position += this.step;
    }
    const discard = Math.max(0, Math.floor(this.position) - this.radius);
    this.samples.splice(0, discard); this.position -= discard;
    return Float32Array.from(output);
  }
}
export class MuLawFramer {
  constructor(inputRate) { this.resampler = new StreamingResampler(inputRate); this.pending = []; }
  push(samples) {
    const frames = [];
    for (const sample of this.resampler.push(samples)) {
      this.pending.push(encodeMuLaw(sample));
      if (this.pending.length === 160) { frames.push(Uint8Array.from(this.pending)); this.pending = []; }
    }
    return frames;
  }
}
export function bytesToBase64(bytes) {
  let binary = ''; for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}
export function base64ToSamples(payload) {
  if (typeof payload !== 'string' || payload.length > 90000 || !/^[A-Za-z0-9+/]*={0,2}$/.test(payload)) throw new Error('Invalid audio payload.');
  const data = atob(payload), samples = new Float32Array(data.length);
  for (let i = 0; i < data.length; i++) samples[i] = decodeMuLaw(data.charCodeAt(i));
  return samples;
}
export function voiceSocketURL(path, location) {
  if (path !== '/ws/browser') throw new ApiError(0, 'Unexpected browser voice endpoint. Check the server contract.');
  return `${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}${path}`;
}
export function startMessages(callId, streamSid) {
  return [{event: 'connected', protocol: 'Call', version: '1.0.0'},
    {event: 'start', sequenceNumber: '1', streamSid, start: {callSid: callId, streamSid, tracks: ['inbound'],
      mediaFormat: {encoding: 'audio/x-mulaw', sampleRate: 8000, channels: 1}}}];
}
export class PacedPlayback {
  constructor(context) { this.context = context; this.sources = new Set(); this.nextTime = 0; this.generation = 0; }
  enqueue(samples) {
    if (!samples.length) return;
    const context = this.context;
    if (context.state !== 'running') throw new ApiError(0, 'Audio playback was suspended. End the call and restart with a user gesture.');
    if (Math.max(this.nextTime, context.currentTime + 0.025) + samples.length / 8000 - context.currentTime > 15) throw new ApiError(0, 'Incoming audio exceeded the playback buffer. End and retry the call.');
    const buffer = context.createBuffer(1, samples.length, 8000); buffer.copyToChannel(samples, 0);
    const source = context.createBufferSource(); source.buffer = buffer; source.connect(context.destination);
    const generation = this.generation;
    source.onended = () => { this.sources.delete(source); source.disconnect(); if (generation !== this.generation) return; };
    this.sources.add(source);
    const at = Math.max(context.currentTime + 0.025, this.nextTime); this.nextTime = at + samples.length / 8000;
    source.start(at);
  }
  clear() {
    this.generation++; this.nextTime = 0;
    for (const source of this.sources) { source.onended = null; try { source.stop(); } catch {} source.disconnect(); }
    this.sources.clear();
  }
}
function microphoneMessage(error) {
  return ({NotAllowedError: 'Microphone access was denied. Allow the microphone in site settings, then start again.',
    NotFoundError: 'No microphone was found. Connect an input device and try again.',
    NotReadableError: 'The microphone is busy or unavailable. Close other capture applications and try again.',
    SecurityError: 'Microphone access needs a secure origin. Use localhost or HTTPS.',
    OverconstrainedError: 'The microphone cannot meet the requested settings. Choose another input device.'})[error?.name]
    || 'Microphone or audio setup failed. Check your input device, site permissions and audio output.';
}
export class BrowserVoice {
  constructor(api, callbacks = {}, environment = globalThis) {
    this.api = api; this.callbacks = callbacks; this.env = environment; this.generation = 0; this.phase = 'idle';
  }
  status(message) { this.callbacks.onStatus?.(message); }
  async start({language, mode}) {
    if (this.phase !== 'idle') throw new ApiError(409, 'A browser call is already starting or active.');
    if (!['en', 'es', 'ca'].includes(language) || !['simulation', 'practice'].includes(mode)) throw new ApiError(0, 'Invalid voice language or environment.');
    const env = this.env, generation = ++this.generation;
    const alive = () => generation === this.generation;
    this.phase = 'starting'; this.status('Requesting microphone permission…');
    try {
      if (!env.isSecureContext || !env.navigator?.mediaDevices?.getUserMedia) throw new ApiError(0, 'Microphone calls need localhost or HTTPS and a browser with microphone support.');
      const AudioContext = env.AudioContext || env.webkitAudioContext;
      if (!AudioContext) throw new ApiError(0, 'Web Audio is unavailable in this browser. Try a current Chrome, Edge, Firefox or Safari.');
      const context = new AudioContext(); this.context = context;
      const resume = context.resume().then(() => null, () => new ApiError(0, 'Audio playback could not start. Allow site audio and restart the call.'));
      const stream = await env.navigator.mediaDevices.getUserMedia({audio: {channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true}});
      if (!alive()) { for (const track of stream.getTracks()) track.stop(); return; }
      this.stream = stream; const resumeError = await resume;
      if (!alive()) return;
      if (resumeError) throw resumeError;
      context.onstatechange = () => { if (alive() && this.phase === 'active' && context.state !== 'running') this.stop('Browser audio was suspended. Restart the call with the page in the foreground.'); };
      if (context.state !== 'running') throw new ApiError(0, 'Audio playback is blocked. Allow site audio and restart the call.');
      this.playback = new PacedPlayback(context); this.framer = new MuLawFramer(context.sampleRate);
      await this.prepareCapture(context, stream, generation);
      if (!alive()) return;
      for (const track of stream.getTracks()) track.onended = () => { if (alive()) this.stop('Microphone disconnected. Call stopped.'); };
      this.status('Requesting a single-use voice ticket · paid providers…');
      const ticket = await this.api.request('/api/voice/ticket', {method: 'POST', body: {language, mode}});
      if (!alive()) return;
      if (!ticket || typeof ticket.ticket !== 'string' || !/^[A-Za-z0-9._~-]+$/.test(ticket.ticket) ||
          !/^[A-Za-z0-9_-]{1,128}$/.test(ticket.call_id || '') || !/^[A-Za-z0-9_-]{1,128}$/.test(ticket.stream_sid || '')) throw new ApiError(0, 'The server returned an invalid voice ticket.');
      const socket = new env.WebSocket(voiceSocketURL(ticket.websocket_path, env.location), ['v2-voice', `ticket.${ticket.ticket}`]);
      this.socket = socket; this.callId = ticket.call_id; this.streamSid = ticket.stream_sid; this.sequence = 1; this.chunk = 0;
      this.status('Connecting voice transport…');
      await new Promise((resolve, reject) => {
        this.connectReject = reject;
        this.readyTimer = setTimeout(() => { if (alive()) { const error = new ApiError(0, 'Voice connection timed out. Check readiness and backend capacity, then retry.'); reject(error); this.stop(error.message); } }, 15000);
        socket.onopen = () => {
          if (!alive()) return;
          if (socket.protocol !== 'v2-voice') { reject(new ApiError(0, 'Voice authentication protocol was not accepted.')); return; }
          for (const message of startMessages(this.callId, this.streamSid)) socket.send(JSON.stringify(message));
        };
        socket.onmessage = event => {
          if (!alive()) return;
          try {
            if (typeof event.data !== 'string' || event.data.length > 100000) throw new ApiError(0, 'Unexpected voice transport message.');
            const message = JSON.parse(event.data);
            if (message.event === 'ready') {
              if (this.phase === 'active') return;
              if (!validRunId(message.run_id)) throw new ApiError(0, 'Voice session has no valid run identifier.');
              clearTimeout(this.readyTimer); this.connectReject = null; this.phase = 'active';
              this.status('Microphone live · paid call. Speak naturally; End session releases all audio.');
              this.callbacks.onReady?.(message.run_id); resolve();
            } else if (message.event === 'media' && this.phase === 'active') {
              this.playback.enqueue(base64ToSamples(message.media?.payload));
            } else if (message.event === 'clear') {
              this.playback.clear();
            } else if (message.event === 'stop') {
              this.stop('The server ended this call. Microphone and playback released.'); resolve();
            } else if (message.event === 'error') {
              throw new ApiError(0, 'The voice backend reported an error. Inspect the run errors and readiness before retrying.');
            }
          } catch (error) {
            const safe = error instanceof ApiError ? error : new ApiError(0, 'Invalid voice audio or transport message. Inspect backend diagnostics.');
            reject(safe); this.stop(safe.message);
          }
        };
        socket.onerror = () => {
          if (!alive()) return;
          const error = new ApiError(0, 'Voice connection failed. Check readiness, Origin permissions and ticket authentication.');
          reject(error); this.stop(error.message);
        };
        socket.onclose = event => {
          if (!alive()) return;
          const message = event.code === 1000 ? 'Call closed. Microphone and playback released.' :
            'Voice disconnected unexpectedly. Check the run for provider errors, capacity limits or an expired ticket.';
          reject(new ApiError(0, message)); this.stop(message);
        };
      });
    } catch (error) {
      if (!alive()) return;
      const safe = error instanceof ApiError ? error : new ApiError(0, microphoneMessage(error));
      this.stop(safe.message); throw safe;
    }
  }
  async prepareCapture(context, stream, generation) {
    const alive = () => generation === this.generation;
    const source = context.createMediaStreamSource(stream); this.source = source;
    const silent = context.createGain(); silent.gain.value = 0; this.silent = silent; silent.connect(context.destination);
    const capture = samples => {
      if (!alive() || this.phase !== 'active' || this.socket?.readyState !== 1) return;
      if (context.state !== 'running') { this.stop('Browser audio was suspended. Restart the call with the page in the foreground.'); return; }
      try {
        for (const bytes of this.framer.push(samples)) {
          if (!alive() || this.socket?.readyState !== 1) return;
          if (this.socket.bufferedAmount > 64000) { this.stop('The network cannot keep up with microphone audio. Call stopped; check connectivity.'); return; }
          this.chunk++; this.sequence++;
          this.socket.send(JSON.stringify({event: 'media', streamSid: this.streamSid, sequenceNumber: String(this.sequence),
            media: {track: 'inbound', chunk: String(this.chunk), timestamp: String((this.chunk - 1) * 20), payload: bytesToBase64(bytes)}}));
        }
      } catch { this.stop('Microphone streaming failed. Call stopped; check your device and connection.'); }
    };
    if (context.audioWorklet && this.env.AudioWorkletNode) {
      try {
        await context.audioWorklet.addModule('/web/capture-worklet.js');
        if (!alive()) return;
        const processor = new this.env.AudioWorkletNode(context, 'v2-microphone', {numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1]});
        this.processor = processor; processor.port.onmessage = event => capture(event.data);
        processor.onprocessorerror = () => { if (alive()) this.stop('Microphone audio processor failed. Restart the call.'); };
        source.connect(processor); processor.connect(silent); return;
      } catch { if (!alive()) return; }
    }
    if (!alive()) return;
    if (!context.createScriptProcessor) throw new ApiError(0, 'Audio capture is not supported by this browser.');
    const processor = context.createScriptProcessor(1024, 1, 1); this.processor = processor;
    processor.onaudioprocess = event => { event.outputBuffer.getChannelData(0).fill(0); capture(event.inputBuffer.getChannelData(0)); };
    source.connect(processor); processor.connect(silent);
  }
  stop(message = 'Call stopped. Microphone released.') {
    if (this.phase === 'idle') return;
    this.generation++; this.phase = 'idle'; clearTimeout(this.readyTimer);
    this.connectReject?.(new ApiError(0, 'Voice start cancelled.')); this.connectReject = null;
    const socket = this.socket; this.socket = null;
    if (socket) {
      socket.onopen = socket.onmessage = socket.onerror = socket.onclose = null;
      try { if (socket.readyState === 1) socket.send(JSON.stringify({event: 'stop', streamSid: this.streamSid, stop: {callSid: this.callId, streamSid: this.streamSid}})); } catch {}
      try { socket.close(1000, 'Operator ended call'); } catch {}
    }
    if (this.processor) {
      if (this.processor.port) { this.processor.port.onmessage = null; this.processor.port.close(); }
      this.processor.onaudioprocess = null; this.processor.onprocessorerror = null;
      try { this.processor.disconnect(); } catch {} this.processor = null;
    }
    try { this.source?.disconnect(); this.silent?.disconnect(); } catch {}
    this.source = this.silent = null; this.playback?.clear(); this.playback = null;
    if (this.stream) { for (const track of this.stream.getTracks()) { track.onended = null; track.stop(); } this.stream = null; }
    const context = this.context; this.context = null;
    if (context) { context.onstatechange = null; if (context.state !== 'closed') context.close().catch(() => {}); }
    this.framer = null; this.callId = this.streamSid = null;
    this.callbacks.onStopped?.(message);
  }
}
