/* Live demo stage: same /api/console/stream as Control, product-facing. */

const LANG = { es: "ES", en: "EN", ca: "CA" };
const PHASE = {
  waiting: "Waiting",
  listening: "Listening",
  thinking: "Thinking",
  tool: "Using tool",
  speaking: "Speaking",
  interrupted: "Interrupted",
  complete: "Call completed",
};
const TOOLS = {
  lookup_patient: "Searching patient",
  open_chart: "Opening chart",
  find_appointments: "Searching appointments",
  find_doctor: "Finding a doctor",
  book_slot: "Booking appointment",
  cancel_appointment: "Cancelling appointment",
  reschedule_appointment: "Moving appointment",
  register_new_patient: "Registering patient",
  end_without_booking: "Closing without booking",
  escalate_call: "Escalating",
};
const ACTIONS = {
  book: "Appointment booked",
  cancel: "Appointment cancelled",
  reschedule: "Appointment moved",
  register: "Patient on file",
  no_action: "No booking needed",
  escalate: "Handed to the clinic",
};

const $ = (id) => document.getElementById(id);

const state = {
  calls: new Map(),
  selected: null,
  phase: "waiting",
  startedAt: 0,
  languages: [],
  history: [],
  interruptTimer: 0,
  completeTimer: 0,
  lastPartial: "",
};

function redact(value) {
  return String(value ?? "")
    .replace(/\b\d{7,8}\s?[A-Za-z]\b/g, "••••")
    .replace(/\+?\d[\d\s\-()]{7,}\d/g, "•••");
}

function firstName(call) {
  const name = call && call.patient && call.patient.name;
  if (name) return redact(String(name).split(/\s+/)[0]);
  if (call && call.from_number) return "Patient";
  return "Waiting";
}

function isNoise(id) {
  const key = String(id || "");
  return key.startsWith("check-") || key.startsWith("demo-");
}

function liveCalls() {
  return [...state.calls.values()].filter((call) => call.live && !isNoise(call.call_id));
}

function pickPrimary() {
  const talkId = window.Talk && Talk.currentId && Talk.currentId();
  if (talkId && state.calls.get(talkId)?.live) return talkId;
  const live = liveCalls().sort((a, b) => (b.turns || 0) - (a.turns || 0));
  if (state.selected && live.some((call) => call.call_id === state.selected)) return state.selected;
  return live[0]?.call_id || null;
}

function setPhase(phase) {
  if (phase === "interrupted") {
    clearTimeout(state.interruptTimer);
    state.phase = "interrupted";
    document.body.dataset.phase = "interrupted";
    $("status-label").textContent = "Patient interrupted";
    state.interruptTimer = setTimeout(() => {
      if (state.phase === "interrupted") setPhase("listening");
    }, 1600);
    return;
  }
  state.phase = phase;
  document.body.dataset.phase = phase;
  $("status-label").textContent = PHASE[phase] || phase;
}

function setHero(role, text, partial) {
  const clean = redact(text || "").trim();
  if (!clean) return;
  $("hero-role").textContent = role === "agent" ? "Agent" : "Patient";
  $("hero-text").textContent = `“${clean}”`;
  $("hero-text").classList.toggle("partial", Boolean(partial));
}

function pushHistory(role, text) {
  const clean = redact(text || "").trim();
  if (!clean) return;
  const last = state.history[state.history.length - 1];
  if (last && last.role === role && last.text === clean) return;
  state.history.push({ role, text: clean });
  state.history = state.history.slice(-6);
  $("history").innerHTML = state.history.map((row) =>
    `<p><b>${row.role === "agent" ? "AGENT" : "PATIENT"}</b>${escapeHtml(row.text)}</p>`
  ).join("");
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function showLang(from, to) {
  if (!to) return;
  const chip = $("lang-chip");
  chip.hidden = false;
  $("lang-from").textContent = LANG[from] || (from || "").toUpperCase();
  $("lang-to").textContent = LANG[to] || to.toUpperCase();
  if (!from || from === to) {
    $("lang-from").textContent = "";
    chip.querySelector(".lang-arrow").hidden = true;
  } else {
    chip.querySelector(".lang-arrow").hidden = false;
  }
}

function friendlyFacts(args, result) {
  const facts = [];
  const source = { ...(args || {}), ...(result || {}) };
  const map = [
    ["when", null], ["asked_for", null], ["name", "Name"],
    ["specialty", null], ["specialty_id", null],
    ["location_id", "Site"], ["site", "Site"], ["location_name", "Site"],
    ["provider_name", "Doctor"], ["doctor", "Doctor"],
  ];
  map.forEach(([key, label]) => {
    const value = source[key];
    if (!value || typeof value === "object") return;
    facts.push(label ? `${label} · ${redact(value)}` : redact(value));
  });
  if (Array.isArray(result?.options) && result.options.length) {
    facts.push(`${result.options.length} appointments found`);
  }
  if (result?.found === 1) facts.push("Patient found");
  if (result?.found === 0) facts.push("No match yet");
  if (result?.booked) facts.push("Slot confirmed");
  return [...new Set(facts)].slice(0, 4);
}

function showTool(name, args, result, running) {
  const card = $("tool-card");
  card.hidden = false;
  $("tool-kicker").textContent = running ? "Working" : "Done";
  $("tool-title").textContent = TOOLS[name] || name.replaceAll("_", " ");
  $("tool-name").textContent = name;
  $("tool-facts").innerHTML = friendlyFacts(args, result)
    .map((row) => `<li>${escapeHtml(row)}</li>`).join("");
}

function showAction(action) {
  const label = ACTIONS[action];
  if (!label) return;
  $("action-card").hidden = false;
  $("action-line").textContent = `✓ ${label}`;
}

function renderRail() {
  const live = liveCalls();
  $("live-count").textContent = String(live.length);
  $("live-rail").hidden = live.length < 2;
  $("live-rows").innerHTML = live.slice(0, 6).map((call) => {
    const label = firstName(call);
    const act = call.activity || "idle";
    return `<div class="rail-row"><b>${escapeHtml(label)}</b><span>${escapeHtml(act)}</span></div>`;
  }).join("");
}

function tickClock() {
  const call = state.selected && state.calls.get(state.selected);
  if (!call || !call.live) {
    $("call-clock").textContent = "00:00";
    return;
  }
  const ms = Date.now() - (call.durationBase || Date.now());
  const s = Math.max(0, Math.floor(ms / 1000));
  $("call-clock").textContent = `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}

function merge(summary) {
  if (!summary?.call_id) return;
  const prev = state.calls.get(summary.call_id) || {};
  const call = { ...prev, ...summary };
  call.live = summary.status === "live";
  call.durationBase = Date.now() - (summary.duration_s || 0) * 1000;
  state.calls.set(call.call_id, call);
}

function resetIdle() {
  state.selected = null;
  state.history = [];
  state.languages = [];
  setPhase("waiting");
  $("hero-role").textContent = "Ready";
  $("hero-text").textContent = "Waiting for a call";
  $("hero-text").classList.remove("partial");
  $("history").innerHTML = "";
  $("patient-name").textContent = "Waiting";
  $("smart-line").textContent = "Idle";
  $("tool-card").hidden = true;
  $("action-card").hidden = true;
  $("lang-chip").hidden = true;
  $("complete").hidden = true;
}

function completeCall(call) {
  const items = [];
  if (call?.patient?.name) items.push("Patient identified");
  (call.actions || []).forEach((row) => {
    const label = ACTIONS[row.action];
    if (label) items.push(label);
  });
  const languages = state.languages.length || (call.language ? 1 : 0);
  const meta = [
    call.interruptions ? `${call.interruptions} interruption${call.interruptions === 1 ? "" : "s"}` : "",
    languages > 1 ? `${languages} languages` : "",
    call.tool_calls ? `${call.tool_calls} tools` : "",
  ].filter(Boolean);
  $("complete-list").innerHTML = (items.length ? items : ["Call ended"])
    .map((row) => `<li>✓ ${escapeHtml(row)}</li>`).join("");
  $("complete-meta").textContent = meta.join(" · ");
  $("complete").hidden = false;
  setPhase("complete");
  clearTimeout(state.completeTimer);
  state.completeTimer = setTimeout(resetIdle, 8000);
}

function onEvent(event, summary) {
  if (summary) merge(summary);
  renderRail();
  const id = pickPrimary();
  if (id && id !== state.selected) {
    state.selected = id;
    $("complete").hidden = true;
  }
  if (window.Talk && event) Talk.onEvent(event);
  if (!event || (state.selected && event.call_id !== state.selected)) {
    if (summary) {
      const call = state.calls.get(summary.call_id);
      $("patient-name").textContent = firstName(state.calls.get(state.selected) || call);
    }
    return;
  }

  const call = state.calls.get(event.call_id) || {};
  $("patient-name").textContent = firstName(call);
  const payload = event.payload || {};

  switch (event.kind) {
    case "caller_speaking":
    case "stt_partial":
      setPhase("listening");
      if (payload.text) {
        state.lastPartial = payload.text;
        setHero("patient", payload.text, true);
      }
      $("smart-line").textContent = "Caller is still speaking…";
      break;
    case "turn_gate":
      setPhase("listening");
      if (payload.event === "complete") $("smart-line").textContent = "Turn complete ✓";
      else if (payload.event === "pause") $("smart-line").textContent = "Pause…";
      else $("smart-line").textContent = "Caller is still speaking…";
      break;
    case "turn_decision":
      $("smart-line").textContent = payload.complete ? "Turn complete ✓" : "Still speaking…";
      if (!payload.complete) setPhase("listening");
      break;
    case "stt_final":
    case "turn_committed":
      if (payload.text) {
        setHero("patient", payload.text);
        pushHistory("patient", payload.text);
      }
      $("smart-line").textContent = "Turn complete ✓";
      setPhase("thinking");
      break;
    case "agent_thinking":
    case "llm":
      setPhase("thinking");
      break;
    case "tool_started":
      setPhase("tool");
      showTool(payload.name, payload.arguments, null, true);
      break;
    case "tool_call":
      setPhase("tool");
      showTool(payload.name, payload.arguments, payload.result, false);
      break;
    case "agent_said":
    case "agent_speaking":
      setPhase("speaking");
      if (payload.text) {
        setHero("agent", payload.text);
        if (event.kind === "agent_said") pushHistory("agent", payload.text);
      }
      break;
    case "agent_turn_end":
      setPhase("listening");
      $("smart-line").textContent = "Listening";
      break;
    case "interruption":
      setPhase("interrupted");
      $("smart-line").textContent = "Yielded";
      if (payload.heard) setHero("patient", payload.heard);
      break;
    case "language_detected": {
      const next = payload.language;
      const prev = state.languages[state.languages.length - 1];
      if (next && next !== prev) {
        state.languages.push(next);
        showLang(prev, next);
      }
      break;
    }
    case "submit":
      showAction(payload.action);
      break;
    default:
      break;
  }
}

function connect() {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${scheme}://${location.host}/api/console/stream`);
  socket.onopen = () => $("link-dot").classList.add("up");
  socket.onclose = () => {
    $("link-dot").classList.remove("up");
    setTimeout(connect, 1500);
  };
  socket.onmessage = (message) => {
    const data = JSON.parse(message.data);
    if (data.type === "hello") {
      (data.overview?.live || []).forEach((call) => merge({ ...call, status: "live" }));
      renderRail();
      const id = pickPrimary();
      if (id) {
        state.selected = id;
        $("patient-name").textContent = firstName(state.calls.get(id));
        setPhase("listening");
      }
      return;
    }
    if (data.type === "call_started") {
      $("complete").hidden = true;
      return;
    }
    if (data.type === "call_ended") {
      const call = state.calls.get(data.call_id);
      if (call) call.live = false;
      renderRail();
      if (data.call_id === state.selected) completeCall(call);
      return;
    }
    if (data.type === "event") onEvent(data.event, data.summary);
  };
}

$("talk-form")?.addEventListener("submit", (event) => {
  event.preventDefault();
  if (window.Talk) Talk.pickup();
});
$("talk-hang")?.addEventListener("click", () => window.Talk && Talk.hangup());
$("caller-photo")?.addEventListener("error", () => {
  $("caller-photo").hidden = true;
  $("caller-photo-wrap")?.classList.add("no-photo");
});
$("caller-photo")?.addEventListener("load", () => {
  $("caller-photo-wrap")?.classList.remove("no-photo");
});

resetIdle();
connect();
setInterval(tickClock, 250);
