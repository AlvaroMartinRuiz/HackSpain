const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

function loadScript(name) {
  const nodes = new Map();
  const node = (id) => {
    if (!nodes.has(id)) nodes.set(id, {
      textContent: "", innerHTML: "", dataset: {},
      querySelectorAll: () => [], addEventListener() {},
      classList: { add() {}, remove() {}, toggle() {} },
    });
    return nodes.get(id);
  };
  const context = vm.createContext({
    document: { getElementById: node, querySelectorAll: () => [], addEventListener() {} },
    window: { addEventListener() {} },
    location: { protocol: "http:", host: "localhost", hash: "#/" },
    fetch: () => new Promise(() => {}),
    WebSocket: class {},
    setInterval() {}, setTimeout() {}, console,
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../src/web/static", name), "utf8"), context);
  return { node, run: (code) => vm.runInContext(code, context) };
}

test("operations KPIs separate submissions, silence, and unknown audio", () => {
  const { node, run } = loadScript("ops.js");
  run("renderStats({ missing_submission_calls: 2, silent_calls: 3, audio_unknown_calls: 4 })");
  const html = node("kpis").innerHTML;
  assert.match(html, /<b>2<\/b><span>Missing submissions/);
  assert.match(html, /<b>3<\/b><span>Silent audio/);
  assert.match(html, /<b>4<\/b><span>Audio unknown/);
});

test("old API silent_calls cannot masquerade as measured audio", () => {
  const { node, run } = loadScript("ops.js");
  run("renderStats({ silent_calls: 9 })");
  assert.match(node("kpis").innerHTML, /<b>–<\/b><span>Silent audio/);
  assert.match(node("kpis").innerHTML, /<b>–<\/b><span>Missing submissions/);
});

test("history filters do not conflate audio and records", () => {
  const { run } = loadScript("ops.js");
  assert.equal(run("SCOPES.missing({live:false, actions:[], audio_status:'signal'})"), true);
  assert.equal(run("SCOPES.silent({live:false, actions:[], audio_status:'signal'})"), false);
  assert.equal(run("SCOPES.silent({live:false, actions:[{}], audio_status:'silent'})"), true);
  assert.equal(run("SCOPES.audio_unknown({live:false, actions:[]})"), true);
  assert.equal(run("SCOPES.silent({live:true, audio_status:'silent'})"), false);
  assert.equal(run("SCOPES.audio_unknown({live:false, audio_status:'not_applicable'})"), false);
});

test("history rows expose audio independently of record badges", () => {
  const { run } = loadScript("ops.js");
  const html = run("callRow({call_id:'test', live:false, actions:[], audio_status:'signal'})");
  assert.match(html, /Signal sent/);
  assert.match(html, /no record/);
  assert.equal((html.match(/<td\b/g) || []).length, 9);
});

test("classic console has independent counters and handles old APIs", () => {
  const { node, run } = loadScript("console.js");
  run("renderStats({missing_submission_calls:2, silent_calls:3, audio_unknown_calls:4})");
  assert.equal(node("stat-missing").textContent, 2);
  assert.equal(node("stat-silent").textContent, 3);
  assert.equal(node("stat-audio-unknown").textContent, 4);
  run("renderStats({silent_calls:9})");
  assert.equal(node("stat-silent").textContent, "–");
  assert.equal(node("stat-missing").textContent, "–");
});

for (const name of ["ops.js", "console.js"]) {
  test(`${name}: failures include structured metadata and escape provider text`, () => {
    const { node, run } = loadScript(name);
    run(`state.detail = {errors: [{where: 'llm', detail: '<script>bad</script>',
      error_type: 'ReadTimeout', phase: 'stream', elapsed_ms: 30000, round: 2}]}; renderTabs()`);
    const html = node("tab-decisions").innerHTML;
    assert.match(html, /ReadTimeout/);
    assert.match(html, /stream/);
    assert.match(html, /30000/);
    assert.doesNotMatch(html, /<script>/);
  });

  test(`${name}: records cannot be mistaken for official scores or live dry runs`, () => {
    const { node, run } = loadScript(name);
    run("state.detail = {submissions:[{action:'book', status:200, accepted:true, dry_run:true}]}; renderTabs()");
    const html = node("tab-record").innerHTML;
    assert.match(html, name === "ops.js" ? /Official scored outcome: not loaded/ : /Resultado oficial: no cargado/);
    assert.match(html, name === "ops.js" ? /Local dry run/ : /Ensayo local/);
    assert.doesNotMatch(html, /HTTP 200/);
  });
}
