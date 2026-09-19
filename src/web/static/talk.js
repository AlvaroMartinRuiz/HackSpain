/* Browser as the Twilio caller: mic → µ-law 8 kHz → /ws, and the reverse. */

const Talk = (() => {
  const FRAME_SAMPLES = 160;
  const FRAME_MS = 20;
  const RATE = 8000;
  const MULAW_BIAS = 0x84;
  const SILENCE = 0xff;

  let running = false;
  let socket = null;
  let micStream = null;
  let captureCtx = null;
  let playCtx = null;
  let processor = null;
  let sendTimer = null;
  let callId = null;
  let streamSid = "";
  let sequence = 2;
  let pending = new Float32Array(0);
  let playAt = 0;
  let sources = [];

  function $(id) {
    return document.getElementById(id);
  }

  function setStatus(text, kind) {
    const node = $("talk-status");
    if (!node) return;
    node.textContent = text;
    node.className = "talk-hint" + (kind ? ` ${kind}` : "");
  }

  function setButtons(onCall) {
    const callBtn = $("talk-call");
    const hangBtn = $("talk-hang");
    if (callBtn) callBtn.disabled = onCall;
    if (hangBtn) hangBtn.disabled = !onCall;
  }

  function pcm16ToMuLaw(sample) {
    let sign = (sample >> 8) & 0x80;
    if (sign) sample = -sample;
    if (sample > 32635) sample = 32635;
    sample += MULAW_BIAS;
    let exponent = 7;
    for (let mask = 0x4000; (sample & mask) === 0 && exponent > 0; exponent--, mask >>= 1) {}
    const mantissa = (sample >> (exponent + 3)) & 0x0f;
    return ~(sign | (exponent << 4) | mantissa) & 0xff;
  }

  function muLawToFloat(mulaw) {
    mulaw = ~mulaw & 0xff;
    const sign = mulaw & 0x80;
    const exponent = (mulaw >> 4) & 0x07;
    const mantissa = mulaw & 0x0f;
    let sample = ((mantissa << 3) + MULAW_BIAS) << exponent;
    sample -= MULAW_BIAS;
    if (sign) sample = -sample;
    return sample / 32768;
  }

  function downsample(input, fromRate) {
    if (fromRate === RATE) return input;
    const ratio = fromRate / RATE;
    const out = new Float32Array(Math.floor(input.length / ratio));
    for (let i = 0; i < out.length; i++) {
      const src = i * ratio;
      const i0 = Math.floor(src);
      const frac = src - i0;
      const a = input[i0] || 0;
      const b = input[i0 + 1] || a;
      out[i] = a + (b - a) * frac;
    }
    return out;
  }

  function appendPending(chunk) {
    const next = new Float32Array(pending.length + chunk.length);
    next.set(pending);
    next.set(chunk, pending.length);
    pending = next;
  }

  function takeFrame() {
    if (pending.length < FRAME_SAMPLES) return null;
    const frame = pending.subarray(0, FRAME_SAMPLES);
    pending = pending.slice(FRAME_SAMPLES);
    const bytes = new Uint8Array(FRAME_SAMPLES);
    for (let i = 0; i < FRAME_SAMPLES; i++) {
      const pcm = Math.max(-1, Math.min(1, frame[i])) * 32767;
      bytes[i] = pcm16ToMuLaw(pcm | 0);
    }
    return bytes;
  }

  function sendMedia(bytes) {
    if (!socket || socket.readyState !== WebSocket.OPEN) return;
    let binary = "";
    for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
    socket.send(JSON.stringify({
      event: "media",
      sequenceNumber: String(sequence),
      streamSid,
      media: {
        track: "inbound",
        chunk: String(sequence),
        timestamp: String((sequence - 2) * FRAME_MS),
        payload: btoa(binary),
      },
    }));
    sequence += 1;
  }

  function clearPlayback() {
    for (const src of sources) {
      try { src.stop(); } catch { /* already ended */ }
    }
    sources = [];
    playAt = 0;
  }

  function enqueuePlayback(payload) {
    if (!playCtx || !payload) return;
    let raw;
    try {
      raw = atob(payload);
    } catch {
      return;
    }
    const pcm = new Float32Array(raw.length);
    for (let i = 0; i < raw.length; i++) pcm[i] = muLawToFloat(raw.charCodeAt(i));
    const buffer = playCtx.createBuffer(1, pcm.length, RATE);
    buffer.getChannelData(0).set(pcm);
    const src = playCtx.createBufferSource();
    src.buffer = buffer;
    src.connect(playCtx.destination);
    const now = playCtx.currentTime;
    if (playAt < now + 0.04) playAt = now + 0.04;
    src.start(playAt);
    playAt += buffer.duration;
    sources.push(src);
    src.onended = () => {
      sources = sources.filter((item) => item !== src);
    };
  }

  function appendLog(role, text) {
    const log = $("talk-log");
    if (!log || !text) return;
    log.hidden = false;
    const who = role === "agent" ? "Agent" : "You";
    log.textContent += (log.textContent ? "\n" : "") + `${who}: ${text}`;
    log.scrollTop = log.scrollHeight;
  }

  function showCallLink(id) {
    const line = $("talk-call-line");
    const link = $("talk-call-link");
    if (!line || !link) return;
    line.hidden = false;
    link.href = `#/calls/${encodeURIComponent(id)}`;
  }

  async function pickup() {
    if (running) return;
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      setStatus("This browser cannot open the microphone.", "bad");
      return;
    }

    setStatus("Asking for the microphone…");
    try {
      micStream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
          channelCount: 1,
        },
      });
    } catch (error) {
      setStatus(`Microphone blocked: ${error.message || error}`, "bad");
      return;
    }

    captureCtx = new AudioContext();
    playCtx = new AudioContext();
    await captureCtx.resume();
    await playCtx.resume();

    const source = captureCtx.createMediaStreamSource(micStream);
    const bufferSize = 4096;
    processor = captureCtx.createScriptProcessor(bufferSize, 1, 1);
    processor.onaudioprocess = (event) => {
      if (!running) return;
      const input = event.inputBuffer.getChannelData(0);
      appendPending(downsample(input, captureCtx.sampleRate));
    };
    const mute = captureCtx.createGain();
    mute.gain.value = 0;
    source.connect(processor);
    processor.connect(mute);
    mute.connect(captureCtx.destination);

    callId = crypto.randomUUID();
    streamSid = `MZ${callId.replace(/-/g, "").slice(0, 30)}`;
    sequence = 2;
    pending = new Float32Array(0);
    clearPlayback();

    const scheme = location.protocol === "https:" ? "wss" : "ws";
    socket = new WebSocket(`${scheme}://${location.host}/ws`);
    socket.onmessage = (message) => {
      let data;
      try {
        data = JSON.parse(message.data);
      } catch {
        return;
      }
      if (data.event === "media") enqueuePlayback((data.media || {}).payload);
      if (data.event === "clear") clearPlayback();
    };
    socket.onclose = () => {
      if (running) hangup({ fromServer: true });
    };
    socket.onerror = () => setStatus("The call socket failed.", "bad");

    try {
      await new Promise((resolve, reject) => {
        socket.onopen = resolve;
        socket.addEventListener("error", () => reject(new Error("socket")), { once: true });
      });
    } catch {
      hangup();
      setStatus("Could not open the call socket.", "bad");
      return;
    }

    const fromNumber = ($("talk-number").value || "").trim();
    const dry = $("talk-dry").checked;
    const custom = { call_id: callId, dry_run: dry ? "true" : "false" };
    if (fromNumber) custom.from_number = fromNumber;

    socket.send(JSON.stringify({ event: "connected", protocol: "Call", version: "1.0.0" }));
    socket.send(JSON.stringify({
      event: "start",
      sequenceNumber: "1",
      streamSid,
      start: {
        streamSid,
        accountSid: "AClocaltalk",
        callSid: callId,
        tracks: ["inbound"],
        mediaFormat: { encoding: "audio/x-mulaw", sampleRate: 8000, channels: 1 },
        customParameters: custom,
      },
    }));

    running = true;
    setButtons(true);
    showCallLink(callId);
    $("talk-log").textContent = "";
    $("talk-log").hidden = true;
    setStatus("On the line. Wait for the greeting, then speak.");
    sendTimer = setInterval(() => {
      if (!running) return;
      const playing = sources.length > 0
        || (playCtx && playAt > playCtx.currentTime + 0.08);
      if (playing) {
        pending = new Float32Array(0);
        sendMedia(Uint8Array.from({ length: FRAME_SAMPLES }, () => SILENCE));
        return;
      }
      const frame = takeFrame() || Uint8Array.from({ length: FRAME_SAMPLES }, () => SILENCE);
      sendMedia(frame);
    }, FRAME_MS);
  }

  function hangup(opts = {}) {
    if (!running && !socket && !micStream) {
      setButtons(false);
      return;
    }
    running = false;
    if (sendTimer) {
      clearInterval(sendTimer);
      sendTimer = null;
    }
    if (socket && socket.readyState === WebSocket.OPEN) {
      try {
        socket.send(JSON.stringify({
          event: "stop",
          sequenceNumber: String(sequence),
          streamSid,
          stop: { accountSid: "AClocaltalk", callSid: callId },
        }));
      } catch { /* closing anyway */ }
      try { socket.close(); } catch { /* already closed */ }
    }
    socket = null;
    if (processor) {
      try { processor.disconnect(); } catch { /* */ }
      processor = null;
    }
    if (micStream) {
      micStream.getTracks().forEach((track) => track.stop());
      micStream = null;
    }
    if (captureCtx) {
      captureCtx.close().catch(() => {});
      captureCtx = null;
    }
    clearPlayback();
    if (playCtx) {
      playCtx.close().catch(() => {});
      playCtx = null;
    }
    pending = new Float32Array(0);
    setButtons(false);
    if (opts.fromServer) setStatus("The agent hung up.", "ok");
    else setStatus("Hung up.");
  }

  function onEvent(event) {
    if (!running || !event || event.call_id !== callId) return;
    const payload = event.payload || {};
    if (event.kind === "stt_final" && payload.text) appendLog("caller", payload.text);
    if (event.kind === "agent_said" && payload.text) appendLog("agent", payload.text);
    if (event.kind === "interruption") appendLog("caller", `⟨cuts in⟩ ${payload.heard || ""}`);
  }

  return {
    pickup,
    hangup,
    onEvent,
    active: () => running,
  };
})();

window.Talk = Talk;
