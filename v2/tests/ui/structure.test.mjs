import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';

const html = await readFile(new URL('../../console.html', import.meta.url), 'utf8');
const app = await readFile(new URL('../../web/app.mjs', import.meta.url), 'utf8');
const core = await readFile(new URL('../../web/core.mjs', import.meta.url), 'utf8');
const voice = await readFile(new URL('../../web/voice.mjs', import.meta.url), 'utf8');
const css = await readFile(new URL('../../web/ops.css', import.meta.url), 'utf8');
const worklet = await readFile(new URL('../../web/capture-worklet.js', import.meta.url), 'utf8');

test('every browser script passes the Node syntax parser without executing browser or network code', () => {
  for (const file of ['app.mjs', 'core.mjs', 'voice.mjs', 'capture-worklet.js']) {
    const result = spawnSync(process.execPath, ['--check', fileURLToPath(new URL(`../../web/${file}`, import.meta.url))], {encoding: 'utf8'});
    assert.equal(result.status, 0, `${file}: ${result.stderr || result.error || ''}`);
  }
});

test('every static JS element reference exists exactly once in the HTML', () => {
  const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map(match => match[1]);
  assert.equal(new Set(ids).size, ids.length);
  for (const match of app.matchAll(/\bel\('([^']+)'\)/g)) assert.ok(ids.includes(match[1]), `missing element ${match[1]}`);
});

test('operator UI has no HTML injection, browser credential persistence or console logging', () => {
  for (const source of [app, core, voice, worklet]) {
    assert.doesNotMatch(source, /\.innerHTML\s*=|\.outerHTML\s*=|insertAdjacentHTML|document\.write\(/);
    assert.doesNotMatch(source, /localStorage|sessionStorage|document\.cookie|indexedDB/);
    assert.doesNotMatch(source, /console\.(?:log|debug|info|warn|error)\(/);
    assert.doesNotMatch(source, /[?&](?:token|ticket)=/);
  }
  assert.match(core, /'X-V2-Token': this\.#token/);
  assert.match(voice, /\['v2-voice', `ticket\.\$\{ticket\.ticket\}`\]/);
});

test('HTML loads only v2-owned local code/assets and offers responsive accessible views', () => {
  assert.match(html, /src="\/web\/app\.mjs"/); assert.match(html, /href="\/web\/ops\.css"/);
  assert.doesNotMatch(html, /src="(?:https?:|\/static\/)|href="https?:/);
  assert.match(html, /autocomplete="off"/); assert.match(html, /type="password"/);
  assert.match(html, /name="viewport"/); assert.match(html, /role="status"/);
  assert.match(html, /role="tablist"/); assert.match(html, /aria-controls="evidence-actions"/);
  assert.match(css, /@media \(max-width: 760px\)/); assert.match(css, /:focus-visible/);
  assert.match(css, /\[hidden\] \{ display: none !important; \}/);
});

test('both natural interaction modes are explicitly paid and live mode cannot be selected', () => {
  assert.match(html, /Start text · paid/); assert.match(html, /Start microphone · paid/);
  assert.match(html, /id="paid-consent"/); assert.match(html, /Run free fixture/);
  assert.match(html, /value="es"/); assert.match(html, /value="en"/); assert.match(html, /value="ca"/);
  assert.doesNotMatch(html, /<option value="live"/);
  assert.match(html, /Official score/); assert.match(html, /Unknown/);
  assert.match(html, /Reconciled actual/); assert.match(html, /Unreconciled reserved/);
});

test('recordings are blob-backed and are not rebuilt by the selected report polling renderer', () => {
  assert.match(core, /\/audio\/\$\{track\}/); assert.match(core, /createObjectURL\(blob\)/); assert.match(core, /revokeObjectURL/);
  const render = app.slice(app.indexOf('function renderDetail('), app.indexOf('async function startText('));
  assert.doesNotMatch(render, /buildRecordings|releaseRecordings|audio\.src|recordings\.clear/);
  assert.doesNotMatch(html, /<audio[^>]+src="\/api/);
});

test('capture worklet transfers real input and explicitly mutes its graph output', () => {
  assert.match(worklet, /registerProcessor\('v2-microphone'/);
  assert.match(worklet, /postMessage\(this\.batch, \[this\.batch\.buffer\]\)/);
  assert.match(worklet, /channel\.fill\(0\)/);
  assert.match(voice, /audioWorklet\.addModule\('\/web\/capture-worklet\.js'\)/);
  assert.match(voice, /track\.stop\(\)/); assert.match(voice, /context\.close\(\)/);
});
