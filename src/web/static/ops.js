/* Control de llamadas y agentes.
 *
 * Reads the same feed as the classic console — /api/console/overview for the
 * picture on arrival, /api/console/stream for everything after — and adds the
 * view the classic one has no room for: every agent at once, and what each of
 * them is doing this second.
 *
 * Icons come from /api/console/assets, drawn by Quiver at build time. When
 * there are none the CSS falls back to coloured discs, so the page is never
 * waiting on an asset to be useful.
 */

const state = {
  icons: {},
  selfAnimated: new Set(),
  calls: new Map(),
  selected: null,
  detail: null,
  tab: "decisions",
  stats: {},
  scope: "all",
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

const ACTIVITY = {
  idle: "idle",
  listening: "listening",
  thinking: "thinking",
  speaking: "speaking",
  ended: "ended",
};

function secs(ms) {
  const s = Math.max(0, ms) / 1000;
  if (s < 60) return `${s.toFixed(1)}s`;
  return `${Math.floor(s / 60)}m ${String(Math.floor(s % 60)).padStart(2, "0")}s`;
}

function dur(ms) {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return `${s}s`;
  return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
}

const clock = (iso) => String(iso || "").slice(11, 19) || "–";
const msOrDash = (value) => (value ? `${value} ms` : "–");

/** A count, whether the field arrived as one or as the list it counts.
 *
 * `summary()` sends tool_calls and clinic_calls as numbers; `detail()` spreads
 * the summary and then overwrites both with the full arrays. Reading either
 * shape here is cheaper than making the two payloads agree. */
const countOf = (value) => (Array.isArray(value) ? value.length : (value ?? 0));

// ---- the drawn parts -------------------------------------------------

function icon(name) {
  return state.icons[name] || "";
}

async function loadAssets() {
  try {
    const response = await fetch("/api/console/assets");
    if (!response.ok) return;
    const body = await response.json();
    state.icons = body.icons || {};
    state.selfAnimated = new Set((body.manifest && body.manifest.animated) || []);
    state.assetManifest = body.manifest || {};
  } catch (error) {
    /* no icons; the CSS fallbacks stand in */
  }
  paintStaticIcons();
}

/** Fill every placeholder that never changes: headings, buttons, the logo. */
function paintStaticIcons(root = document) {
  root.querySelectorAll("[data-icon]").forEach((node) => {
    const markup = icon(node.dataset.icon);
    if (markup) node.innerHTML = markup;
  });
}

// ---- header and counters --------------------------------------------

function renderOverview(overview) {
  state.overview = overview;
  el("clinic-name").textContent = overview.clinic || "Clínica Arenal";

  const p = overview.providers || {};
  const endpoint = (overview.endpoint && overview.endpoint.public_ws_url)
    || `wss://…:${(overview.endpoint || {}).port}${(overview.endpoint || {}).path || "/ws"}`;
  const pills = [
    `<span class="pill">stt <b>${escapeHtml(p.stt)}</b></span>`,
    `<span class="pill">model <b>${escapeHtml(p.llm)}</b></span>`,
    `<span class="pill">voice <b>${escapeHtml(p.tts)}</b></span>`,
    `<span class="pill">endpoint <b>${escapeHtml(endpoint)}</b></span>`,
  ];
  if (!overview.ready) {
    pills.push(`<span class="pill warn">missing keys: <b>${
      escapeHtml((overview.missing_keys || []).join(", "))}</b></span>`);
  }
  const manifest = state.assetManifest || {};
  if (manifest.model) {
    pills.push(`<span class="pill drawn">icons <b>${escapeHtml(manifest.model)}</b></span>`);
  }
  el("pills").innerHTML = pills.join("");

  state.calls.clear();
  (overview.live || []).forEach((call) => mergeSummary({ ...call, status: "live" }));
  (overview.recent || []).forEach((call) => {
    if (!state.calls.has(call.call_id)) mergeSummary(call);
  });

  renderStats(overview.stats);
  renderAgents();
  renderCalls();
}

const KPIS = [
  { key: "live", label: "Live", icon: "metric-live", tone: (v) => (v > 0 ? "good" : "") },
  { key: "peak_concurrency", label: "Peak concurrent", icon: "metric-peak" },
  { key: "median_response_ms", label: "Response p50", icon: "metric-latency", format: msOrDash },
  { key: "p90_response_ms", label: "Response p90", icon: "metric-latency", format: msOrDash },
  { key: "with_accepted_submission", label: "Accepted records", icon: "outcome-book",
    tone: (v) => (v > 0 ? "good" : "") },
  { key: "silent_calls", label: "No record", icon: "metric-silent",
    tone: (v) => (v > 0 ? "bad" : "") },
  { key: "interruptions", label: "Interruptions", icon: "metric-interruption" },
  { key: "calls_with_errors", label: "With errors", icon: "outcome-no_action",
    tone: (v) => (v > 0 ? "warn" : "") },
];

function renderStats(stats) {
  if (!stats) return;
  state.stats = stats;
  el("kpis").innerHTML = KPIS.map((kpi) => {
    const raw = stats[kpi.key] ?? 0;
    const tone = kpi.tone ? kpi.tone(raw) : "";
    const shown = kpi.format ? kpi.format(stats[kpi.key]) : raw;
    return `<div class="kpi ${tone}">
      <span class="k-icon">${icon(kpi.icon)}</span>
      <span class="k-text"><b>${escapeHtml(shown)}</b><span>${escapeHtml(kpi.label)}</span></span>
    </div>`;
  }).join("");
}

// ---- keeping the local picture ---------------------------------------

/** Fold one summary into what we hold, keeping the clocks running locally.
 *
 * `activity_ms` and `duration_s` are snapshots taken when the server sent them.
 * Storing the instant they started instead lets the cards count on without a
 * message per second from a server that has calls to answer.
 */
function mergeSummary(summary) {
  if (!summary || !summary.call_id) return null;
  const previous = state.calls.get(summary.call_id) || {};
  const call = { ...previous, ...summary };
  call.live = summary.status === "live";
  call.activityBase = Date.now() - (summary.activity_ms || 0);
  call.durationBase = Date.now() - (summary.duration_s || 0) * 1000;
  state.calls.set(call.call_id, call);
  return call;
}

const sorted = () => [...state.calls.values()].sort((a, b) => {
  if (a.live !== b.live) return a.live ? -1 : 1;
  return String(b.started_at).localeCompare(String(a.started_at));
});

// ---- the fleet -------------------------------------------------------

function renderAgents() {
  const live = sorted().filter((call) => call.live);
  el("agent-count").textContent = live.length;

  if (!live.length) {
    const scene = icon("empty-quiet");
    el("agent-grid").innerHTML = `<div class="empty">
      ${scene ? `<div class="scene">${scene}</div>` : ""}
      <div>No one on the line. The endpoint is listening.</div>
    </div>`;
    return;
  }

  el("agent-grid").innerHTML = live.map(agentCard).join("");
  el("agent-grid").querySelectorAll(".agent").forEach((node) => {
    node.onclick = () => selectCall(node.dataset.id);
  });
}

function agentCard(call) {
  const activity = call.activity || "idle";
  const who = (call.patient && call.patient.name) || call.from_number || call.call_id.slice(0, 12);
  const glyph = icon(`status-${activity}`);
  const selfAnimated = state.selfAnimated.has(`status-${activity}`) ? " self-animated" : "";

  const badges = [];
  if (call.stage) badges.push(`<span class="badge accent">${escapeHtml(call.stage)}</span>`);
  if (call.intent) badges.push(`<span class="badge">${escapeHtml(call.intent)}</span>`);
  badges.push(`<span class="badge">${call.turns ?? 0} turns</span>`);
  if (call.interruptions) {
    badges.push(`<span class="badge no">
      <span class="b-icon">${icon("metric-interruption")}</span>${call.interruptions}× cut
    </span>`);
  }
  if (call.median_response_ms) {
    badges.push(`<span class="badge">p50 ${call.median_response_ms} ms</span>`);
  }
  (call.actions || []).forEach((action) => {
    badges.push(`<span class="badge ${action.accepted ? "ok" : "err"}">
      <span class="b-icon">${icon(`outcome-${action.action}`)}</span>${escapeHtml(action.action)}
    </span>`);
  });
  if (call.errors) badges.push(`<span class="badge err">${call.errors} error(s)</span>`);

  const selected = state.selected === call.call_id ? " selected" : "";
  return `<article class="agent ${activity}${selected}" data-id="${escapeHtml(call.call_id)}">
    <div class="a-top">
      <span class="glyph${selfAnimated}">${glyph}</span>
      <span class="who">${escapeHtml(who)}</span>
      <span class="clock" data-clock>${dur(Date.now() - call.durationBase)}</span>
    </div>
    <div class="a-act">
      <span>${escapeHtml(ACTIVITY[activity] || activity)}</span>
      <span class="since" data-since>${secs(Date.now() - call.activityBase)}</span>
      ${call.current_tool ? `<span class="tool">${escapeHtml(call.current_tool)}</span>` : ""}
    </div>
    ${call.last_caller ? `<div class="line"><em>caller</em><span>${escapeHtml(call.last_caller)}</span></div>` : ""}
    ${call.last_agent ? `<div class="line"><em>agent</em><span>${escapeHtml(call.last_agent)}</span></div>` : ""}
    <div class="a-foot">${badges.join("")}</div>
  </article>`;
}

// ---- the calls table -------------------------------------------------

const SCOPES = {
  all: () => true,
  submitted: (call) => (call.actions || []).length > 0,
  silent: (call) => !call.live && !(call.actions || []).length,
  errors: (call) => Boolean(call.errors),
};

function renderCalls() {
  const rows = sorted().filter(SCOPES[state.scope] || SCOPES.all);
  el("call-count").textContent = rows.length;
  el("calls-body").innerHTML = rows.length
    ? rows.map(callRow).join("")
    : `<tr class="empty-row"><td colspan="8">Nothing matches this filter</td></tr>`;

  el("calls-body").querySelectorAll("tr[data-id]").forEach((node) => {
    node.onclick = () => selectCall(node.dataset.id);
  });
}

function callRow(call) {
  const who = (call.patient && call.patient.name) || call.from_number || call.call_id.slice(0, 12);
  const activity = call.live ? (call.activity || "idle") : "ended";
  const actions = call.actions || [];

  const record = actions.length
    ? actions.map((action) => `<span class="badge ${action.accepted ? "ok" : "err"}">
        <span class="b-icon">${icon(`outcome-${action.action}`)}</span>${escapeHtml(action.action)}
      </span>`).join("")
    : (call.live
      ? '<span class="dim">–</span>'
      : `<span class="badge no"><span class="b-icon">${icon("metric-silent")}</span>no record</span>`);

  const selected = state.selected === call.call_id ? " selected" : "";
  const duration = call.live
    ? dur(Date.now() - call.durationBase)
    : dur((call.duration_s || 0) * 1000);

  return `<tr class="${selected.trim()}" data-id="${escapeHtml(call.call_id)}">
    <td class="dim">${escapeHtml(clock(call.started_at))}</td>
    <td class="who">${escapeHtml(who)}</td>
    <td><span class="act-tag ${activity}">${escapeHtml(ACTIVITY[activity] || activity)}</span></td>
    <td class="num" data-clock>${duration}</td>
    <td class="num">${call.turns ?? 0}</td>
    <td class="num">${call.interruptions || ""}</td>
    <td class="num">${call.median_response_ms || ""}</td>
    <td class="acts">${record}${call.errors ? `<span class="badge err">${call.errors}</span>` : ""}</td>
  </tr>`;
}

/** Move the clocks on without rebuilding the cards, so hover and selection
 *  survive and a twenty-agent grid does not repaint every second. */
function tickClocks() {
  const now = Date.now();
  document.querySelectorAll(".agent[data-id]").forEach((node) => {
    const call = state.calls.get(node.dataset.id);
    if (!call) return;
    const clockNode = node.querySelector("[data-clock]");
    const sinceNode = node.querySelector("[data-since]");
    if (clockNode) clockNode.textContent = dur(now - call.durationBase);
    if (sinceNode) sinceNode.textContent = secs(now - call.activityBase);
  });
  document.querySelectorAll("#calls-body tr[data-id]").forEach((node) => {
    const call = state.calls.get(node.dataset.id);
    if (!call || !call.live) return;
    const cell = node.querySelector("[data-clock]");
    if (cell) cell.textContent = dur(now - call.durationBase);
  });
}

// ---- the selected call ----------------------------------------------

async function selectCall(callId) {
  if (!callId) return;
  state.selected = callId;
  renderAgents();
  renderCalls();
  try {
    const response = await fetch(`/api/console/calls/${encodeURIComponent(callId)}`);
    if (!response.ok) return;
    state.detail = await response.json();
    renderDetail();
  } catch (error) {
    /* the live feed will fill it in */
  }
}

function renderDetail() {
  const detail = state.detail;
  if (!detail) return;

  const patient = detail.patient_full || {};
  const name = patient.full_name
    || (detail.patient && detail.patient.name)
    || detail.from_number
    || "Unidentified";

  const chips = [];
  if (patient.patient_id) chips.push(`<span class="badge">${escapeHtml(patient.patient_id)}</span>`);
  if (patient.insurer) chips.push(`<span class="badge">${escapeHtml(patient.insurer)}</span>`);
  if (patient.has_visited_before !== undefined) {
    chips.push(`<span class="badge">${patient.has_visited_before ? "known patient" : "first visit"}</span>`);
  }
  if (detail.language) chips.push(`<span class="badge">${escapeHtml(detail.language)}</span>`);
  if (detail.stage) chips.push(`<span class="badge accent">${escapeHtml(detail.stage)}</span>`);
  (detail.actions || []).forEach((action) => {
    chips.push(`<span class="badge ${action.accepted ? "ok" : "err"}">
      <span class="b-icon">${icon(`outcome-${action.action}`)}</span>${escapeHtml(action.action)} · ${escapeHtml(action.status)}
    </span>`);
  });

  el("call-head").innerHTML = `
    <div class="name">${escapeHtml(name)}</div>
    <div class="meta">${escapeHtml(detail.from_number || "number withheld")} ·
      ${countOf(detail.turns)} turns · ${countOf(detail.tool_calls)} tools ·
      ${countOf(detail.clinic_calls)} clinic calls · ${dur((detail.duration_s || 0) * 1000)}</div>
    <div class="chips">${chips.join("")}</div>
    ${patient.note ? `<div class="note">${escapeHtml(patient.note)}</div>` : ""}`;

  renderTape(detail);
  el("transcript").innerHTML = (detail.transcript || []).map(turnRow).join("");
  el("transcript").scrollTop = el("transcript").scrollHeight;
  renderTabs();
}

/** Put the players up only when what they would play has changed.
 *
 * renderDetail runs on every event of the selected call, and replacing an
 * <audio> element is the same as stopping it. Rebuilding this only when the call
 * or its recordings change is what lets a tape keep playing while the console
 * carries on updating around it.
 */
function renderTape(detail) {
  const rec = detail.recordings || {};
  const key = `${detail.call_id}|${Boolean(rec.conversation)}`;
  if (state.tapeKey === key) return;
  state.tapeKey = key;
  el("tape").innerHTML = tapePlayers(detail);
  const player = el("tape").querySelector("audio");
  if (!player) return;
  player.addEventListener("loadedmetadata", () => {
    const slot = el("tape").querySelector(".len");
    if (slot && Number.isFinite(player.duration)) slot.textContent = dur(player.duration * 1000);
  });
}

function tapePlayers(detail) {
  const id = detail.call_id;
  if (!id) return "";
  const rec = detail.recordings || {};

  const head = `<div class="tape-head">
    <span class="h-icon">${icon("metric-live")}</span> Recording
    <span class="len"></span>
  </div>`;

  if (!rec.conversation) {
    const why = String(id).startsWith("rehearsal-")
      ? "Text rehearsal — there is no audio to play."
      : "No recording for this call.";
    return `<div class="tape">${head}<div class="none">${escapeHtml(why)}</div></div>`;
  }

  const src = `/api/console/calls/${encodeURIComponent(id)}/audio/conversation`;
  return `<div class="tape">${head}
    <div class="track conversation">
      <audio controls preload="metadata" src="${escapeHtml(src)}"></audio>
      <a class="grab" href="${escapeHtml(src)}" download="${escapeHtml(id)}-conversation.wav"
         title="Download the WAV">WAV</a>
    </div>
  </div>`;
}

function turnRow(turn) {
  const who = turn.role === "agent" ? "agent" : "caller";
  const cut = turn.cut ? " cut" : "";
  return `<div class="turn ${turn.role}${cut}">
    <div class="bubble"><span class="tag">${who}</span>${escapeHtml(turn.text)}</div>
  </div>`;
}

function renderTabs() {
  const detail = state.detail || {};

  el("tab-decisions").innerHTML = (detail.decisions || []).length
    ? detail.decisions.map(decisionRow).join("")
    : '<div class="entry"><div class="trace">No decisions yet.</div></div>';

  el("tab-tools").innerHTML = (detail.tool_calls || []).length
    ? detail.tool_calls.map(toolRow).join("")
    : '<div class="entry"><div class="trace">No tools yet.</div></div>';

  const submissions = detail.submissions || [];
  const clinic = (detail.clinic_calls || []).slice(-12);
  el("tab-record").innerHTML =
    (submissions.length
      ? submissions.map(submissionRow).join("")
      : '<div class="entry"><div class="trace">Nothing submitted yet.</div></div>')
    + (clinic.length
      ? `<div class="group-label">Clinic lookups</div>${clinic.map(clinicRow).join("")}`
      : "");
}

function decisionRow(decision) {
  const trace = (decision.trace || []).map((line) => `<div>· ${escapeHtml(line)}</div>`).join("");
  const parts = [];
  if (decision.why) parts.push(escapeHtml(decision.why));
  if (decision.reason) parts.push(`<span class="reason">${escapeHtml(decision.reason)}</span>`);
  if (decision.found !== undefined) parts.push(`${decision.found} slots`);
  return `<div class="entry">
    <div class="head"><b class="stage">${escapeHtml(decision.stage || "decision")}</b>
      <span class="ms">${escapeHtml(clock(decision.ts))}</span></div>
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
  const ok = submission.accepted;
  const retries = submission.attempts > 1 ? ` · ${submission.attempts} attempts` : "";
  return `<div class="entry">
    <div class="head">
      <b class="${ok ? "stage" : "reason"}">
        <span class="b-icon">${icon(`outcome-${submission.action}`)}</span>${escapeHtml(submission.action)}
      </b>
      <span class="ms">HTTP ${submission.status} · ${submission.elapsed_ms ?? "?"} ms${retries}</span>
    </div>
    <pre>${escapeHtml(pretty(submission.payload))}</pre>
  </div>`;
}

function clinicRow(call) {
  return `<div class="entry">
    <div class="head"><b>${escapeHtml(call.path)}</b><span class="ms">${call.elapsed_ms ?? "?"} ms</span></div>
    ${call.params ? `<pre>${escapeHtml(pretty(call.params))}</pre>` : ""}
    ${call.summary ? `<pre>${escapeHtml(pretty(call.summary).slice(0, 500))}</pre>` : ""}
  </div>`;
}

// ---- the live feed ---------------------------------------------------

function applyEvent(message) {
  const { event, summary } = message;
  if (summary) {
    mergeSummary(summary);
    renderAgents();
    renderCalls();
  }
  if (!event || event.call_id !== state.selected) return;

  const detail = state.detail;
  if (!detail) return;
  const payload = event.payload || {};
  detail.transcript = detail.transcript || [];

  switch (event.kind) {
    case "stt_partial":
      el("partial").textContent = payload.text ? `…${payload.text}` : "";
      return;
    case "stt_final":
      el("partial").textContent = "";
      detail.transcript.push({ role: "caller", text: payload.text });
      break;
    case "agent_said":
      detail.transcript.push({ role: "agent", text: payload.text });
      break;
    case "interruption":
      detail.transcript.push({
        role: "caller", cut: true,
        text: `⟨cuts the agent⟩ ${payload.heard || ""}`,
      });
      break;
    case "tool_call":
      (detail.tool_calls = detail.tool_calls || []).push(payload);
      break;
    case "clinic_call":
      (detail.clinic_calls = detail.clinic_calls || []).push(payload);
      break;
    case "decision":
      (detail.decisions = detail.decisions || []).push({ ...payload, ts: event.ts });
      break;
    case "submit":
      (detail.submissions = detail.submissions || []).push(payload);
      break;
    case "patient_identified":
      detail.patient_full = payload.patient;
      break;
    default:
      break;
  }

  if (summary) Object.assign(detail, summary);
  renderDetail();
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
        const first = sorted().find((call) => call.live);
        if (first) selectCall(first.call_id);
      }
      return;
    }
    if (data.type === "ping") {
      renderStats(data.stats);
      return;
    }
    if (data.type === "call_started" || data.type === "call_ended") {
      refreshOverview();
      if (data.type === "call_started" && !state.selected) selectCall(data.call_id);
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

// ---- the one control on the page ------------------------------------

function openRehearsal(open) {
  el("rehearse-modal").classList.toggle("hidden", !open);
  if (open) el("rehearse-turns").focus();
}

async function runRehearsal() {
  const turns = el("rehearse-turns").value
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);

  const status = el("rehearse-status");
  if (!turns.length) {
    status.className = "status bad";
    status.textContent = "At least one turn is required.";
    return;
  }

  const button = el("rehearse-run");
  button.disabled = true;
  status.className = "status";
  status.textContent = `Rehearsing ${turns.length} turn(s)…`;

  try {
    const response = await fetch("/api/console/rehearse", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        turns,
        from_number: el("rehearse-number").value.trim() || null,
        dry_run: el("rehearse-dry").checked,
        label: "console",
      }),
    });
    const body = await response.json();
    if (!response.ok) {
      status.className = "status bad";
      status.textContent = body.detail || `Failed with HTTP ${response.status}`;
      return;
    }
    const submissions = body.submissions || [];
    status.className = "status ok";
    status.textContent = submissions.length
      ? `Recorded: ${submissions.map((s) => s.action).join(", ")}`
      : "Finished with no record.";
    await refreshOverview();
    await selectCall(body.call_id);
    openRehearsal(false);
  } catch (error) {
    status.className = "status bad";
    status.textContent = String(error);
  } finally {
    button.disabled = false;
  }
}

// ---- resizable panes -------------------------------------------------

const LAYOUT_KEY = "elturno.ops.layout";

function loadLayout() {
  try {
    return JSON.parse(localStorage.getItem(LAYOUT_KEY)) || {};
  } catch (error) {
    return {};
  }
}

const layout = loadLayout();

function saveLayout() {
  try {
    localStorage.setItem(LAYOUT_KEY, JSON.stringify(layout));
  } catch (error) {
    /* storage off: the panes still resize, the browser just forgets */
  }
}

/* Each draggable pane described once: which custom property sizes it, how to
   read a size out of a pointer, and how small the pane on the other side is
   allowed to get. The CSS defaults stay in charge until something is stored. */
const PANES = {
  detail: {
    key: "detailWidth",
    cssVar: "--detail-w",
    axis: "x",
    grow: "ArrowLeft",
    // The pane's far edge holds still while dragging, so measuring from it is
    // exact and needs none of main's padding arithmetic.
    fromPointer: (event) => el("detail-pane").getBoundingClientRect().right - event.clientX,
    current: () => el("detail-pane").getBoundingClientRect().width,
    bounds: () => {
      const main = document.querySelector("main");
      const room = main.clientWidth - 36 - 11;   // side padding, then the gutter
      return { min: 300, max: Math.max(300, room - 360) };
    },
  },
  fleet: {
    key: "fleetHeight",
    cssVar: "--fleet-h",
    axis: "y",
    grow: "ArrowDown",
    fromPointer: (event) => event.clientY - el("left-col").getBoundingClientRect().top,
    current: () => el("agent-grid").closest(".panel").getBoundingClientRect().height,
    bounds: () => {
      const room = el("left-col").clientHeight - 11;
      return { min: 120, max: Math.max(120, room - 140) };
    },
  },
};

function sizePane(name, px, remember = true) {
  const pane = PANES[name];
  const { min, max } = pane.bounds();
  const size = Math.round(Math.min(max, Math.max(min, px)));
  document.documentElement.style.setProperty(pane.cssVar, `${size}px`);
  if (remember) layout[pane.key] = size;
  return size;
}

function resetPane(name) {
  const pane = PANES[name];
  document.documentElement.style.removeProperty(pane.cssVar);
  delete layout[pane.key];
  saveLayout();
}

function applyLayout(remember = true) {
  Object.entries(PANES).forEach(([name, pane]) => {
    if (layout[pane.key]) sizePane(name, layout[pane.key], remember);
  });
}

function setupSplitter(handleId, name) {
  const handle = el(handleId);
  const pane = PANES[name];
  const cursor = pane.axis === "x" ? "resizing-x" : "resizing-y";
  let dragging = false;

  handle.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    dragging = true;
    // Captured, so a fast drag that outruns the 11px handle keeps resizing
    // instead of dropping the gesture over whatever it passed onto. Guarded
    // because capturing a pointer that is not live throws, and a splitter that
    // cannot capture should still drag.
    try {
      handle.setPointerCapture(event.pointerId);
    } catch (error) {
      /* no capture; the listeners below still fire */
    }
    handle.classList.add("dragging");
    document.body.classList.add(cursor);
    event.preventDefault();
  });

  handle.addEventListener("pointermove", (event) => {
    if (dragging) sizePane(name, pane.fromPointer(event));
  });

  const stop = () => {
    if (!dragging) return;
    dragging = false;
    handle.classList.remove("dragging");
    document.body.classList.remove(cursor);
    saveLayout();
  };
  handle.addEventListener("pointerup", stop);
  handle.addEventListener("pointercancel", stop);
  handle.addEventListener("lostpointercapture", stop);

  handle.addEventListener("dblclick", () => resetPane(name));

  // A separator with no keyboard is a separator half the room cannot use.
  handle.addEventListener("keydown", (event) => {
    const step = event.shiftKey ? 48 : 16;
    if (event.key === pane.grow) sizePane(name, pane.current() + step);
    else if (event.key === (pane.axis === "x" ? "ArrowRight" : "ArrowUp")) {
      sizePane(name, pane.current() - step);
    } else if (event.key === "Home" || event.key === "End") {
      resetPane(name);
      return;
    } else return;
    event.preventDefault();
    saveLayout();
  });
}

// A window that shrank must not leave a pane wider than the window; re-clamped
// without remembering, so the size asked for survives the window coming back.
window.addEventListener("resize", () => applyLayout(false));

// ---- wiring ---------------------------------------------------------

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

el("call-filter").querySelectorAll("button").forEach((button) => {
  button.onclick = () => {
    el("call-filter").querySelectorAll("button").forEach((other) => other.classList.remove("on"));
    button.classList.add("on");
    state.scope = button.dataset.scope;
    renderCalls();
  };
});

el("btn-rehearse").onclick = () => openRehearsal(true);
el("rehearse-cancel").onclick = () => openRehearsal(false);
el("rehearse-run").onclick = runRehearsal;
el("rehearse-modal").onclick = (event) => {
  if (event.target === el("rehearse-modal")) openRehearsal(false);
};
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") openRehearsal(false);
});

setupSplitter("split-detail", "detail");
setupSplitter("split-fleet", "fleet");
applyLayout();

loadAssets().then(() => {
  refreshOverview();
  connect();
});
setInterval(tickClocks, 500);
