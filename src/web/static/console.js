const state = {
  selected: null,
  calls: new Map(),
  detail: null,
  tab: "decisions",
};

const el = (id) => document.getElementById(id);
const escapeHtml = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function pretty(value) {
  try {
    return JSON.stringify(value, null, 1);
  } catch (error) {
    return String(value);
  }
}

// ---- header ---------------------------------------------------------

function renderOverview(overview) {
  el("clinic-name").textContent = overview.clinic || "Clínica Arenal";
  const p = overview.providers || {};
  const pills = [
    `<span class="pill">escucha <b>${escapeHtml(p.stt)}</b>${p.stt_model ? ` · ${escapeHtml(p.stt_model)}` : ""}</span>`,
    `<span class="pill">modelo <b>${escapeHtml(p.llm)}</b></span>`,
    `<span class="pill">voz <b>${escapeHtml(p.tts)}</b></span>`,
    `<span class="pill">endpoint <b>${escapeHtml(
      (overview.endpoint && overview.endpoint.public_ws_url)
        || `wss://…:${overview.endpoint.port}${overview.endpoint.path}`
    )}</b></span>`,
  ];
  if (!overview.ready) {
    pills.push(`<span class="pill warn">faltan claves: <b>${escapeHtml((overview.missing_keys || []).join(", "))}</b></span>`);
  }
  el("pills").innerHTML = pills.join("");
  renderStats(overview.stats);

  state.calls.clear();
  (overview.live || []).forEach((call) => state.calls.set(call.call_id, { ...call, live: true }));
  (overview.recent || []).forEach((call) => {
    if (!state.calls.has(call.call_id)) state.calls.set(call.call_id, { ...call, live: false });
  });
  renderCallLists();
}

function renderStats(stats) {
  if (!stats) return;
  el("stat-live").textContent = stats.live ?? 0;
  el("stat-peak").textContent = stats.peak_concurrency ?? 0;
  el("stat-latency").textContent = stats.median_response_ms ? `${stats.median_response_ms} ms` : "–";
  el("stat-silent").textContent = stats.silent_calls ?? 0;
}

// ---- call lists -----------------------------------------------------

function renderCallLists() {
  const live = [];
  const recent = [];
  for (const call of state.calls.values()) (call.live ? live : recent).push(call);

  live.sort((a, b) => String(a.started_at).localeCompare(String(b.started_at)));
  recent.sort((a, b) => String(b.started_at).localeCompare(String(a.started_at)));

  el("live-list").innerHTML = live.length
    ? live.map(callItem).join("")
    : '<li class="empty">Nadie al teléfono</li>';
  el("recent-list").innerHTML = recent.length
    ? recent.slice(0, 25).map(callItem).join("")
    : '<li class="empty">Sin historial</li>';

  document.querySelectorAll(".call-item").forEach((node) => {
    node.onclick = () => selectCall(node.dataset.id);
  });
}

function callItem(call) {
  const who = call.patient?.name || call.from_number || call.call_id.slice(0, 10);
  const actions = call.actions || [];
  const badges = [];

  if (call.live) badges.push('<span class="badge live live-ring">en curso</span>');
  if (call.stage) badges.push(`<span class="badge">${escapeHtml(call.stage)}</span>`);
  if (actions.length) {
    actions.forEach((action) => {
      const cls = action.accepted ? "ok" : "err";
      badges.push(`<span class="badge ${cls}">${escapeHtml(action.action)}</span>`);
    });
  } else if (!call.live) {
    badges.push('<span class="badge no">sin registro</span>');
  }
  if (call.interruptions) badges.push(`<span class="badge">${call.interruptions}× corte</span>`);

  const selected = state.selected === call.call_id ? " selected" : "";
  return `<li class="call-item${selected}" data-id="${escapeHtml(call.call_id)}">
    <div class="row1"><span class="who">${escapeHtml(who)}</span>
    <span class="time">${call.duration_s ?? 0}s</span></div>
    <div class="row2">${badges.join("")}</div>
  </li>`;
}

// ---- selected call --------------------------------------------------

function hydrateDetail(detail) {
  if (!detail || (detail.transcript && detail.transcript.length) || !(detail.events || []).length) {
    return detail;
  }
  const transcript = [];
  for (const event of detail.events) {
    const payload = event.payload || {};
    if (event.kind === "stt_final" && payload.text) {
      transcript.push({ role: "caller", text: payload.text });
    } else if (event.kind === "agent_said" && payload.text) {
      transcript.push({ role: "agent", text: payload.text });
    }
  }
  detail.transcript = transcript;
  return detail;
}

async function selectCall(callId) {
  state.selected = callId;
  renderCallLists();
  try {
    const response = await fetch(`/api/console/calls/${encodeURIComponent(callId)}`);
    if (!response.ok) return;
    state.detail = hydrateDetail(await response.json());
    renderDetail();
  } catch (error) {
    /* the live feed will fill it in */
  }
}

function renderDetail() {
  const detail = state.detail;
  if (!detail) return;

  const patient = detail.patient_full || {};
  const name = patient.full_name || detail.patient?.name || detail.from_number || "Sin identificar";
  const chips = [];
  if (patient.patient_id) chips.push(`<span class="badge">${escapeHtml(patient.patient_id)}</span>`);
  if (patient.insurer) chips.push(`<span class="badge">${escapeHtml(patient.insurer)}</span>`);
  if (patient.has_visited_before !== undefined) {
    chips.push(`<span class="badge">${patient.has_visited_before ? "paciente conocido" : "primera visita"}</span>`);
  }
  if (detail.language) {
    const confidence = detail.language_confidence != null
      ? ` ${Math.round(Number(detail.language_confidence) * 100)}%`
      : "";
    const source = detail.language_source ? ` · ${detail.language_source}` : "";
    chips.push(`<span class="badge">${escapeHtml(detail.language + confidence + source)}</span>`);
  }
  (detail.actions || []).forEach((action) => {
    chips.push(`<span class="badge ${action.accepted ? "ok" : "err"}">${escapeHtml(action.action)} · ${action.status}</span>`);
  });

  el("call-head").innerHTML = `
    <div class="name">${escapeHtml(name)}</div>
    <div class="meta">${escapeHtml(detail.from_number || "número oculto")} ·
      ${detail.turns ?? 0} turnos · ${detail.tool_calls ?? 0} herramientas ·
      ${detail.clinic_calls ?? 0} consultas · ${detail.duration_s ?? 0}s</div>
    <div class="chips">${chips.join("")}</div>
    ${patient.note ? `<div class="note">${escapeHtml(patient.note)}</div>` : ""}
    ${tapePlayers(detail)}`;

  el("transcript").innerHTML = (detail.transcript || []).map(turnRow).join("");
  scrollTranscript();
  renderTabs();
}

function tapePlayers(detail) {
  const rec = detail.recordings || {};
  const id = detail.call_id;
  if (!id || (!rec.inbound && !rec.outbound)) return "";
  const parts = [];
  if (rec.inbound) {
    parts.push(`<label>Paciente (lo que nos llegó)
      <audio controls preload="metadata" src="/api/console/calls/${encodeURIComponent(id)}/audio/inbound"></audio>
    </label>`);
  }
  if (rec.outbound) {
    parts.push(`<label>Agente (lo que enviamos)
      <audio controls preload="metadata" src="/api/console/calls/${encodeURIComponent(id)}/audio/outbound"></audio>
    </label>`);
  }
  return `<div class="tape">${parts.join("")}</div>`;
}

function turnRow(turn) {
  const who = turn.role === "agent" ? "agente" : "paciente";
  return `<div class="turn ${turn.role}">
    <div class="bubble"><span class="tag">${who}</span>${escapeHtml(turn.text)}</div>
  </div>`;
}

function scrollTranscript() {
  const node = el("transcript");
  node.scrollTop = node.scrollHeight;
}

function renderTabs() {
  const detail = state.detail || {};

  el("tab-decisions").innerHTML = (detail.decisions || []).length
    ? (detail.decisions || []).map(decisionRow).join("")
    : '<div class="entry"><div class="trace">Todavía sin decisiones.</div></div>';

  el("tab-tools").innerHTML = (detail.tool_calls || []).length
    ? (detail.tool_calls || []).map(toolRow).join("")
    : '<div class="entry"><div class="trace">Sin herramientas todavía.</div></div>';

  const submissions = detail.submissions || [];
  const emails = detail.followup_emails || [];
  const clinic = (detail.clinic_calls || []).slice(-12);
  el("tab-record").innerHTML =
    (submissions.length
      ? submissions.map(submissionRow).join("")
      : '<div class="entry"><div class="trace">Nada enviado todavía.</div></div>') +
    (emails.length
      ? `<div class="group-label">Correo de seguimiento</div>` + emails.map(followupRow).join("")
      : "") +
    (clinic.length
      ? `<div class="group-label">Consultas al EHR</div>` + clinic.map(clinicRow).join("")
      : "");
}

function decisionRow(decision) {
  const trace = (decision.trace || []).map((line) => `<div>· ${escapeHtml(line)}</div>`).join("");
  const parts = [];
  if (decision.why) parts.push(escapeHtml(decision.why));
  if (decision.reason) parts.push(`<span class="reason">${escapeHtml(decision.reason)}</span>`);
  if (decision.found !== undefined) parts.push(`${decision.found} huecos`);
  return `<div class="entry">
    <div class="head"><b class="stage">${escapeHtml(decision.stage || "decisión")}</b>
    <span class="ms">${escapeHtml((decision.ts || "").slice(11, 19))}</span></div>
    <div class="trace">${parts.join(" · ")}</div>
    ${trace ? `<div class="trace">${trace}</div>` : ""}
    ${decision.payload ? `<pre>${escapeHtml(pretty(decision.payload))}</pre>` : ""}
  </div>`;
}

function toolRow(call) {
  const args = Object.keys(call.arguments || {}).length ? pretty(call.arguments) : "";
  return `<div class="entry">
    <div class="head"><b>${escapeHtml(call.name)}</b><span class="ms">${call.elapsed_ms ?? "?"} ms</span></div>
    ${args ? `<pre>${escapeHtml(args)}</pre>` : ""}
    <pre>${escapeHtml(pretty(call.result).slice(0, 700))}</pre>
  </div>`;
}

function submissionRow(submission) {
  const cls = submission.accepted ? "ok" : "err";
  const retries = submission.attempts > 1 ? ` · ${submission.attempts} intentos` : "";
  return `<div class="entry">
    <div class="head"><b class="${cls === "ok" ? "stage" : "reason"}">${escapeHtml(submission.action)}</b>
    <span class="ms">HTTP ${submission.status} · ${submission.elapsed_ms ?? "?"} ms${retries}</span></div>
    <pre>${escapeHtml(pretty(submission.payload))}</pre>
  </div>`;
}

function followupRow(email) {
  const sent = email.sent;
  const to = (email.to || []).join(", ") || "solo vista previa";
  const status = sent ? "enviado" : (email.reason || "guardado");
  return `<div class="entry">
    <div class="head"><b class="${sent ? "stage" : "reason"}">${escapeHtml(email.action || "correo")}</b>
    <span class="ms">${escapeHtml(status)}</span></div>
    <div class="trace">${escapeHtml(email.subject || "")} · ${escapeHtml(to)}</div>
    ${email.text ? `<pre>${escapeHtml(email.text)}</pre>` : ""}
  </div>`;
}

function clinicRow(call) {
  return `<div class="entry">
    <div class="head"><b>${escapeHtml(call.path)}</b><span class="ms">${call.elapsed_ms ?? "?"} ms</span></div>
    ${call.params ? `<pre>${escapeHtml(pretty(call.params))}</pre>` : ""}
    ${call.summary ? `<pre>${escapeHtml(pretty(call.summary).slice(0, 500))}</pre>` : ""}
  </div>`;
}

// ---- live feed ------------------------------------------------------

function applyEvent(message) {
  const { event, summary } = message;
  if (summary) {
    const existing = state.calls.get(summary.call_id) || {};
    state.calls.set(summary.call_id, { ...existing, ...summary, live: summary.status === "live" });
    renderCallLists();
    renderStats({ ...(state.lastStats || {}), live: countLive() });
  }
  if (!event || event.call_id !== state.selected) return;

  const detail = state.detail;
  if (!detail) return;
  const payload = event.payload || {};

  switch (event.kind) {
    case "stt_partial":
      el("partial").textContent = payload.text ? `…${payload.text}` : "";
      return;
    case "stt_final":
      el("partial").textContent = "";
      detail.transcript = detail.transcript || [];
      detail.transcript.push({ role: "caller", text: payload.text });
      break;
    case "agent_said":
      detail.transcript = detail.transcript || [];
      detail.transcript.push({ role: "agent", text: payload.text });
      break;
    case "interruption":
      detail.transcript = detail.transcript || [];
      detail.transcript.push({ role: "caller", text: `⟨corta al agente⟩ ${payload.heard || ""}` });
      break;
    case "tool_call":
      detail.tool_calls = detail.tool_calls || [];
      detail.tool_calls.push(payload);
      break;
    case "clinic_call":
      detail.clinic_calls = detail.clinic_calls || [];
      detail.clinic_calls.push(payload);
      break;
    case "decision":
      detail.decisions = detail.decisions || [];
      detail.decisions.push({ ...payload, ts: event.ts });
      break;
    case "submit":
      detail.submissions = detail.submissions || [];
      detail.submissions.push(payload);
      break;
    case "followup_email":
      detail.followup_emails = detail.followup_emails || [];
      detail.followup_emails.push(payload);
      break;
    case "patient_identified":
      detail.patient_full = payload.patient;
      break;
    default:
      break;
  }

  if (summary) {
    for (const [key, value] of Object.entries(summary)) {
      if (key === "tool_calls" || key === "clinic_calls" || key === "errors"
          || key === "transcript" || key === "decisions" || key === "submissions") {
        continue;
      }
      detail[key] = value;
    }
  }
  renderDetail();
}

function countLive() {
  let count = 0;
  for (const call of state.calls.values()) if (call.live) count += 1;
  return count;
}

function connect() {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${scheme}://${location.host}/api/console/stream`);

  socket.onopen = () => el("link-dot").classList.add("up");
  socket.onclose = () => {
    el("link-dot").classList.remove("up");
    setTimeout(connect, 1500);
  };
  socket.onmessage = (message) => {
    const data = JSON.parse(message.data);
    if (data.type === "hello") {
      renderOverview(data.overview);
      if (!state.selected) {
        const first = [...state.calls.values()].find((call) => call.live);
        if (first) selectCall(first.call_id);
      }
      return;
    }
    if (data.type === "ping") {
      state.lastStats = data.stats;
      renderStats(data.stats);
      return;
    }
    if (data.type === "call_started") {
      refreshOverview();
      if (!state.selected) selectCall(data.call_id);
      return;
    }
    if (data.type === "call_ended") {
      refreshOverview();
      return;
    }
    if (data.type === "event") applyEvent(data);
  };
}

async function refreshOverview() {
  try {
    const response = await fetch("/api/console/overview");
    if (response.ok) renderOverview(await response.json());
  } catch (error) {
    /* the socket will retry */
  }
}

document.querySelectorAll(".tab").forEach((tab) => {
  tab.onclick = () => {
    document.querySelectorAll(".tab").forEach((other) => other.classList.remove("active"));
    tab.classList.add("active");
    state.tab = tab.dataset.tab;
    ["decisions", "tools", "record"].forEach((name) => {
      el(`tab-${name}`).classList.toggle("hidden", name !== state.tab);
    });
  };
});

refreshOverview();
connect();
setInterval(() => {
  for (const call of state.calls.values()) if (call.live) call.duration_s = (call.duration_s || 0) + 1;
  renderCallLists();
}, 1000);
