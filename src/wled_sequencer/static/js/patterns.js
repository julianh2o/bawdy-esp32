/**
 * Pattern editor (/, also /patterns): edit Lua patterns, preview them in the browser, run them on channels with params and
 * brightness, save what's running as a scene and activate scenes.
 *
 * Talks to lightd through the web server's /engine/* proxy. The preview runs lightd's own
 * runtime.lua in wasmoon (Lua 5.4 in WASM), so it renders exactly what the LEDs will show.
 */

import { LuaFactory } from "https://cdn.jsdelivr.net/npm/wasmoon@1.16.0/+esm";
import { initOutputSwitch, showOutput } from "./output_switch.js";

const API = "/engine/api";
const WASM_URL = "https://cdn.jsdelivr.net/npm/wasmoon@1.16.0/dist/glue.wasm";
const POLL_MS = 2000;
const PREVIEW_FPS = 30;
const API_VALUES_MS = 100; // how often the preview fetches values for type = "api" params
const STORE_KEY = "lightd.patterns";
// Floppy-disk "save" icon, drawn in the button's text color.
const SAVE_ICON = `<svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true" fill="none" stroke="currentColor"
  stroke-width="1.4" stroke-linejoin="round"><path d="M2.5 2.5h9l2 2v9h-11z"/><path d="M5 2.5v3.5h5.5V2.5"/>
  <rect x="4.5" y="9" width="7" height="4.5"/></svg>`;

const TEMPLATE = `-- pixel(n, t, p) runs for every pixel, every frame.
--   n: pixel number, 0 .. COUNT - 1      t: Unix time in seconds (the same on every channel)
--   p: params (declared below)           return r, g, b in 0 .. 1, and optionally w (white)
--   w lights white LEDs (RGBW strips, linked Govee white); on RGB-only LEDs it's added to r, g, b.
--   Leave w out and white LEDs get the white part of r, g, b automatically. WHITE: true on white-capable LEDs
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
  // builtin: a built-in of this name exists; modified: an edited copy replaces it. saved: source as stored (null = unsaved)
  current: { name: "", builtin: false, modified: false, saved: null },
  controllers: [],
  runs: [],
  selected: new Set(),
  scenes: [],
  online: false,
  schema: {}, // params declared by the editor's code (last version that compiled)
  params: {}, // values set in the Params panel; unset params use the schema default
  apiValues: {}, // the values API (/values), fed to type = "api" params in the preview
  brightness: 1, // applied to the selected channels on Run
  preview: { compile: null, render: null, clearParams: null, setParam: null, schema: null, ok: false, playing: true, pausedAt: 0, last: 0, error: null },
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

function icon(node, svg) {
  node.innerHTML = svg;
  return node;
}

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
    local values = {}
    function preview_compile(src)
      local e, err = RT.compile(src)
      if not e then env = nil; return tostring(err) end
      env, p = e, RT.params(e, values)
      return ""
    end
    -- Param values: cleared, then set one by one. (wasmoon can't take null arguments.)
    function preview_clear_params()
      values = {}
      if env then p = RT.params(env, values) end
    end
    function preview_param(k, v)
      values[k] = v
      if env then p = RT.params(env, values) end
    end
    local function json(v)
      local ty = type(v)
      if ty == "table" then
        local out = {}
        if #v > 0 then
          for i, x in ipairs(v) do out[i] = json(x) end
          return "[" .. table.concat(out, ",") .. "]"
        end
        for k, x in pairs(v) do out[#out + 1] = json(tostring(k)) .. ":" .. json(x) end
        return "{" .. table.concat(out, ",") .. "}"
      elseif ty == "string" then
        return '"' .. v:gsub('[%c"\\\\]', function(c) return string.format("\\\\u%04x", c:byte()) end) .. '"'
      elseif ty == "number" or ty == "boolean" then
        return tostring(v)
      end
      return "null"
    end
    function preview_schema()
      if not env then return "" end
      return json(RT.schema(env))
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
  S.preview.clearParams = lua.global.get("preview_clear_params");
  S.preview.setParam = lua.global.get("preview_param");
  S.preview.schema = lua.global.get("preview_schema");
}

function compilePreview() {
  if (!S.preview.compile) return;
  const err = S.preview.compile(source());
  S.preview.ok = !err;
  S.preview.error = err || null;
  showError(err || null);
  if (!err) {
    try { S.schema = JSON.parse(S.preview.schema() || "{}"); } catch { /* keep the last schema */ }
    pushPreviewParams();
    renderParams();
  }
  updateButtons();
}

function pushPreviewParams() {
  if (!S.preview.setParam) return;
  S.preview.clearParams();
  for (const [k, v] of Object.entries(S.params)) if (v !== null && v !== undefined) S.preview.setParam(k, v);
  for (const name of apiParams()) {
    const spec = S.schema[name];
    const v = S.apiValues[spec.key || name] ?? spec.default;
    if (v !== null && v !== undefined) S.preview.setParam(name, v);
  }
}

// type = "api" params come from the values API, not the Params panel.
function apiParams() {
  return Object.keys(S.schema).filter((name) => S.schema[name].type === "api");
}

// While the pattern has api params, keep the preview fed with the server's current values.
async function pollApiValues() {
  if (apiParams().length) {
    try {
      S.apiValues = (await api("GET", "/values")).values;
      pushPreviewParams();
    } catch { /* keep the last values */ }
  }
  setTimeout(pollApiValues, API_VALUES_MS);
}

// Wall-clock time, like lightd, so the preview is in step with the LEDs.
function previewTime() {
  return S.preview.playing ? Date.now() / 1000 : S.preview.pausedAt;
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
  const t = previewTime();
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
  $("preview-time").textContent = `t = ${t.toFixed(1)} (${new Date(t * 1000).toLocaleTimeString()})`;
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
    }, p.name,
    p.modified ? el("span", { class: "tag", title: "Your edited copy is used instead of the built-in" }, "edited") : null,
    p.error ? el("span", { class: "tag err" }, "error") : null);
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

async function openPattern(name, { force = false, params = {} } = {}) {
  if (!force && !confirmDiscard()) return false;
  try {
    const p = await api("GET", `/patterns/${encodeURIComponent(name)}`);
    setCurrent(p, params);
    return true;
  } catch (e) {
    banner(`Could not open ${name}: ${e.message}`, "err");
    return false;
  }
}

function setCurrent({ name, builtin, modified, source: src }, params = {}) {
  S.current = { name, builtin, modified, saved: src };
  S.params = { ...params };
  S.schema = S.library.find((p) => p.name === name)?.params || S.schema; // until the preview compiles it
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
  S.current = { name: "", builtin: false, modified: false, saved: null };
  S.params = {};
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
  // Saving over a built-in stores an edited copy; ask first unless that built-in is the one open.
  const builtin = S.library.find((p) => p.name === name && p.builtin);
  if (builtin && name !== S.current.name &&
      !confirm(`Replace the built-in pattern '${name}' with this code? Revert brings the original back.`)) return;
  try {
    const res = await api("PUT", `/patterns/${encodeURIComponent(name)}`, { source: source() });
    S.current = { name, builtin: res.builtin, modified: res.modified, saved: source() };
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

// Delete a user pattern, or revert an edited built-in to the original.
async function deletePattern() {
  const { name, builtin, modified, saved } = S.current;
  if (!name || saved === null || (builtin && !modified)) return;
  const question = modified
    ? `Revert ${name} to the built-in version? Your edits are lost; runs already playing them keep going.`
    : `Delete ${name}? Runs already playing it keep going.`;
  if (!confirm(question)) return;
  try {
    await api("DELETE", `/patterns/${encodeURIComponent(name)}`);
    await loadLibrary();
    if (modified) {
      await openPattern(name, { force: true, params: S.params });
    } else {
      S.current = { name: "", builtin: false, modified: false, saved: null };
      $("name-input").value = "";
      updateButtons();
    }
  } catch (e) {
    banner(`Could not delete: ${e.message}`, "err");
  }
}

function updateButtons() {
  const { name, builtin, modified, saved } = S.current;
  const typed = $("name-input").value.trim();
  $("source-tag").textContent = saved === null ? "new"
    : typed !== name ? "" : modified ? "edited built-in" : builtin ? "built-in" : "";
  $("dirty-indicator").textContent = isDirty() ? "Unsaved changes" : "";
  $("delete-btn").hidden = saved === null || (builtin && !modified);
  $("delete-btn").textContent = modified ? "Revert" : "Delete";
  $("delete-btn").title = modified ? "Go back to the original built-in version" : "";
  $("run-btn").disabled = !S.selected.size || !S.preview.ok && S.preview.compile !== null || !S.online;
  $("run-btn").textContent = S.selected.size > 1 ? `Run on ${S.selected.size} channels` : "Run on selected";
  $("run-hint").textContent = isDirty() || S.current.saved === null
    ? "Unsaved code runs as-is, but only saved patterns can go in a scene." : "";
  $("save-scene-btn").disabled = !S.runs.some((r) => r.pattern) || !S.online;
  $("stop-all-btn").disabled = !S.runs.length || !S.online;
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
              el("span", {}, ch.name || ch.segment_name || `Ch ${ch.number}`),
              run ? el("span", { class: "busy", title: "Playing now" }, `▶ ${run.pattern || "editor"}`) : null,
              el("span", { class: "meta" }, usable
                ? `${ch.length} px${ch.linked ? " + white" : ch.rgbw ? " · RGBW" : ""}${ch.reversed ? " · rev" : ""}${ch.brightness < 1 ? ` · ${Math.round(ch.brightness * 100)}%` : ""}`
                : "not on board"));
          })
        : el("div", { class: "muted small" }, c.online ? "No LED outputs reported." : "Channels appear once the board is reachable."))));
  updateButtons();
}

function matchPreviewToSelection({ brightness = true } = {}) {
  const first = [...S.selected][0];
  if (!first) return;
  for (const c of S.controllers) {
    const ch = c.channels.find((x) => x.target === first);
    if (ch?.length) {
      $("preview-count").value = ch.length;
      $("preview-reversed").checked = !!ch.reversed;
      if (brightness) { S.brightness = ch.brightness ?? 1; renderParams(); }
    }
  }
}

// ---------- params panel ----------

function renderParams() {
  const names = Object.keys(S.schema).filter((name) => S.schema[name].type !== "api").sort();
  const rows = names.flatMap((name) => {
    const spec = S.schema[name];
    const isSet = Object.hasOwn(S.params, name);
    const value = isSet ? S.params[name] : spec.default;
    const label = el("span", { class: `pe-param-name${isSet ? " set" : ""}`, title: name }, spec.label || name);
    const reset = el("button", {
      class: `btn btn-small btn-ghost pe-reset${isSet ? " set" : ""}`,
      title: `Back to the default (${spec.default})`,
      "aria-label": `Reset ${spec.label || name}`,
      onclick: () => { delete S.params[name]; pushPreviewParams(); renderParams(); },
    }, "↺");
    const set = (v) => {
      S.params[name] = v;
      label.classList.add("set");
      reset.classList.add("set");
      pushPreviewParams();
    };
    return [label, paramInput(spec, value, set, spec.label || name), reset];
  });
  const pct = Math.round(S.brightness * 100);
  const num = el("span", { class: "num" }, `${pct}%`);
  rows.push(
    el("span", { class: "pe-param-name", title: "Applied to the selected channels" }, "Brightness"),
    el("span", { class: "pe-value" },
      el("input", {
        type: "range", min: 0, max: 100, value: pct, "aria-label": "Brightness",
        oninput: (e) => { S.brightness = e.target.value / 100; num.textContent = `${e.target.value}%`; },
      }),
      num),
    el("span"));
  if (!names.length) rows.unshift(el("span", { class: "muted small pe-noparams" }, "This pattern has no params."));
  $("params-form").replaceChildren(...rows);
}

function paramInput(spec, value, set, label) {
  if (spec.type === "number") {
    if (spec.min !== undefined && spec.max !== undefined) {
      // The slider covers min..max; the box takes any number, beyond the slider's range too.
      const outside = (v) => v < spec.min || v > spec.max;
      const slider = el("input", {
        type: "range", min: spec.min, max: spec.max, step: spec.step ?? "any", value, "aria-label": label,
        oninput: (e) => { box.value = fmt(Number(e.target.value)); box.classList.remove("out"); set(Number(e.target.value)); },
      });
      const box = el("input", {
        type: "number", class: `num-box${outside(value) ? " out" : ""}`, value: fmt(value), step: "any",
        "aria-label": `${label} (any number)`, title: `Slider range ${spec.min} to ${spec.max}; type any number`,
        oninput: (e) => {
          const v = Number(e.target.value);
          if (e.target.value === "" || !Number.isFinite(v)) return;
          slider.value = v;
          box.classList.toggle("out", outside(v));
          set(v);
        },
      });
      return el("span", { class: "pe-value" }, slider, box);
    }
    return el("input", { type: "number", value, step: spec.step ?? "any", "aria-label": label, oninput: (e) => set(Number(e.target.value)) });
  }
  if (spec.type === "color") {
    return el("span", { class: "pe-value" },
      el("input", { type: "color", value: value || "#000000", "aria-label": label, oninput: (e) => set(e.target.value) }));
  }
  if (spec.type === "boolean") {
    return el("span", { class: "pe-value" },
      el("input", { type: "checkbox", checked: !!value, "aria-label": label, onchange: (e) => set(e.target.checked) }));
  }
  if (spec.type === "choice" && Array.isArray(spec.options)) {
    return el("select", { "aria-label": label, onchange: (e) => set(e.target.value) },
      spec.options.map((o) => el("option", { value: o, selected: o === value }, o)));
  }
  return el("input", { type: "text", value: value ?? "", "aria-label": label, oninput: (e) => set(e.target.value) });
}

function fmt(v) {
  return Number.isInteger(v) ? String(v) : Number(v).toFixed(2).replace(/\.?0+$/, "");
}

function resetParams() {
  S.params = {};
  pushPreviewParams();
  renderParams();
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
        r.pattern ? el("button", {
          class: "btn btn-small btn-ghost", title: "Load this run's pattern, channels and params into the editor",
          onclick: () => editRun(r),
        }, "Edit") : null,
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

async function editRun(r) {
  if (!(await openPattern(r.pattern, { params: r.params }))) return;
  S.selected = new Set(r.targets);
  matchPreviewToSelection();
  savePrefs();
  renderTargets();
}

async function runSelected() {
  const targets = [...S.selected];
  const { name, saved } = S.current;
  const body = { targets, params: S.params };
  if (name && saved !== null && !isDirty()) body.pattern = name;
  else body.source = source();
  try {
    await api("POST", "/runs", body);
    await api("PUT", "/brightness", { targets, brightness: S.brightness });
    await loadControllers();
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

// ---------- scenes ----------

async function loadScenes() {
  try {
    S.scenes = (await api("GET", "/scenes")).scenes;
  } catch {
    S.scenes = [];
  }
  renderScenes();
}

function renderScenes() {
  const items = S.scenes.map((sc) =>
    el("div", { class: "pe-scene" },
      el("button", {
        class: `pe-item${sc.active ? " current" : ""}`,
        title: `Activate: ${sc.entries.map((e) => `${e.pattern} on ${e.targets.map(targetLabel).join(", ")}`).join("; ")}`,
        onclick: () => activateScene(sc.name),
      }, el("span", { class: "pe-play" }, "▶"), sc.name, sc.active ? el("span", { class: "tag ok" }, "active") : null),
      icon(el("button", {
        class: "btn btn-small btn-ghost pe-scene-btn", title: `Overwrite ${sc.name} with what's playing now`,
        "aria-label": `Overwrite ${sc.name} with what's playing now`,
        onclick: () => overwriteScene(sc.name),
      }), SAVE_ICON),
      el("button", {
        class: "btn btn-small btn-ghost btn-danger pe-scene-btn", title: `Delete ${sc.name}`, "aria-label": `Delete ${sc.name}`,
        onclick: () => deleteScene(sc.name),
      }, "×")));
  $("scene-list").replaceChildren(...(items.length ? items
    : [el("div", { class: "muted small" }, "None yet. Run patterns, then “Save as scene” under Running.")]));
}

async function activateScene(name) {
  try {
    const res = await api("POST", `/scenes/${encodeURIComponent(name)}/activate`);
    if (res.errors.length) banner(`${name} started, except: ${res.errors.join("; ")}`, "err");
    else banner(null);
  } catch (e) {
    banner(`${name} didn't start: ${e.message}`, "err");
  }
  await Promise.all([loadRuns(), loadControllers(), loadScenes()]);
  renderTargets();
}

async function deleteScene(name) {
  if (!confirm(`Delete scene ${name}? Whatever it started keeps playing.`)) return;
  try {
    await api("DELETE", `/scenes/${encodeURIComponent(name)}`);
  } catch (e) {
    banner(`Could not delete: ${e.message}`, "err");
  }
  await loadScenes();
}

// What's playing now as scene entries: one per run and brightness level (editor code can't go in a scene).
// Returns null (after explaining why) when nothing from the library is playing.
async function runningAsEntries() {
  await Promise.all([loadRuns(), loadControllers()]);
  const usable = S.runs.filter((r) => r.pattern);
  if (!usable.length) { banner("Nothing from the library is playing. Save the pattern, then run it.", "err"); return null; }
  const level = (t) => {
    for (const c of S.controllers) {
      const ch = c.channels.find((x) => x.target === t);
      if (ch) return ch.brightness ?? 1;
    }
    return 1;
  };
  const entries = [];
  for (const r of usable) {
    const groups = new Map();
    for (const t of r.targets) groups.set(level(t), [...(groups.get(level(t)) || []), t]);
    for (const [brightness, targets] of groups) entries.push({ targets, pattern: r.pattern, params: r.params, brightness });
  }
  return { entries, skipped: S.runs.length - usable.length };
}

function describeEntries(entries) {
  return entries.map((e) => `• ${e.pattern} on ${e.targets.map(targetLabel).join(", ")}` +
    (e.brightness < 1 ? ` at ${Math.round(e.brightness * 100)}%` : "")).join("\n");
}

async function storeScene(name, { entries, skipped }) {
  try {
    await api("PUT", `/scenes/${encodeURIComponent(name)}`, { entries });
  } catch (e) {
    banner(`Scene not saved: ${e.message}`, "err");
    return;
  }
  banner(skipped ? `Saved ${name}, without ${skipped} run${skipped > 1 ? "s" : ""} of unsaved editor code.` : `Saved ${name}.`,
    skipped ? "err" : "ok");
  if (!skipped) setTimeout(() => banner(null), 2500);
  await loadScenes();
}

async function saveAsScene() {
  const running = await runningAsEntries();
  if (!running) return;
  const active = S.scenes.find((sc) => sc.active);
  const name = (prompt("Scene name (lowercase letters, digits, - and _):", active?.name || "") || "").trim();
  if (!name) return;
  if (S.scenes.some((sc) => sc.name === name) &&
      !confirm(`Overwrite scene ${name} with what's playing now?\n\n${describeEntries(running.entries)}`)) return;
  await storeScene(name, running);
}

// The scene list's overwrite button: replace a scene with what's playing now, after confirming.
async function overwriteScene(name) {
  const running = await runningAsEntries();
  if (!running) return;
  const was = S.scenes.find((sc) => sc.name === name);
  const question = `Overwrite scene ${name} with what's playing now?\n\nNew:\n${describeEntries(running.entries)}` +
    (was ? `\n\nReplaces:\n${describeEntries(was.entries)}` : "");
  if (!confirm(question)) return;
  await storeScene(name, running);
}

async function stopAll() {
  try {
    await api("POST", "/blackout");
  } catch (e) {
    banner(`Could not stop: ${e.message}`, "err");
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
    showOutput(s.output);
  } catch {
    S.online = false;
    pill.className = "pill err";
    pill.textContent = "lightd offline";
  }
}

async function poll() {
  await pollStatus();
  await Promise.all([loadControllers(), loadRuns(), loadScenes()]);
  renderTargets();
  updateButtons();
}

// ---------- preview controls ----------

function restartPreview() {
  compilePreview();
}

// Pausing freezes t; playing again jumps back to the current time.
function togglePlay() {
  const p = S.preview;
  if (p.playing) p.pausedAt = Date.now() / 1000;
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
  $("stop-all-btn").addEventListener("click", stopAll);
  initOutputSwitch(api, (msg) => banner(msg, "err"));
  $("refresh-btn").addEventListener("click", poll);
  $("preview-play").addEventListener("click", togglePlay);
  $("save-scene-btn").addEventListener("click", saveAsScene);
  $("params-reset").addEventListener("click", resetParams);
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
  renderParams();
  setInterval(poll, POLL_MS);
  pollApiValues();
}

bootstrap();
