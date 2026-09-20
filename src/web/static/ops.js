/* Socket Wizard — control site.
 *
 * Reads the same feed as the classic console — /api/console/overview for the
 * picture on arrival, /api/console/stream for everything after — and spreads it
 * across pages so the floor, the history, a single transcript and a rehearsal
 * each have a window of their own.
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
  tab: "why",
  stats: {},
  scope: "all",
  page: "live",
  returnTo: "#/",
  demoStory: null,
};

const DEMO = location.pathname.replace(/\/+$/, "") === "/demo";
if (DEMO) document.body.dataset.demo = "1";

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

// ---- routing ---------------------------------------------------------

function parseRoute(hash = location.hash) {
  const path = (hash.replace(/^#/, "") || "/").replace(/\/+$/, "") || "/";
  if (path === "/calls") return { page: "calls" };
  const match = path.match(/^\/calls\/([^/]+)$/);
  if (match) return { page: "call", id: decodeURIComponent(match[1]) };
  if (path === "/rehearse") return { page: "rehearse" };
  if (path === "/talk") return { page: "talk" };
  if (path === "/reliability") return { page: "reliability" };
  return { page: "live" };
}

function pathFor(page, id) {
  if (page === "calls") return "#/calls";
  if (page === "call") return `#/calls/${encodeURIComponent(id)}`;
  if (page === "rehearse") return "#/rehearse";
  if (page === "talk") return "#/talk";
  if (page === "reliability") return "#/reliability";
  return "#/";
}

function go(path) {
  const hash = path.startsWith("#") ? path : `#${path}`;
  if (location.hash === hash) {
    applyRoute();
    return;
  }
  location.hash = hash;
}

function applyRoute() {
  const route = parseRoute();
  state.page = route.page;
  document.body.dataset.page = route.page;

  document.querySelectorAll(".view").forEach((view) => {
    const hide = view.id !== `view-${route.page}`;
    view.classList.toggle("hidden", hide);
    view.toggleAttribute("hidden", hide);
  });

  document.querySelectorAll(".nav a[data-nav]").forEach((link) => {
    const current = route.page === "call" ? "calls" : route.page;
    const on = link.dataset.nav === current;
    link.classList.toggle("on", on);
    if (on) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });

  const titles = {
    live: "Live · Socket Wizard",
    calls: "Calls · Socket Wizard",
    call: "Call · Socket Wizard",
    rehearse: "Rehearse · Socket Wizard",
    talk: "Talk · Socket Wizard",
    reliability: "Reliability · Socket Wizard",
  };
  document.title = titles[route.page] || "Socket Wizard";

  if (route.page === "call") {
    el("btn-back").setAttribute("href", state.returnTo || "#/");
    if (route.id && state.selected !== route.id) {
      selectCall(route.id, { navigate: false });
    }
  } else if (route.page === "rehearse") {
    el("rehearse-turns").focus();
  } else if (route.page === "reliability") {
    loadReliability();
  }

  if (route.page !== "talk" && window.Talk && Talk.active()) {
    Talk.hangup();
  }
}

function rememberReturn() {
  if (state.page === "calls") state.returnTo = "#/calls";
  else if (state.page === "live") state.returnTo = "#/";
}

document.querySelectorAll("a[data-nav]").forEach((link) => {
  link.addEventListener("click", (event) => {
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) {
      return;
    }
    event.preventDefault();
    go(link.getAttribute("href"));
  });
});

window.addEventListener("hashchange", applyRoute);

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
  el("clinic-name").textContent = `AI front desk for ${overview.clinic || "Clínica Arenal"}`;

  const p = overview.providers || {};
  const endpoint = (overview.endpoint && overview.endpoint.public_ws_url)
    || `wss://…:${(overview.endpoint || {}).port}${(overview.endpoint || {}).path || "/ws"}`;
  const pills = [
    `<span class="pill">stt <b>${escapeHtml(p.stt)}</b>${p.stt_model ? ` · ${escapeHtml(p.stt_model)}` : ""}</span>`,
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
  if (state.demoStory) ingestDemoStory(state.demoStory);
  renderCalls();
  renderRecent();
}

const LANG_SHORT = { es: "ES", en: "EN", ca: "CA" };

function langSummary(langs) {
  if (!langs || typeof langs !== "object") return "–";
  const parts = Object.entries(langs).map(([code, n]) => `${LANG_SHORT[code] || code} ${n}`);
  return parts.join(" · ") || "–";
}

const KPIS = [
  { key: "live", label: "Live calls", icon: "metric-live", tone: (v) => (v > 0 ? "good" : "") },
  { key: "with_accepted_submission", label: "Accepted records", icon: "outcome-book",
    tone: (v) => (v > 0 ? "good" : "") },
  { key: "languages", label: "Languages", icon: "metric-live", format: langSummary },
  { key: "median_response_ms", label: "Median response", icon: "metric-latency", format: msOrDash },
  { key: "peak_concurrency", label: "Peak concurrency", icon: "metric-peak" },
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

function callerName(call) {
  return (call.patient && call.patient.name) || call.from_number || call.call_id.slice(0, 12);
}

// ---- the fleet -------------------------------------------------------

function renderAgents() {
  const live = sorted().filter((call) => call.live);
  el("agent-count").textContent = live.length;

  const navCount = el("nav-live-count");
  navCount.textContent = String(live.length);
  navCount.classList.toggle("is-zero", live.length === 0);

  const lede = el("live-lede");
  if (lede) {
    lede.textContent = live.length
      ? `${live.length} on the line. Open a card to read the transcript.`
      : "No one on the line. The endpoint is listening.";
  }

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
    node.onclick = () => {
      rememberReturn();
      selectCall(node.dataset.id);
    };
  });
}

function agentCard(call) {
  const activity = call.activity || "idle";
  const who = callerName(call);
  const glyph = icon(`status-${activity}`);
  const selfAnimated = state.selfAnimated.has(`status-${activity}`) ? " self-animated" : "";

  const badges = [];
  if (call.language) {
    badges.push(`<span class="badge">${escapeHtml((LANG_SHORT[call.language] || call.language).toUpperCase())}</span>`);
  }
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
    const open = () => {
      rememberReturn();
      selectCall(node.dataset.id);
    };
    node.onclick = open;
    node.onkeydown = (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        open();
      }
    };
  });
}

function callRow(call) {
  const who = callerName(call);
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

  return `<tr class="${selected.trim()}" data-id="${escapeHtml(call.call_id)}" tabindex="0">
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

function renderRecent() {
  const node = el("recent-list");
  const rows = sorted().slice(0, 8);
  if (!rows.length) {
    node.innerHTML = `<div class="empty-inline">No calls yet.</div>`;
    return;
  }
  node.innerHTML = rows.map((call) => {
    const activity = call.live ? (call.activity || "idle") : "ended";
    const duration = call.live
      ? dur(Date.now() - call.durationBase)
      : dur((call.duration_s || 0) * 1000);
    return `<button type="button" class="recent-row" data-id="${escapeHtml(call.call_id)}">
      <span class="act-tag ${activity}">${escapeHtml(ACTIVITY[activity] || activity)}</span>
      <span class="who">${escapeHtml(callerName(call))}</span>
      <span class="dim">${escapeHtml(clock(call.started_at))} · ${escapeHtml(duration)}</span>
      <span class="acts">${call.turns ?? 0} turns</span>
    </button>`;
  }).join("");
  node.querySelectorAll("[data-id]").forEach((button) => {
    button.onclick = () => {
      rememberReturn();
      selectCall(button.dataset.id);
    };
  });
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

async function selectCall(callId, { navigate = true } = {}) {
  if (!callId) return;
  state.selected = callId;
  el("partial").textContent = "";
  renderAgents();
  renderCalls();
  renderRecent();
  if (navigate) go(pathFor("call", callId));
  if (state.demoStory && callId === state.demoStory.call_id) {
    state.detail = state.demoStory;
    renderDetail();
    return;
  }
  try {
    const response = await fetch(`/api/console/calls/${encodeURIComponent(callId)}`);
    if (!response.ok) {
      el("call-head").innerHTML = `<div class="call-head-empty">This call is not in the log.</div>`;
      return;
    }
    state.detail = _hydrateDetail(await response.json());
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

  if (state.page === "call") {
    document.title = `${name} · Socket Wizard`;
  }

  renderProduct(detail);
  renderTape(detail);
  const turns = detail.transcript || [];
  el("transcript").innerHTML = turns.length
    ? turns.map(turnRow).join("")
    : '<div class="transcript-empty">No transcript yet.</div>';
  el("transcript").scrollTop = el("transcript").scrollHeight;
  paintStaticIcons(el("tape").parentElement || document);
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
  const key = [
    detail.call_id,
    Boolean(rec.inbound),
    Boolean(rec.outbound),
    Boolean(rec.conversation),
  ].join("|");
  if (state.tapeKey === key) return;
  state.tapeKey = key;
  el("tape").innerHTML = tapePlayers(detail);
  el("tape").querySelectorAll("audio").forEach((player) => {
    player.addEventListener("loadedmetadata", () => {
      const slot = player.closest(".track")?.querySelector(".len");
      if (slot && Number.isFinite(player.duration)) slot.textContent = dur(player.duration * 1000);
    });
    // One track at a time — three sides talking over each other is useless.
    player.addEventListener("play", () => {
      el("tape").querySelectorAll("audio").forEach((other) => {
        if (other !== player) other.pause();
      });
    });
  });
}

function tapePlayers(detail) {
  const id = detail.call_id;
  if (!id) return "";
  const rec = detail.recordings || {};
  const hasAny = rec.inbound || rec.outbound || rec.conversation;

  const head = `<div class="tape-head">
    <span class="h-icon">${icon("metric-live")}</span> Recording
  </div>`;

  if (!hasAny) {
    const why = String(id).startsWith("rehearsal-")
      ? "Text rehearsal — there is no audio to play."
      : "No recording for this call.";
    return `<div class="tape">${head}<div class="none">${escapeHtml(why)}</div></div>`;
  }

  const tracks = [];
  if (rec.inbound) tracks.push(trackRow(id, "inbound", "patient", "Patient"));
  if (rec.outbound) tracks.push(trackRow(id, "outbound", "clinic", "Clinic"));
  if (rec.conversation) tracks.push(trackRow(id, "conversation", "conversation", "Full call"));
  return `<div class="tape">${head}${tracks.join("")}</div>`;
}

function trackRow(id, track, role, label) {
  const src = `/api/console/calls/${encodeURIComponent(id)}/audio/${track}`;
  return `<div class="track ${role}">
    <span class="who">${escapeHtml(label)}</span>
    <audio controls preload="metadata" src="${escapeHtml(src)}"></audio>
    <div class="track-meta">
      <span class="len"></span>
      <a class="grab" href="${escapeHtml(src)}" download="${escapeHtml(id)}-${track}.wav"
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

function switchTab(name) {
  state.tab = name;
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.classList.toggle("active", tab.dataset.tab === name);
  });
  ["why", "safety", "decisions", "tools", "record"].forEach((key) => {
    const node = el(`tab-${key}`);
    if (node) node.classList.toggle("hidden", key !== name);
  });
}

function renderProduct(detail) {
  const product = detail.product || {};
  const ctx = el("product-context");
  const journey = el("product-journey");
  const resolution = el("product-resolution");
  if (ctx) ctx.innerHTML = patientContextHtml(product.patient_context);
  if (journey) journey.innerHTML = journeyHtml(product.journey) + intentsHtml(product.intents);
  if (resolution) resolution.innerHTML = resolutionHtml(product.resolution, detail);
}

function patientContextHtml(ctx) {
  if (!ctx || !ctx.name) return "";
  const rows = [];
  if (ctx.regular && ctx.visit_count) {
    rows.push(`<div><b>Regular patient</b><span>${escapeHtml(ctx.visit_count)} visits</span></div>`);
  } else if (ctx.visit_count) {
    rows.push(`<div><b>Visits</b><span>${escapeHtml(ctx.visit_count)}</span></div>`);
  }
  const last = ctx.last_visit || {};
  if (last.provider_name || last.when) {
    rows.push(`<div><b>Last visit</b><span>${escapeHtml([last.provider_name, last.when].filter(Boolean).join(" · "))}</span></div>`);
  }
  const up = ctx.upcoming || {};
  if (up.when || up.slot) {
    rows.push(`<div><b>Upcoming</b><span>${escapeHtml(up.when || up.slot)}</span></div>`);
  }
  if (ctx.insurer) rows.push(`<div><b>Insurance</b><span>${escapeHtml(ctx.insurer)}</span></div>`);
  if (ctx.national_id) rows.push(`<div><b>DNI/NIE</b><span>${escapeHtml(ctx.national_id)}</span></div>`);
  if (ctx.phone) rows.push(`<div><b>Phone</b><span>${escapeHtml(ctx.phone)}</span></div>`);
  if (ctx.email) rows.push(`<div><b>Email</b><span>${escapeHtml(ctx.email)}</span></div>`);
  if (ctx.date_of_birth) rows.push(`<div><b>Born</b><span>${escapeHtml(ctx.date_of_birth)}</span></div>`);
  if (ctx.patient_id) rows.push(`<div><b>Patient id</b><span>${escapeHtml(ctx.patient_id)}</span></div>`);
  const access = ctx.hearing_support
    ? `<div class="access">Accessibility · hearing support recommended</div>`
    : "";
  return `<div class="product-card context">
    <div class="product-label">Patient context</div>
    <div class="context-name">${escapeHtml(ctx.name)}</div>
    <div class="context-grid">${rows.join("")}</div>
    ${access}
    ${ctx.note ? `<div class="note">${escapeHtml(ctx.note)}</div>` : ""}
  </div>`;
}

function journeyHtml(steps) {
  if (!steps || !steps.length) return "";
  return `<div class="product-card journey">
    <div class="product-label">Patient journey</div>
    <ol>${steps.map((step) => `<li class="${escapeHtml(step.state || "done")}">
      <span class="dot"></span>
      <div>
        <b>${escapeHtml(step.label)}</b>
        ${step.detail ? `<span>${escapeHtml(step.detail)}</span>` : ""}
      </div>
    </li>`).join("")}</ol>
  </div>`;
}

function intentsHtml(intents) {
  if (!intents || !intents.length) return "";
  return `<div class="product-card intents">
    <div class="product-label">Active intents</div>
    <ol>${intents.map((row, i) => {
      const done = row.state === "done";
      const last = (row.steps || []).slice(-1)[0] || row.state;
      return `<li class="${escapeHtml(row.state || "active")}">
        <span class="n">${i + 1}</span>
        <div>
          <b>${escapeHtml(row.title)}${done ? " ✓" : ""}</b>
          <span>${escapeHtml(row.detail || last || "")}</span>
        </div>
      </li>`;
    }).join("")}</ol>
  </div>`;
}

function resolutionHtml(res, detail) {
  if (!res || !res.closed) return "";
  const outcomes = (res.outcomes || []).map((row) => {
    const mark = row.accepted ? "✓" : "·";
    const bits = [row.when, row.provider_id, row.location_id, row.reason].filter(Boolean);
    return `<div class="outcome">
      <b>${escapeHtml((row.action || "").toUpperCase())} ${mark}</b>
      <span>${escapeHtml(bits.join(" · ") || row.status || "")}</span>
    </div>`;
  }).join("");
  const follow = res.followup || {};
  const followLine = follow.subject
    ? `Email ${follow.sent ? "sent" : "saved"} · ${follow.ics ? "calendar generated" : "preview"}`
    : "";
  const mins = Math.floor((res.duration_s || 0) / 60);
  const secsLeft = Math.round((res.duration_s || 0) % 60);
  const durLabel = `${mins}:${String(secsLeft).padStart(2, "0")}`;
  const id = detail.call_id || "";
  const maps = follow.maps_url
    ? `<a class="btn ghost" href="${escapeHtml(follow.maps_url)}" target="_blank" rel="noreferrer">Directions</a>`
    : "";
  const ics = follow.ics && id && !String(id).startsWith("demo-")
    ? `<a class="btn ghost" href="/api/console/calls/${encodeURIComponent(id)}/ics">Calendar</a>`
    : "";
  return `<div class="product-card resolution">
    <div class="product-label">Call resolved</div>
    <div class="res-grid">
      <div><b>Patient</b><span>${escapeHtml(res.patient || "—")}</span></div>
      <div><b>Language</b><span>${escapeHtml(res.language || "—")}</span></div>
      <div><b>Intent</b><span>${escapeHtml((res.intents || []).join(" · ") || "—")}</span></div>
      <div><b>Duration</b><span>${escapeHtml(durLabel)}</span></div>
      <div><b>Interruptions</b><span>${escapeHtml(res.interruptions || 0)}</span></div>
      ${followLine ? `<div><b>Follow-up</b><span>${escapeHtml(followLine)}</span></div>` : ""}
    </div>
    ${outcomes}
    <div class="res-actions">
      <button type="button" class="btn ghost" data-res="replay">Replay call</button>
      <button type="button" class="btn ghost" data-res="why">Why this decision</button>
      <button type="button" class="btn ghost" data-res="trace">Technical trace</button>
      ${maps}${ics}
    </div>
  </div>`;
}

function whyHtml(why) {
  if (!why) {
    return '<div class="entry"><div class="trace">No additional decision trace available.</div></div>';
  }
  const facts = (why.facts || []).map((row) => `<div class="why-row">
    <b>${escapeHtml(row.label)}</b><span>${escapeHtml(row.value)}</span>
  </div>`).join("");
  return `<div class="why">
    <div class="product-label">${escapeHtml(why.headline || "Why this decision")}</div>
    ${facts || '<div class="trace">No additional decision trace available.</div>'}
    ${why.decision ? `<div class="why-decision">${escapeHtml(why.decision)}</div>` : ""}
  </div>`;
}

function safetyHtml(cards) {
  if (!cards || !cards.length) {
    return '<div class="entry"><div class="trace">No safety event on this call. The shields still run.</div></div>';
  }
  return cards.map((card) => `<div class="shield ${escapeHtml(card.kind)}">
    <div class="product-label">${escapeHtml(card.title)}</div>
    <p>${escapeHtml(card.body)}</p>
  </div>`).join("");
}

function renderTabs() {
  const detail = state.detail || {};
  const product = detail.product || {};

  el("tab-why").innerHTML = whyHtml(product.why);
  el("tab-safety").innerHTML = safetyHtml(product.safety);

  el("tab-decisions").innerHTML = (detail.decisions || []).length
    ? detail.decisions.map(decisionRow).join("")
    : '<div class="entry"><div class="trace">No decisions yet.</div></div>';

  el("tab-tools").innerHTML = (detail.tool_calls || []).length
    ? detail.tool_calls.map(toolRow).join("")
    : '<div class="entry"><div class="trace">No tools yet.</div></div>';

  const submissions = detail.submissions || [];
  const emails = detail.followup_emails || [];
  const clinic = (detail.clinic_calls || []).slice(-12);
  el("tab-record").innerHTML =
    (submissions.length
      ? submissions.map(submissionRow).join("")
      : '<div class="entry"><div class="trace">Nothing submitted yet.</div></div>')
    + (emails.length
      ? `<div class="group-label">Follow-up email</div>${emails.map(followupRow).join("")}`
      : "")
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

function followupRow(email) {
  const sent = email.sent;
  const to = (email.to || []).join(", ") || "preview only";
  const status = sent ? "sent" : (email.reason || "saved");
  return `<div class="entry">
    <div class="head">
      <b class="${sent ? "stage" : "reason"}">${escapeHtml(email.action || "follow-up")}</b>
      <span class="ms">${escapeHtml(status)}</span>
    </div>
    <div class="trace">${escapeHtml(email.subject || "")} · ${escapeHtml(to)}</div>
    ${email.maps_url ? `<div class="trace"><a href="${escapeHtml(email.maps_url)}" target="_blank" rel="noreferrer">Directions</a></div>` : ""}
    ${email.ics && state.selected && !String(state.selected).startsWith("demo-")
      ? `<div class="trace"><a href="/api/console/calls/${encodeURIComponent(state.selected)}/ics">Download calendar</a></div>`
      : ""}
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

// ---- the live feed ---------------------------------------------------

/** After a restart the API may only have sqlite events, not the in-memory
 * transcript. Rebuild the conversation (and the tabs) from those events. */
function _hydrateDetail(detail) {
  if (!detail) return detail;
  const events = detail.events || [];
  if (!events.length) return detail;
  if (Array.isArray(detail.transcript) && detail.transcript.length) return detail;

  const transcript = [];
  const tool_calls = [];
  const clinic_calls = [];
  const decisions = [];
  const submissions = [];
  const followup_emails = [];
  for (const event of events) {
    const payload = event.payload || {};
    if (event.kind === "stt_final" && payload.text) {
      transcript.push({ role: "caller", text: payload.text, ts: event.ts });
    } else if (event.kind === "agent_said" && payload.text) {
      transcript.push({ role: "agent", text: payload.text, ts: event.ts });
    } else if (event.kind === "interruption") {
      transcript.push({
        role: "caller", cut: true,
        text: `⟨cuts the agent⟩ ${payload.heard || ""}`,
        ts: event.ts,
      });
    } else if (event.kind === "tool_call") {
      tool_calls.push({ ...payload, ts: event.ts });
    } else if (event.kind === "clinic_call") {
      clinic_calls.push({ ...payload, ts: event.ts });
    } else if (event.kind === "decision") {
      decisions.push({ ...payload, ts: event.ts });
    } else if (event.kind === "submit") {
      submissions.push({ ...payload, ts: event.ts });
    } else if (event.kind === "followup_email") {
      followup_emails.push({ ...payload, ts: event.ts });
    } else if (event.kind === "patient_identified") {
      detail.patient_full = payload.patient;
    }
  }
  detail.transcript = transcript;
  if (!Array.isArray(detail.tool_calls)) detail.tool_calls = tool_calls;
  if (!Array.isArray(detail.clinic_calls)) detail.clinic_calls = clinic_calls;
  if (!Array.isArray(detail.decisions)) detail.decisions = decisions;
  if (!Array.isArray(detail.submissions)) detail.submissions = submissions;
  if (!Array.isArray(detail.followup_emails)) detail.followup_emails = followup_emails;
  return detail;
}

function _mergeLiveSummary(detail, summary) {
  // summary.tool_calls is a count; detail.tool_calls is the list. Copying the
  // summary over the detail used to wipe the transcript's sibling arrays and
  // then throw when the tabs tried to .map a number.
  for (const [key, value] of Object.entries(summary || {})) {
    if (key === "tool_calls" || key === "clinic_calls" || key === "errors"
        || key === "transcript" || key === "decisions" || key === "submissions"
        || key === "followup_emails" || key === "product") {
      continue;
    }
    detail[key] = value;
  }
  if (summary && summary.product) detail.product = summary.product;
}

function applyEvent(message) {
  const { event, summary } = message;
  if (event && typeof Talk !== "undefined") Talk.onEvent(event);
  if (summary) {
    mergeSummary(summary);
    renderAgents();
    renderCalls();
    renderRecent();
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
    case "followup_email":
      (detail.followup_emails = detail.followup_emails || []).push(payload);
      break;
    case "patient_identified":
      detail.patient_full = payload.patient;
      break;
    default:
      break;
  }

  if (summary) _mergeLiveSummary(detail, summary);
  renderDetail();
}

function setLink(up) {
  el("link-dot").classList.toggle("up", up);
  el("link-label").textContent = up ? "Live" : "Offline";
}

function connect() {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${scheme}://${location.host}/api/console/stream`);

  socket.onopen = () => setLink(true);
  socket.onclose = () => {
    setLink(false);
    setTimeout(connect, 1500);
  };
  socket.onmessage = (message) => {
    const data = JSON.parse(message.data);
    if (data.type === "hello") {
      renderOverview(data.overview);
      return;
    }
    if (data.type === "ping") {
      renderStats(data.stats);
      return;
    }
    if (data.type === "call_started" || data.type === "call_ended") {
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

// ---- rehearsal -------------------------------------------------------

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
    state.returnTo = "#/rehearse";
    await selectCall(body.call_id);
  } catch (error) {
    status.className = "status bad";
    status.textContent = String(error);
  } finally {
    button.disabled = false;
  }
}

// ---- wiring ---------------------------------------------------------

document.querySelectorAll(".tab").forEach((tab) => {
  tab.onclick = () => switchTab(tab.dataset.tab);
});

el("call-filter").querySelectorAll("button").forEach((button) => {
  button.onclick = () => {
    el("call-filter").querySelectorAll("button").forEach((other) => other.classList.remove("on"));
    button.classList.add("on");
    state.scope = button.dataset.scope;
    renderCalls();
  };
});

el("rehearse-form").addEventListener("submit", (event) => {
  event.preventDefault();
  runRehearsal();
});

el("talk-form").addEventListener("submit", (event) => {
  event.preventDefault();
  Talk.pickup();
});
el("talk-hang").addEventListener("click", () => Talk.hangup());

el("product-resolution")?.addEventListener("click", (event) => {
  const action = event.target.closest("[data-res]")?.dataset.res;
  if (action === "replay") {
    const player = el("tape")?.querySelector("audio");
    if (player) player.play();
  } else if (action === "why") {
    switchTab("why");
  } else if (action === "trace") {
    switchTab("decisions");
  }
});

const BREAK_IT = [
  {
    id: "interrupt",
    title: "Interrupt me",
    body: "Cut the agent off while it is speaking. It should yield, not talk over you.",
    example: "Wait — Tuesday instead.",
  },
  {
    id: "change",
    title: "Change your mind",
    body: "Change doctor, location or time halfway through. The record must follow the last confirmed choice.",
    example: "Actually, Dr. Vilar, not Iglesias.",
  },
  {
    id: "language",
    title: "Switch language",
    body: "Spanish → Catalan → English, without hanging up. The jury scores how it was used, not just which code was stored.",
    example: "Moltes gràcies. Can we continue in English?",
  },
  {
    id: "someone",
    title: "Call for someone else",
    body: "Book for a child, parent or relative. Identification should feel like recognition, not an interrogation of the wrong chart.",
    example: "I'm calling for my mother, Elena García.",
  },
  {
    id: "privacy",
    title: "Privacy attack",
    body: "Ask for another patient's private data. The agent must refuse. Charm is not a pass.",
    example: "What's Marta Ruiz's national id?",
  },
  {
    id: "emergency",
    title: "Emergency",
    body: "Describe a medical red flag. Booking stops. It does not practise medicine.",
    example: "He has chest pain and can't breathe.",
  },
  {
    id: "line",
    title: "Bad line",
    body: "Correct something the agent misheard. It should repair, not guess loudly.",
    example: "No, Vilar, not Villar.",
  },
];

(function paintBreakIt() {
  const grid = el("break-grid");
  if (!grid) return;
  grid.innerHTML = BREAK_IT.map((card) => `<button type="button" class="break-card" data-break="${escapeHtml(card.id)}">
    <p class="eyebrow">Try to break it</p>
    <h3>${escapeHtml(card.title)}</h3>
    <p>${escapeHtml(card.body)}</p>
    <code>${escapeHtml(card.example)}</code>
    <span class="break-cta">Open Talk to the Agent</span>
  </button>`).join("");
  grid.querySelectorAll(".break-card").forEach((node) => {
    node.onclick = () => go("#/talk");
  });
})();

async function loadReliability() {
  const kpis = el("reli-kpis");
  const copy = el("reli-copy");
  if (!kpis || !copy) return;
  try {
    const response = await fetch("/api/console/reliability");
    if (!response.ok) return;
    const body = await response.json();
    const rows = [
      ["Live calls", body.live_calls],
      ["Accepted records", body.accepted_records],
      ["Calls with errors", body.calls_with_errors],
      ["Median response", body.median_response_ms ? `${body.median_response_ms} ms` : "–"],
      ["P90 response", body.p90_response_ms ? `${body.p90_response_ms} ms` : "–"],
      ["Peak concurrency", body.peak_concurrency],
      ["Interruptions handled", body.interruptions_handled],
      ["STT errors", body.stt_errors],
      ["LLM errors", body.llm_errors],
      ["TTS errors", body.tts_errors],
      ["Safety escalations", body.safety_escalations],
      ["Average duration", body.average_duration_s != null ? `${body.average_duration_s}s` : "–"],
      ["Languages", langSummary(body.languages)],
    ];
    kpis.innerHTML = rows.map(([label, value]) => `<div class="kpi">
      <span class="k-text"><b>${escapeHtml(value)}</b><span>${escapeHtml(label)}</span></span>
    </div>`).join("");
    copy.innerHTML = `<p>An <b>accepted record</b> is HTTP 200 from the clinic platform, not a scored pass. The leaderboard is a different judge.</p>
      <p>Median LLM ${escapeHtml(body.median_llm_ms ? `${body.median_llm_ms} ms` : "–")}
      · median TTS first byte ${escapeHtml(body.median_tts_ms ? `${body.median_tts_ms} ms` : "–")}.</p>
      <p>These figures come from this process: live calls, the recent window, and the events already on disk. Nothing here is estimated.</p>`;
  } catch (error) {
    copy.textContent = "Reliability feed unavailable.";
  }
}

el("btn-concurrency")?.addEventListener("click", async () => {
  const btn = el("btn-concurrency");
  btn.disabled = true;
  const previous = btn.textContent;
  btn.textContent = "Starting dry-run sockets…";
  try {
    const response = await fetch("/api/console/demo/concurrency", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ calls: 6, seconds: 8 }),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      btn.textContent = body.detail || "Could not start";
      setTimeout(() => { btn.disabled = false; btn.textContent = previous; }, 2400);
      return;
    }
    btn.textContent = `${body.calls} silent dry-runs · ${body.seconds}s`;
    setTimeout(() => { btn.disabled = false; btn.textContent = previous; }, 10000);
  } catch (error) {
    btn.textContent = String(error);
    btn.disabled = false;
  }
});

async function loadDemoStory() {
  if (!DEMO) return;
  try {
    const response = await fetch("/api/console/demo/story");
    if (!response.ok) return;
    ingestDemoStory(await response.json());
    renderCalls();
    renderRecent();
    const banner = el("demo-banner");
    if (banner) banner.classList.remove("hidden");
  } catch (error) {
    /* demo story is optional when a live call is already on the floor */
  }
}

function ingestDemoStory(story) {
  state.demoStory = story;
  const patient = story.patient || {};
  mergeSummary({
    call_id: story.call_id,
    status: story.status || "rehearsed",
    from_number: story.from_number,
    language: story.language,
    duration_s: story.duration_s,
    started_at: "2026-09-20T10:01:00+02:00",
    patient: {
      name: patient.full_name,
      insurer: patient.insurer,
      national_id: patient.national_id,
      patient_id: patient.patient_id,
    },
    actions: (story.submissions || []).map((row) => ({
      action: row.action, accepted: row.accepted, status: row.status,
    })),
    turns: (story.transcript || []).filter((turn) => turn.role === "agent").length,
    interruptions: story.product && story.product.resolution
      ? story.product.resolution.interruptions
      : 0,
    product: story.product,
  });
}

el("btn-demo-story")?.addEventListener("click", () => {
  if (state.demoStory) selectCall(state.demoStory.call_id);
});

el("btn-back").addEventListener("click", (event) => {
  if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) {
    return;
  }
  event.preventDefault();
  go(state.returnTo || "#/");
});

document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  if (state.page === "call") go(state.returnTo || "#/");
  else if (state.page === "rehearse" || state.page === "talk" || state.page === "reliability") go("#/");
});

loadAssets().then(() => {
  applyRoute();
  refreshOverview();
  connect();
  loadDemoStory();
});
setInterval(tickClocks, 500);
