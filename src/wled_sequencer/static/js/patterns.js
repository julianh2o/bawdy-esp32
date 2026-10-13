/**
 * Pattern editor (/patterns): edit Lua patterns, preview them in the browser, run them on channels.
 *
 * Talks to lightd through the web server's /engine/* proxy. The preview runs lightd's own
 * runtime.lua in wasmoon (Lua 5.4 in WASM), so it renders exactly what the LEDs will show.
 */

import { LuaFactory } from "https://cdn.jsdelivr.net/npm/wasmoon@1.16.0/+esm";

const API = "/engine/api";
const WASM_URL = "https://cdn.jsdelivr.net/npm/wasmoon@1.16.0/dist/glue.wasm";
const POLL_MS = 2000;
const PREVIEW_FPS = 30;
const STORE_KEY = "lightd.patterns";

const TEMPLATE = `-- pixel(n, t, p) runs for every pixel, every frame.
--   n: pixel number, 0 .. COUNT - 1      t: seconds since the run started
--   p: params (declared below)           return r, g, b in 0 .. 1
-- Helpers: hsv(h, s, v), mix(a, b, k), clamp(x, lo, hi), noise(x, y), hash(n, seed), color("#rrggbb")

params = {
  color = { type = "color", default = "#00c8ff" },
  speed = { type = "number", default = 1, min = 0, max = 10 },
}

function pixel(n, t, p)
  local k = (math.sin(n / COUNT * 6.283 * 3 - t * p.speed * 4) + 1) / 2
  return p.color.r * k, p.color.g * k, p.color.b * k
end
`;

const S = {
  library: [],
  current: { name: "", builtin: false, saved: null }, // saved: source as stored in the library (null = unsaved)
  controllers: [],
  runs: [],
  selected: new Set(),
  online: false,
  preview: { compile: null, render: null, ok: false, playing: true, t0: performance.now(), pausedAt: 0, last: 0, error: null },
};
let editor = null;
let errorMark = null;

// ---------- helpers ----------

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "dataset") Object.assign(node.dataset, v);
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (k in node && typeof v !== "string") node[k] = v;
    else node.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false || c === "") continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

const $ = (id) => document.getElementById(id);

async function api(method, path, body) {
  const resp = await fetch(API + path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await resp.text();
  let data = {};
  try { data = text ? JSON.parse(text) : {}; } catch { data = { message: text }; }
  if (!resp.ok) {
    const err = new Error(data.message || `${resp.status} ${resp.statusText}`);
    err.line = data.line;
    throw err;
  }
  return data;
}

function banner(message, kind = "") {
  const b = $("banner");
  if (!message) { b.hidden = true; return; }
  b.hidden = false;
  b.className = `banner ${kind}`;
  b.textContent = message;
}

function loadPrefs() {
  try { return JSON.parse(localStorage.getItem(STORE_KEY) || "{}"); } catch { return {}; }
}

function savePrefs() {
  try {
    localStorage.setItem(STORE_KEY, JSON.stringify({ pattern: S.current.name, selected: [...S.selected] }));
  } catch { /* storage unavailable */ }
}

const source = () => editor.getValue();
const untouched = () => S.current.saved === null && (source() === TEMPLATE || source().trim() === "");
const isDirty = () => !untouched() && (S.current.saved === null || source() !== S.current.saved);

// ---------- errors in the editor ----------

function showError(message, line) {
  if (errorMark !== null) { editor.removeLineClass(errorMark, "background", "error-line"); errorMark = null; }
  const bar = $("error-bar");
  if (!message) { bar.hidden = true; return; }
  bar.hidden = false;
  bar.textContent = message;
  const m = line ?? /^pattern:(\d+):/.exec(message)?.[1];
  if (m) {
    errorMark = editor.addLineClass(Number(m) - 1, "background", "error-line");
  }
}

// ---------- preview (wasmoon) ----------

async function initPreview() {
  const factory = new LuaFactory(WASM_URL);
  const lua = await factory.createEngine();
  const runtime = await (await fetch(`${API}/runtime.lua`)).text();
  lua.global.set("RUNTIME_SRC", runtime);
  await lua.doString(`
    RT = assert(load(RUNTIME_SRC, "=runtime"))()
    local env, p
    function preview_compile(src)
      local e, err = RT.compile(src)
      if not e then env = nil; return tostring(err) end
      env, p = e, RT.params(e, nil)
      return ""
    end
    function preview_render(count, t, reversed)
      if not env then return "" end
      local ok, data = RT.render_hex(env, count, t, p, reversed)
      if not ok then return "!" .. tostring(data) end
      return data
    end
  `);
  S.preview.compile = lua.global.get("preview_compile");
  S.preview.render = lua.global.get("preview_render");
}

function compilePreview() {
  if (!S.preview.compile) return;
  const err = S.preview.compile(source());
  S.preview.ok = !err;
  S.preview.error = err || null;
  showError(err || null);
  updateButtons();
}

function previewTime(now = performance.now()) {
  return ((S.preview.playing ? now : S.preview.pausedAt) - S.preview.t0) / 1000;
}

function drawPreview(now) {
  requestAnimationFrame(drawPreview);
  if (now - S.preview.last < 1000 / PREVIEW_FPS) return;
  S.preview.last = now;
  const canvas = $("preview-canvas");
  const dpr = window.devicePixelRatio || 1;
  const w = Math.round(canvas.clientWidth * dpr);
  const h = Math.round(canvas.clientHeight * dpr);
  if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = "#07080a";
  ctx.fillRect(0, 0, w, h);
  if (!S.preview.render || !S.preview.ok) return;

  const count = Math.max(1, Math.min(2000, parseInt($("preview-count").value, 10) || 150));
  const t = previewTime(now);
  const t0 = performance.now();
  const data = S.preview.render(count, t, $("preview-reversed").checked);
  const ms = performance.now() - t0;
  if (data.startsWith("!")) {
    if (S.preview.error !== data.slice(1)) { S.preview.error = data.slice(1); showError(S.preview.error); }
    return;
  }
  if (S.preview.error) { S.preview.error = null; showError(null); }

  const cell = w / count;
  const gap = cell >= 4 ? Math.max(1, cell * 0.15) : 0;
  const height = Math.round(h * 0.6);
  for (let i = 0; i < count; i++) {
    ctx.fillStyle = `#${data.substr(i * 6, 6)}`;
    ctx.fillRect(i * cell + gap / 2, (h - height) / 2, Math.max(1, cell - gap), height);
  }
  $("preview-time").textContent = `t = ${t.toFixed(1)} s`;
  $("preview-perf").textContent = `${count} px · ${ms.toFixed(1)} ms/frame`;
}

// ---------- library ----------

async function loadLibrary() {
  try {
    S.library = (await api("GET", "/patterns")).patterns;
  } catch (e) {
    banner(`Could not load patterns: ${e.message}`, "err");
    S.library = [];
  }
  renderLibrary();
}

function renderLibrary() {
  const mine = S.library.filter((p) => !p.builtin);
  const builtin = S.library.filter((p) => p.builtin);
  const item = (p) =>
    el("button", {
      class: `pe-item${p.name === S.current.name && S.current.saved !== null ? " current" : ""}`,
      onclick: () => openPattern(p.name),
      title: p.error ? p.error.message : "",
    }, p.name, p.error ? el("span", { class: "tag err" }, "error") : null);
  $("library-list").replaceChildren(
    el("div", { class: "pe-group" }, "Mine"),
    ...(mine.length ? mine.map(item) : [el("div", { class: "muted small" }, "Nothing saved yet.")]),
    el("div", { class: "pe-group" }, "Built-in"),
    ...builtin.map(item),
  );
}

function confirmDiscard() {
  return !isDirty() || confirm("Discard unsaved changes?");
}

async function openPattern(name, { force = false } = {}) {
  if (!force && !confirmDiscard()) return;
  try {
    const p = await api("GET", `/patterns/${encodeURIComponent(name)}`);
    setCurrent(p.name, p.builtin, p.source);
  } catch (e) {
    banner(`Could not open ${name}: ${e.message}`, "err");
  }
}

function setCurrent(name, builtin, src) {
  S.current = { name, builtin, saved: src };
  $("name-input").value = name;
  editor.setValue(src);
  editor.clearHistory();
  restartPreview();
  renderLibrary();
  updateButtons();
  savePrefs();
}

function newPattern() {
  if (!confirmDiscard()) return;
  S.current = { name: "", builtin: false, saved: null };
  $("name-input").value = "";
  editor.setValue(TEMPLATE);
  restartPreview();
  renderLibrary();
  updateButtons();
  $("name-input").focus();
}

async function savePattern() {
  const name = $("name-input").value.trim();
  if (!name) { banner("Give the pattern a name first.", "err"); $("name-input").focus(); return; }
  const builtin = S.library.find((p) => p.name === name && p.builtin);
  if (builtin) { banner(`'${name}' is a built-in pattern. Save your version under another name.`, "err"); $("name-input").focus(); return; }
  try {
    await api("PUT", `/patterns/${encodeURIComponent(name)}`, { source: source() });
    S.current = { name, builtin: false, saved: source() };
    banner(`Saved ${name}.`, "ok");
    setTimeout(() => banner(null), 2500);
    await loadLibrary();
    updateButtons();
    savePrefs();
  } catch (e) {
    showError(e.message, e.line);
    banner(`Not saved: ${e.message}`, "err");
  }
}

async function deletePattern() {
  const { name, builtin, saved } = S.current;
  if (!name || builtin || saved === null) return;
  if (!confirm(`Delete ${name}? Runs already playing it keep going.`)) return;
  try {
    await api("DELETE", `/patterns/${encodeURIComponent(name)}`);
    S.current = { name: "", builtin: false, saved: null };
    $("name-input").value = "";
    await loadLibrary();
    updateButtons();
  } catch (e) {
    banner(`Could not delete: ${e.message}`, "err");
  }
}

function updateButtons() {
  const { name, builtin, saved } = S.current;
  const typed = $("name-input").value.trim();
  $("source-tag").textContent = builtin && typed === name ? "built-in" : saved === null ? "new" : "";
  $("dirty-indicator").textContent = isDirty() ? "Unsaved changes" : "";
  $("save-btn").textContent = builtin && typed === name ? "Save as…" : "Save";
  $("delete-btn").hidden = builtin || saved === null;
  $("run-btn").disabled = !S.selected.size || !S.preview.ok && S.preview.compile !== null || !S.online;
  $("run-btn").textContent = S.selected.size > 1 ? `Run on ${S.selected.size} channels` : "Run on selected";
}

// ---------- controllers & runs ----------

async function loadControllers() {
  try {
    S.controllers = (await api("GET", "/controllers")).controllers;
    S.online = true;
  } catch (e) {
    S.online = false;
  }
  const known = new Set(S.controllers.flatMap((c) => c.channels.map((ch) => ch.target)));
  for (const t of [...S.selected]) if (!known.has(t)) S.selected.delete(t);
  renderTargets();
}

function runOn(target) {
  return S.runs.find((r) => r.targets.includes(target));
}

function renderTargets() {
  const list = $("targets-list");
  if (!S.online) {
    list.replaceChildren(el("div", { class: "empty" }, "lightd isn't reachable."));
    updateButtons();
    return;
  }
  if (!S.controllers.length) {
    list.replaceChildren(el("div", { class: "empty" }, "No controllers. Adopt boards on the ", el("a", { href: "/config" }, "setup page"), "."));
    updateButtons();
    return;
  }
  list.replaceChildren(...S.controllers.map((c) =>
    el("div", { class: "card pe-ctrl" },
      el("div", { class: "pe-ctrl-head" },
        el("strong", {}, c.id),
        c.name ? el("span", { class: "muted small" }, c.name) : null,
        el("span", { class: "spacer" }),
        el("span", { class: `tag ${c.online ? "ok" : "warn"}`, title: c.error || "" }, c.online ? "online" : "offline")),
      c.channels.length
        ? c.channels.map((ch) => {
            const usable = ch.on_board;
            const run = runOn(ch.target);
            return el("label", { class: `pe-chan${usable ? "" : " disabled"}`, title: usable ? ch.target : "This channel isn't on the board" },
              el("input", {
                type: "checkbox",
                checked: S.selected.has(ch.target),
                disabled: !usable,
                onchange: (e) => {
                  e.target.checked ? S.selected.add(ch.target) : S.selected.delete(ch.target);
                  matchPreviewToSelection();
                  savePrefs();
                  updateButtons();
                },
              }),
              el("span", {}, ch.name || `Ch ${ch.number}`),
              run ? el("span", { class: "busy", title: "Playing now" }, `▶ ${run.pattern || "editor"}`) : null,
              el("span", { class: "meta" }, usable ? `${ch.length} px${ch.reversed ? " · rev" : ""}` : "not on board"));
          })
        : el("div", { class: "muted small" }, c.online ? "No LED outputs reported." : "Channels appear once the board is reachable."))));
  updateButtons();
}

function matchPreviewToSelection() {
  const first = [...S.selected][0];
  if (!first) return;
  for (const c of S.controllers) {
    const ch = c.channels.find((x) => x.target === first);
    if (ch?.length) {
      $("preview-count").value = ch.length;
      $("preview-reversed").checked = !!ch.reversed;
    }
  }
}

async function loadRuns() {
  try {
    S.runs = (await api("GET", "/runs")).runs;
  } catch {
    S.runs = [];
  }
  renderRuns();
}

function renderRuns() {
  const list = $("runs-list");
  if (!S.runs.length) {
    list.replaceChildren(el("div", { class: "muted small" }, "Nothing running. Idle channels are black."));
    return;
  }
  list.replaceChildren(...S.runs.map((r) =>
    el("div", { class: "card pe-run" },
      el("div", { class: "pe-run-top" },
        el("strong", {}, r.pattern || "editor code"),
        r.error ? el("span", { class: "tag err" }, "error") : null,
        el("span", { class: "spacer" }),
        el("span", { class: "muted small" }, `${Math.floor(r.elapsed)} s`),
        el("button", { class: "btn btn-small", onclick: () => stopRun(r.id) }, "Stop")),
      el("div", { class: "targets" }, r.targets.map(targetLabel).join(", ")),
      r.error ? el("div", { class: "err" }, r.error.message) : null)));
}

function targetLabel(target) {
  for (const c of S.controllers) {
    const ch = c.channels.find((x) => x.target === target);
    if (ch?.name) return `${target} (${ch.name})`;
  }
  return target;
}

async function runSelected() {
  const targets = [...S.selected];
  const { name, saved } = S.current;
  const body = { targets, params: {} };
  if (name && saved !== null && !isDirty()) body.pattern = name;
  else body.source = source();
  try {
    await api("POST", "/runs", body);
    banner(null);
    showError(null);
    await loadRuns();
    renderTargets();
  } catch (e) {
    showError(e.message, e.line);
    banner(`Didn't start: ${e.message}`, "err");
  }
}

async function stopRun(id) {
  try {
    await api("DELETE", `/runs/${id}`);
  } catch (e) {
    banner(`Could not stop: ${e.message}`, "err");
  }
  await loadRuns();
  renderTargets();
}

async function blackout() {
  try {
    await api("POST", "/blackout");
  } catch (e) {
    banner(`Blackout failed: ${e.message}`, "err");
  }
  await loadRuns();
  renderTargets();
}

async function pollStatus() {
  const pill = $("lightd-status");
  try {
    const s = await api("GET", "/status");
    S.online = true;
    pill.className = "pill ok";
    pill.textContent = `lightd · ${Math.round(s.actual_fps)} fps · ${s.controllers.online}/${s.controllers.total} online`;
  } catch {
    S.online = false;
    pill.className = "pill err";
    pill.textContent = "lightd offline";
  }
}

async function poll() {
  await pollStatus();
  await loadControllers();
  await loadRuns();
  renderTargets();
}

// ---------- preview controls ----------

function restartPreview() {
  S.preview.t0 = performance.now();
  S.preview.pausedAt = S.preview.t0;
  compilePreview();
}

function togglePlay() {
  const p = S.preview;
  const now = performance.now();
  if (p.playing) p.pausedAt = now;
  else p.t0 += now - p.pausedAt;
  p.playing = !p.playing;
  $("preview-play").textContent = p.playing ? "Pause" : "Play";
}

// ---------- boot ----------

async function bootstrap() {
  editor = CodeMirror($("editor"), {
    value: TEMPLATE,
    mode: "lua",
    lineNumbers: true,
    indentUnit: 2,
    tabSize: 2,
    matchBrackets: true,
    extraKeys: {
      "Ctrl-S": () => savePattern(),
      "Cmd-S": () => savePattern(),
      "Ctrl-Enter": () => runSelected(),
      "Cmd-Enter": () => runSelected(),
      Tab: (cm) => cm.replaceSelection("  "),
    },
  });
  let timer = null;
  editor.on("change", () => {
    updateButtons();
    clearTimeout(timer);
    timer = setTimeout(compilePreview, 250);
  });

  $("new-btn").addEventListener("click", newPattern);
  $("save-btn").addEventListener("click", savePattern);
  $("delete-btn").addEventListener("click", deletePattern);
  $("name-input").addEventListener("input", updateButtons);
  $("run-btn").addEventListener("click", runSelected);
  $("blackout-btn").addEventListener("click", blackout);
  $("refresh-btn").addEventListener("click", poll);
  $("preview-play").addEventListener("click", togglePlay);
  $("preview-restart").addEventListener("click", restartPreview);
  window.addEventListener("beforeunload", (e) => { if (isDirty()) e.preventDefault(); });

  const prefs = loadPrefs();
  for (const t of prefs.selected || []) S.selected.add(t);

  try {
    await initPreview();
  } catch (e) {
    banner(`Preview unavailable: ${e.message}`, "err");
  }
  requestAnimationFrame(drawPreview);

  await Promise.all([loadLibrary(), poll()]);
  const start = prefs.pattern && S.library.find((p) => p.name === prefs.pattern) ? prefs.pattern : null;
  if (start) await openPattern(start, { force: true });
  else compilePreview();
  matchPreviewToSelection();
  setInterval(poll, POLL_MS);
}

bootstrap();
