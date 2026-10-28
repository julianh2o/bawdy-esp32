/**
 * Interface (/interface, also /patterns): pick a pattern or scene, tune it, stage it on deck, then Send it to the
 * strips.
 *
 *  - Selected panel: a working copy of one pattern (code, params, brightness). Clicking a Library pattern, a deck
 *    line or a ▶ run on a light selects it. Edits only touch the copy until "Load to deck".
 *  - On deck: one line per strip, kept in localStorage. Nothing reaches the strips until Send. The orange deck boxes
 *    choose which lines "Load to deck" fills; staging a scene replaces the whole deck.
 *  - Send: one POST /runs per group of identical lines (or POST /scenes/<name>/activate when the deck is exactly a
 *    saved scene). Lines already matching what's playing are skipped.
 *  - Editor modal: the CodeMirror editor, the full preview and the assistant chat, all on the working copy.
 *
 * Talks to lightd through the web server's /engine/* proxy. Previews run lightd's own runtime.lua in wasmoon
 * (Lua 5.4 in WASM), so they render exactly what the LEDs will show.
 */

import { LuaFactory } from "https://cdn.jsdelivr.net/npm/wasmoon@1.16.0/+esm";
import { initOutputSwitch, showOutput } from "./output_switch.js";
import { initAssistant, askQuick } from "./assistant.js";

const API = "/engine/api";
const WASM_URL = "https://cdn.jsdelivr.net/npm/wasmoon@1.16.0/dist/glue.wasm";
const POLL_MS = 2000;
const PREVIEW_FPS = 30;
const MINI_FPS = 10; // deck-line and live-bar previews
const API_VALUES_MS = 100; // how often the preview fetches values for type = "api" params
const DECK_KEY = "lightd.deck";
const NAME_RE = /^[a-z0-9_-]{1,64}$/;
const NEW_EXAMPLE = "solid"; // + New starts from this example

const S = {
  library: [],
  sources: new Map(), // library pattern name -> its saved source
  controllers: [],
  runs: [],
  scenes: [],
  online: false,
  schema: {}, // params declared by the working copy's code (last version that compiled)
  apiValues: {}, // the values API (/values), fed to type = "api" params in the preview
  // The working copy: {name, source: library|deck|live|new, target, scene, code, params, brightness,
  //                    base: {name, code, params, brightness}, builtin, modified}
  sel: null,
  deck: {}, // target -> {pattern, code (unsaved source, or null for the library pattern), params, brightness, from}
  deckOn: new Set(), // the orange deck boxes: where "Load to deck" lands
  deckScene: null, // the scene the deck was staged from (shown as "scene: x · modified")
  sentCode: new Map(), // run id -> source, for ad-hoc runs this page started (lightd doesn't report run source)
  scenePick: "", // the scene picked in the narrow-screen dropdown
  wizBusy: false,
  boardSig: "",
  preview: { compile: null, render: null, clearParams: null, setParam: null, schema: null, check: null, ok: false, playing: true, pausedAt: 0, last: 0, lastMini: 0, error: null },
  mini: { compile: null, clear: null, param: null, render: null, ids: new Map(), errors: new Map(), next: 1 },
};
let editor = null;
let errorMark = null;
let settingEditor = false; // true while the page (not the user) changes the editor's text

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
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

// Fill a <select> with [{value, label}], keeping the user's choice. Rebuilt only when the options change, so the
// 2-second poll doesn't close a dropdown someone has open.
function syncSelect(select, options, value) {
  const sig = JSON.stringify(options);
  if (select.dataset.sig !== sig) {
    select.replaceChildren(...options.map((o) => el("option", { value: o.value, disabled: o.disabled }, o.label)));
    select.dataset.sig = sig;
  }
  select.value = value;
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

let bannerTimer = null;
function banner(message, kind = "", ms = 0) {
  const b = $("banner");
  clearTimeout(bannerTimer);
  if (!message) { b.hidden = true; return; }
  b.hidden = false;
  b.className = `banner ${kind}`;
  b.textContent = message;
  if (ms) bannerTimer = setTimeout(() => { b.hidden = true; }, ms);
}

// ---------- params comparison ----------

function schemaFor(pattern) {
  return S.library.find((p) => p.name === pattern)?.params || {};
}

// Params with defaults filled in (api params left out: they're live values), as a comparable string.
function paramsKey(params, schema) {
  const out = {};
  for (const [k, spec] of Object.entries(schema || {})) {
    if (spec.type === "api") continue;
    const v = params?.[k];
    out[k] = v === undefined || v === null ? spec.default : v;
  }
  for (const [k, v] of Object.entries(params || {})) if (!(k in out) && schema?.[k]?.type !== "api") out[k] = v;
  return JSON.stringify(Object.keys(out).sort().map((k) => [k, typeof out[k] === "string" ? out[k].toLowerCase() : out[k]]));
}
const sameParams = (a, b, schema) => paramsKey(a, schema) === paramsKey(b, schema);
const sameLevel = (a, b) => Math.abs((a ?? 1) - (b ?? 1)) < 0.005;

// ---------- the working copy ----------

const codeChanged = (s = S.sel) => !!s && s.code !== s.base.code;
const paramsChanged = (s = S.sel) => !!s && (!sameParams(s.params, s.base.params, S.schema) || !sameLevel(s.brightness, s.base.brightness));
const inLibrary = (name) => S.library.some((p) => p.name === name);
// The working copy's code isn't the saved library pattern of its name (so deck lines must carry the code itself).
const unsavedCode = (s = S.sel) => !!s && (!inLibrary(s.name) || S.sources.get(s.name) !== s.code);

// Ask before throwing away code edits that live nowhere else (not on deck, not in the library).
function confirmDiscard() {
  const s = S.sel;
  if (!s || !codeChanged(s)) return true;
  if (S.sources.get(s.name) === s.code) return true;
  if (Object.values(S.deck).some((l) => l?.code === s.code)) return true;
  return confirm(`Discard the code changes to ${s.name}? Load it to deck or save it to the library to keep them.`);
}

async function patternSource(name) {
  if (S.sources.has(name)) return S.sources.get(name);
  const p = await api("GET", `/patterns/${encodeURIComponent(name)}`);
  S.sources.set(name, p.source);
  return p.source;
}

// Fetch the sources of library patterns used on deck or playing, for their mini previews.
let fetching = new Set();
function ensureSources() {
  const names = new Set([...Object.values(S.deck).filter((l) => l && !l.code).map((l) => l.pattern),
    ...S.runs.filter((r) => r.pattern).map((r) => r.pattern)]);
  for (const name of names) {
    if (S.sources.has(name) || fetching.has(name) || !inLibrary(name)) continue;
    fetching.add(name);
    patternSource(name).catch(() => {}).finally(() => { fetching.delete(name); });
  }
}

function setSel(sel, { count, reversed } = {}) {
  S.sel = sel;
  S.wizOpen = false;
  $("wiz").hidden = true;
  $("wiz-status").hidden = true;
  if (count) $("preview-count").value = count;
  if (reversed !== undefined) $("preview-reversed").checked = !!reversed;
  S.schema = sel ? (sel.code === S.sources.get(sel.name) ? schemaFor(sel.name) : S.schema) : {};
  settingEditor = true;
  editor.setValue(sel ? sel.code : "");
  editor.clearHistory();
  settingEditor = false;
  $("name-input").value = sel ? sel.name : "";
  compilePreview();
  renderSelected();
  renderLibrary();
  renderBoard(true);
}

function makeSel(fields) {
  const sel = { target: null, scene: null, params: {}, brightness: 1, builtin: false, modified: false, ...fields };
  sel.params = { ...sel.params };
  sel.base = { name: sel.name, code: sel.code, params: { ...sel.params }, brightness: sel.brightness };
  const lib = S.library.find((p) => p.name === sel.name);
  if (lib && S.sources.get(sel.name) === sel.code) { sel.builtin = lib.builtin; sel.modified = lib.modified; }
  return sel;
}

async function selectLibrary(name) {
  if (!confirmDiscard()) return false;
  try {
    S.sources.delete(name); // fresh copy: it may have been saved elsewhere
    const code = await patternSource(name);
    setSel(makeSel({ name, source: "library", code }));
    return true;
  } catch (e) {
    banner(`Could not open ${name}: ${e.message}`, "err");
    return false;
  }
}

async function selectDeck(target) {
  const line = S.deck[target];
  if (!line || !confirmDiscard()) return;
  let code = line.code;
  if (code === null) {
    try { code = await patternSource(line.pattern); } catch (e) { banner(`Could not open ${line.pattern}: ${e.message}`, "err"); return; }
  }
  // Tick every deck line holding the same pattern (and code) with the same param values.
  S.deckOn = new Set(Object.entries(S.deck).filter(([, l]) => l && l.pattern === line.pattern && l.code === line.code &&
    sameParams(l.params, line.params, schemaFor(line.pattern))).map(([t]) => t));
  saveDeck();
  const ch = channel(target);
  setSel(makeSel({ name: line.pattern, source: "deck", target, code, params: line.params, brightness: line.brightness ?? 1,
    from: line.from }), { count: ch?.length, reversed: ch?.reversed });
}

async function selectLive(target) {
  const run = runOn(target);
  if (!run || !confirmDiscard()) return;
  let code = run.pattern ? null : S.sentCode.get(run.id);
  if (run.pattern) {
    try { code = await patternSource(run.pattern); } catch (e) { banner(`Could not open ${run.pattern}: ${e.message}`, "err"); return; }
  }
  if (code == null) return; // ad-hoc code started elsewhere: lightd doesn't hand its source out
  S.deckOn = new Set(run.targets.filter((t) => channel(t)));
  saveDeck();
  const ch = channel(target);
  setSel(makeSel({ name: run.pattern || "untitled", source: "live", target, scene: run.scene, code, params: run.params || {},
    brightness: ch?.brightness ?? 1 }), { count: ch?.length, reversed: ch?.reversed });
}

async function newPattern() {
  if (!confirmDiscard()) return;
  let code = null;
  try { code = (await api("GET", `/examples/${NEW_EXAMPLE}`)).source; } catch { /* older lightd without examples */ }
  if (code == null) {
    try { code = await patternSource(NEW_EXAMPLE); } catch { code = "function pixel(n, t, p)\n  return 1, 0.5, 0\nend\n"; }
  }
  setSel(makeSel({ name: "untitled", source: "new", code }));
  openEditor();
  $("name-input").select();
}

// Example picked in the editor: a new, unsaved working copy with its code.
function loadExample(ex) {
  if (!confirmDiscard()) return;
  setSel(makeSel({ name: ex.name, source: "new", code: ex.source }));
  banner(`Loaded example ${ex.name}. Load it to deck to try it, or save it to the library to keep it.`, "ok", 3500);
}

// ---------- the Selected panel ----------

const SOURCE_TAGS = {
  library: ["library pattern", "lib"],
  deck: ["on deck", "warn"],
  live: ["live", "ok"],
  new: ["new · unsaved", ""],
};

function renderSelected() {
  const s = S.sel;
  const panel = $("selp");
  panel.classList.toggle("empty", !s);
  if (!s) {
    $("sel-name").textContent = "";
    $("sel-tag").textContent = "";
    $("sel-dirty").textContent = "";
    $("sel-sub").textContent = "Pick a Library pattern, click a deck line, or click ▶ on a light to select it here.";
    $("params-form").replaceChildren();
    updateSelButtons();
    return;
  }
  $("sel-name").textContent = s.name;
  const [tag, kind] = SOURCE_TAGS[s.source];
  $("sel-tag").textContent = tag;
  $("sel-tag").className = `tag ${kind}`;
  const where = {
    library: () => `saved in the library${s.modified ? " (edited built-in)" : s.builtin ? " (built-in)" : ""} · defaults loaded`,
    deck: () => `from ${chName(s.target)}'s deck line${s.from ? ` (${s.from})` : ""} · ${plural(S.deckOn.size, "matching line")} ticked`,
    live: () => `playing on ${chName(s.target)}${s.scene ? ` · scene ${s.scene}` : ""}`,
    new: () => "not in the library yet · Save to library to keep it",
  }[s.source]();
  $("sel-sub").textContent = where;
  updateSelMarkers();
  renderParams();
}

function updateSelMarkers() {
  const s = S.sel;
  const marks = s ? [paramsChanged(s) ? "params changed" : null, codeChanged(s) ? "code changed" : null].filter(Boolean) : [];
  $("sel-dirty").textContent = marks.map((m) => `· ${m}`).join(" ");
  $("dirty-indicator").textContent = marks.length ? marks.join(" · ") : "";
  if (s) {
    const [tag] = SOURCE_TAGS[s.source];
    $("source-tag").textContent = s.source === "deck" || s.source === "live" ? `${tag} · ${chName(s.target)}` : tag;
  } else $("source-tag").textContent = "";
  updateSelButtons();
}

function updateSelButtons() {
  const s = S.sel;
  const n = S.deckOn.size;
  const compiles = !s || S.preview.ok || S.preview.compile === null;
  $("load-btn").disabled = !s || !n || !compiles;
  $("load-btn").textContent = n ? `Load to deck (${n}) ↓` : "Load to deck ↓";
  $("load-btn").title = !n ? "Tick the orange deck boxes to choose where it goes" : !compiles ? "Fix the code first" : "";
  const overwrite = s?.source === "library" && s.name === s.base.name;
  for (const id of ["save-lib-btn", "save-btn"]) {
    const b = $(id);
    b.disabled = !s || (overwrite && !codeChanged(s));
    b.textContent = overwrite ? "Save to library" : "Save to library…";
    b.title = overwrite ? (codeChanged(s) ? `Overwrite ${s.name} in the library` : "No code changes to save (params aren't stored in patterns)")
      : "Save as a new library pattern";
  }
  $("open-editor-btn").disabled = !s;
  $("ask-ai-btn").disabled = !s;
  $("params-reset").disabled = !s || !paramsChanged(s);
  const del = $("delete-btn");
  const lib = s && S.library.find((p) => p.name === s.name);
  const deletable = s?.source === "library" && lib && (!lib.builtin || lib.modified);
  del.hidden = !deletable;
  del.textContent = lib?.modified ? "Revert" : "Delete";
  del.title = lib?.modified ? "Go back to the original built-in version" : `Delete ${s?.name || ""} from the library`;
  $("ed-reset-btn").disabled = !codeChanged(s);
}

// ---------- params panel ----------

function renderParams() {
  const s = S.sel;
  if (!s) { $("params-form").replaceChildren(); return; }
  const names = Object.keys(S.schema).filter((name) => S.schema[name].type !== "api").sort();
  const rows = names.flatMap((name) => {
    const spec = S.schema[name];
    const isSet = Object.hasOwn(s.params, name);
    const value = isSet ? s.params[name] : spec.default;
    const label = el("span", { class: `pe-param-name${isSet ? " set" : ""}`, title: name }, spec.label || name);
    const reset = el("button", {
      class: `btn btn-small btn-ghost pe-reset${isSet ? " set" : ""}`, type: "button",
      title: `Back to the default (${spec.default})`,
      "aria-label": `Reset ${spec.label || name}`,
      onclick: () => { delete s.params[name]; pushPreviewParams(); renderParams(); updateSelMarkers(); },
    }, "↺");
    const set = (v) => {
      s.params[name] = v;
      label.classList.add("set");
      reset.classList.add("set");
      pushPreviewParams();
      updateSelMarkers();
    };
    return [label, paramInput(spec, value, set, spec.label || name), reset];
  });
  const pct = Math.round(s.brightness * 100);
  const num = el("span", { class: "num" }, `${pct}%`);
  rows.push(
    el("span", { class: "pe-param-name", title: "Channel brightness, applied on Send" }, "Brightness"),
    el("span", { class: "pe-value" },
      el("input", {
        type: "range", min: 0, max: 100, value: pct, "aria-label": "Brightness",
        oninput: (e) => { s.brightness = e.target.value / 100; num.textContent = `${e.target.value}%`; updateSelMarkers(); },
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

// Reset: params and brightness back to where the selection came from.
function resetParams() {
  const s = S.sel;
  if (!s) return;
  s.params = { ...s.base.params };
  s.brightness = s.base.brightness;
  pushPreviewParams();
  renderParams();
  updateSelMarkers();
}

// ---------- Load to deck / Save to library ----------

function loadToDeck() {
  const s = S.sel;
  if (!s || !S.deckOn.size) return;
  if (S.preview.compile && !S.preview.ok) { banner("The code doesn't compile yet; fix it in the editor first.", "err"); return; }
  const edited = codeChanged(s) || paramsChanged(s);
  const from = s.source === "new" ? "new" : edited ? "edited" : s.source === "library" ? "library" : "copied";
  const code = unsavedCode(s) ? s.code : null;
  for (const t of S.deckOn) {
    S.deck[t] = { pattern: s.name, code, params: { ...s.params }, brightness: s.brightness, from };
  }
  saveDeck();
  renderBoard(true);
  banner(`Loaded ${s.name} onto ${plural(S.deckOn.size, "deck line")}. Send ▶ to play it.`, "ok", 2500);
}

async function saveToLibrary() {
  const s = S.sel;
  if (!s) return;
  const typed = ($("name-input").value.trim() || s.name).toLowerCase();
  let name = s.name;
  const overwrite = s.source === "library" && s.name === s.base.name;
  if (!overwrite) {
    name = (prompt("Save to the library as (lowercase letters, digits, - and _):", typed === "untitled" ? "" : typed) || "").trim();
    if (!name) return;
    if (!NAME_RE.test(name)) { banner("Pattern names use lowercase letters, digits, - and _.", "err"); return; }
    const existing = S.library.find((p) => p.name === name);
    if (existing?.builtin && !existing.modified) {
      if (!confirm(`Replace the built-in pattern '${name}' with this code? Revert brings the original back.`)) return;
    } else if (existing && !confirm(`A pattern named ${name} exists. Replace it?`)) return;
  } else if (s.builtin && !s.modified &&
      !confirm(`Replace the built-in pattern '${name}' with this code? Revert brings the original back.`)) return;
  const code = s.code;
  try {
    const res = await api("PUT", `/patterns/${encodeURIComponent(name)}`, { source: code });
    S.sources.set(name, code);
    await loadLibrary();
    // Deck lines carrying this exact code now hold the saved pattern instead.
    for (const line of Object.values(S.deck)) {
      if (line?.code === code) { line.code = null; line.pattern = name; }
    }
    saveDeck();
    s.name = name;
    s.source = "library";
    s.builtin = res.builtin;
    s.modified = res.modified;
    s.base = { name, code, params: { ...s.base.params }, brightness: s.base.brightness };
    $("name-input").value = name;
    banner(`Saved ${name} to the library.`, "ok", 2500);
    renderSelected();
    renderBoard(true);
  } catch (e) {
    showError(e.message, e.line);
    banner(`Not saved: ${e.message}`, "err");
  }
}

// Delete a user pattern, or revert an edited built-in to the original.
async function deletePattern() {
  const s = S.sel;
  const lib = s && S.library.find((p) => p.name === s.name);
  if (!lib || (lib.builtin && !lib.modified)) return;
  const question = lib.modified
    ? `Revert ${s.name} to the built-in version? Your edits are lost; runs already playing them keep going.`
    : `Delete ${s.name}? Runs already playing it keep going; deck lines keep a copy of its code.`;
  if (!confirm(question)) return;
  const old = S.sources.get(s.name);
  try {
    await api("DELETE", `/patterns/${encodeURIComponent(s.name)}`);
    S.sources.delete(s.name);
    await loadLibrary();
    if (lib.modified) {
      await selectLibrary(s.name);
    } else {
      for (const line of Object.values(S.deck)) if (line && line.pattern === s.name && !line.code && old) line.code = old;
      saveDeck();
      s.source = "new";
      s.base = { ...s.base, name: s.name };
      renderSelected();
      renderLibrary();
      renderBoard(true);
    }
  } catch (e) {
    banner(`Could not delete: ${e.message}`, "err");
  }
}

// ---------- Ask AI (the small prompt in the panel) ----------

function toggleWiz(open = $("wiz").hidden) {
  $("wiz").hidden = !open;
  if (open) {
    $("wiz-input").placeholder = `make ${S.sel?.name || "it"} slower and more purple…`;
    $("wiz-input").focus();
  }
}

async function sendWiz(e) {
  e?.preventDefault();
  const text = $("wiz-input").value.trim();
  if (!text || S.wizBusy || !S.sel) return;
  S.wizBusy = true;
  $("wiz-send").disabled = true;
  const status = $("wiz-status");
  status.hidden = false;
  status.className = "muted small";
  status.textContent = "Asking the assistant…";
  const res = await askQuick(text, (msg) => { status.textContent = msg; });
  S.wizBusy = false;
  $("wiz-send").disabled = false;
  if (res.error) {
    status.className = "small err";
    status.textContent = res.error;
  } else if (res.applied) {
    $("wiz-input").value = "";
    status.textContent = "Applied to the selected pattern ✓ (the chat is in the editor; Ctrl-Z there undoes it)";
  } else {
    status.textContent = res.note || "No working code came back. Open the editor to see the reply.";
  }
}

// ---------- errors in the editor ----------

function showError(message, line) {
  if (errorMark !== null) { editor.removeLineClass(errorMark, "background", "error-line"); errorMark = null; }
  for (const id of ["error-bar", "sel-error"]) {
    const bar = $(id);
    bar.hidden = !message;
    bar.textContent = message || "";
  }
  if (!message) return;
  const m = line ?? /^pattern:(\d+):/.exec(message)?.[1];
  if (m) errorMark = editor.addLineClass(Number(m) - 1, "background", "error-line");
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
        return '"' .. v:gsub('[%c"\\\\\\\\]', function(c) return string.format("\\\\\\\\u%04x", c:byte()) end) .. '"'
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
    -- For the assistant: compile code in its own environment and render a few frames with default params,
    -- on RGB and RGBW strips. "" if it works, else the error. Doesn't touch the editor's preview.
    function preview_check(src, count, t)
      local e, err = RT.compile(src)
      if not e then return tostring(err) end
      local ok, perr = pcall(RT.params, e, {})
      if not ok then return "params: " .. tostring(perr) end
      local cases = { { 1, false, false }, { count, false, false }, { count, true, true } }
      for i, c in ipairs(cases) do
        local ok2, data = RT.render_hex(e, c[1], t + i * 7.3, perr, c[2], c[3])
        if not ok2 then return tostring(data) end
      end
      return ""
    end
    -- Mini previews (deck lines, live bars): one compiled environment per distinct source.
    local minis = {}
    function mini_compile(id, src)
      local e, err = RT.compile(src)
      if not e then minis[id] = nil; return tostring(err) end
      minis[id] = { env = e, values = {} }
      return ""
    end
    function mini_clear(id)
      local m = minis[id]
      if m then m.values = {} end
    end
    function mini_param(id, k, v)
      local m = minis[id]
      if m then m.values[k] = v end
    end
    function mini_render(id, count, t, reversed)
      local m = minis[id]
      if not m then return "" end
      local okp, pp = pcall(RT.params, m.env, m.values)
      if not okp then return "!" .. tostring(pp) end
      local ok, data = RT.render_hex(m.env, count, t, pp, reversed)
      if not ok then return "!" .. tostring(data) end
      return data
    end
  `);
  S.preview.compile = lua.global.get("preview_compile");
  S.preview.render = lua.global.get("preview_render");
  S.preview.clearParams = lua.global.get("preview_clear_params");
  S.preview.setParam = lua.global.get("preview_param");
  S.preview.schema = lua.global.get("preview_schema");
  S.preview.check = lua.global.get("preview_check");
  S.mini.compile = lua.global.get("mini_compile");
  S.mini.clear = lua.global.get("mini_clear");
  S.mini.param = lua.global.get("mini_param");
  S.mini.render = lua.global.get("mini_render");
}

function compilePreview() {
  if (!S.preview.compile) return;
  const code = S.sel?.code ?? "";
  if (!code.trim()) {
    S.preview.ok = false;
    S.preview.error = null;
    if (!S.sel) S.schema = {};
    showError(null);
    renderParams();
    updateSelButtons();
    return;
  }
  const err = S.preview.compile(code);
  S.preview.ok = !err;
  S.preview.error = err || null;
  showError(err || null);
  if (!err) {
    try { S.schema = JSON.parse(S.preview.schema() || "{}"); } catch { /* keep the last schema */ }
    pushPreviewParams();
    renderParams();
  }
  updateSelMarkers();
}

function pushPreviewParams() {
  if (!S.preview.setParam) return;
  S.preview.clearParams();
  for (const [k, v] of Object.entries(S.sel?.params || {})) if (v !== null && v !== undefined) S.preview.setParam(k, v);
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

// Paint a hex frame ("rrggbb" per pixel) onto a canvas. gaps: separate the pixels when there's room.
function paintStrip(canvas, data, count, { gaps = false } = {}) {
  const dpr = window.devicePixelRatio || 1;
  const w = Math.round(canvas.clientWidth * dpr);
  const h = Math.round(canvas.clientHeight * dpr);
  if (!w || !h) return;
  if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = "#07080a";
  ctx.fillRect(0, 0, w, h);
  if (!data) return;
  const cell = w / count;
  const gap = gaps && cell >= 4 ? Math.max(1, cell * 0.15) : 0;
  const height = gaps ? Math.round(h * 0.6) : h;
  for (let i = 0; i < count; i++) {
    ctx.fillStyle = `#${data.substr(i * 6, 6)}`;
    ctx.fillRect(i * cell + gap / 2, (h - height) / 2, Math.max(1, cell - gap + (gap ? 0 : 0.6)), height);
  }
}

const modalOpen = () => !$("ed-scrim").hidden;

function drawPreview(now) {
  requestAnimationFrame(drawPreview);
  if (document.hidden) return;
  if (now - S.preview.lastMini >= 1000 / MINI_FPS) { S.preview.lastMini = now; drawMinis(); }
  if (now - S.preview.last < 1000 / PREVIEW_FPS) return;
  S.preview.last = now;
  const big = $("sel-canvas");
  const full = $("preview-canvas");
  const showFull = modalOpen();
  if (!S.preview.render || !S.preview.ok || !S.sel) {
    paintStrip(big, null, 1);
    if (showFull) paintStrip(full, null, 1);
    return;
  }
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
  paintStrip(big, data, count);
  if (showFull) {
    paintStrip(full, data, count, { gaps: true });
    $("preview-time").textContent = `t = ${t.toFixed(1)} (${new Date(t * 1000).toLocaleTimeString()})`;
    $("preview-perf").textContent = `${count} px · ${ms.toFixed(1)} ms/frame`;
  }
}

// ---------- mini previews ----------

function miniId(code) {
  let id = S.mini.ids.get(code);
  if (id === undefined) {
    id = S.mini.next++;
    S.mini.ids.set(code, id);
    const err = S.mini.compile(id, code);
    if (err) S.mini.errors.set(id, err);
  }
  return id;
}

function drawMinis() {
  if (!S.mini.render) return;
  const t0 = performance.now();
  const t = Date.now() / 1000;
  const frames = new Map();
  for (const canvas of document.querySelectorAll("canvas.mini")) {
    const spec = canvas._spec;
    if (!spec) continue;
    const code = spec.code ?? (spec.pattern ? S.sources.get(spec.pattern) : null);
    if (!code) { paintStrip(canvas, null, 1); continue; }
    const id = miniId(code);
    if (S.mini.errors.has(id)) { paintStrip(canvas, null, 1); canvas.title = S.mini.errors.get(id); continue; }
    const count = Math.max(1, Math.min(2000, spec.count || 60));
    const key = `${id}|${count}|${spec.reversed ? 1 : 0}|${JSON.stringify(spec.params || {})}`;
    if (!frames.has(key)) {
      S.mini.clear(id);
      for (const [k, v] of Object.entries(spec.params || {})) if (v !== null && v !== undefined) S.mini.param(id, k, v);
      const data = S.mini.render(id, count, t, !!spec.reversed);
      frames.set(key, data.startsWith("!") ? null : data);
    }
    paintStrip(canvas, frames.get(key), count);
  }
  S.preview.miniMs = performance.now() - t0;
}

function miniCanvas(spec, cls = "") {
  const c = el("canvas", { class: `mini ${cls}`, height: 8 });
  c._spec = spec;
  return c;
}

// ---------- library & scenes (left column) ----------

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
  const mine = S.library;
  const current = S.sel?.source === "library" ? S.sel.name : null;
  const item = (p) =>
    el("button", {
      class: `pe-item${p.name === current ? " current" : ""}`, type: "button",
      onclick: () => selectLibrary(p.name),
      title: p.error ? p.error.message : `Select ${p.name}`,
    }, p.name,
    p.modified ? el("span", { class: "tag", title: "Your edited copy is used instead of the built-in" }, "edited") : null,
    p.error ? el("span", { class: "tag err" }, "error") : null);
  $("library-list").replaceChildren(
    ...(mine.length ? mine.map(item) : [el("div", { class: "muted small" }, "Nothing saved yet.")]),
  );
  // Narrow screens: the same library as a dropdown; picking a pattern selects it.
  const label = (p) => `${p.name}${p.modified ? " (edited)" : ""}${p.error ? " (error)" : ""}`;
  const options = [
    { value: "", label: "Library pattern…", disabled: true },
    ...mine.map((p) => ({ value: p.name, label: label(p) })),
  ];
  syncSelect($("library-select"), options, current && mine.some((p) => p.name === current) ? current : "");
}

async function loadScenes() {
  try {
    S.scenes = (await api("GET", "/scenes")).scenes;
    if (S.deckScene && !S.scenes.some((sc) => sc.name === S.deckScene)) { S.deckScene = null; saveDeck(); }
  } catch {
    /* keep the last list */
  }
  renderScenes();
}

function renderScenes() {
  const items = S.scenes.map((sc) =>
    el("div", { class: "pe-scene" },
      el("button", {
        class: "btn btn-small btn-ghost pe-scene-play", type: "button",
        title: `Play ${sc.name} now`, "aria-label": `Play ${sc.name} now`,
        onclick: () => activateScene(sc.name),
      }, "▶"),
      el("button", {
        class: `pe-item${sc.name === S.deckScene ? " current" : ""}`, type: "button",
        title: `Stage on deck: ${sc.entries.map((e) => `${e.pattern} on ${e.targets.map(chName).join(", ")}`).join("; ")}`,
        onclick: () => stageScene(sc.name),
      }, sc.name, sc.active ? el("span", { class: "tag ok" }, "active") : null)));
  $("scene-list").replaceChildren(...(items.length ? items
    : [el("div", { class: "muted small" }, "None yet. Fill the deck, then “Save as…” on the On deck header.")]));
  // Narrow screens: a dropdown that stages, with ▶ to play the picked one.
  if (S.scenePick && !S.scenes.some((sc) => sc.name === S.scenePick)) S.scenePick = "";
  const options = [
    { value: "", label: S.scenes.length ? "Scene…" : "No scenes yet", disabled: true },
    ...S.scenes.map((sc) => ({ value: sc.name, label: `${sc.name}${sc.active ? " · active" : ""}` })),
  ];
  syncSelect($("scene-select"), options, S.scenePick || "");
  $("scene-play").hidden = !S.scenePick;
}

// Staging a scene replaces the whole deck: lines it doesn't cover become empty.
function stageScene(name) {
  const sc = S.scenes.find((x) => x.name === name);
  if (!sc) return;
  S.deck = {};
  for (const e of sc.entries) {
    for (const t of e.targets) {
      S.deck[t] = { pattern: e.pattern, code: null, params: { ...(e.params || {}) }, brightness: e.brightness ?? 1, from: `scene ${name}` };
    }
  }
  S.deckScene = name;
  saveDeck();
  ensureSources();
  renderScenes();
  renderBoard(true);
}

async function activateScene(name) {
  try {
    const res = await api("POST", `/scenes/${encodeURIComponent(name)}/activate`);
    if (res.errors.length) banner(`${name} started, except: ${res.errors.join("; ")}`, "err");
    else banner(`Playing ${name}.`, "ok", 2000);
  } catch (e) {
    banner(`${name} didn't start: ${e.message}`, "err");
  }
  await refreshLive();
}

// ---------- deck state ----------

function saveDeck() {
  try {
    const runIds = new Set(S.runs.map((r) => r.id));
    localStorage.setItem(DECK_KEY, JSON.stringify({
      deck: S.deck, on: [...S.deckOn], scene: S.deckScene,
      sent: [...S.sentCode].filter(([id]) => runIds.has(id) || !S.runs.length),
    }));
  } catch { /* storage unavailable or full */ }
}

function loadDeck() {
  try {
    const d = JSON.parse(localStorage.getItem(DECK_KEY) || "{}");
    S.deck = d.deck && typeof d.deck === "object" ? d.deck : {};
    S.deckOn = new Set(Array.isArray(d.on) ? d.on : []);
    S.deckScene = d.scene || null;
    S.sentCode = new Map(Array.isArray(d.sent) ? d.sent : []);
  } catch { /* start empty */ }
  for (const [t, l] of Object.entries(S.deck)) if (!l || !l.pattern) delete S.deck[t];
}

function clearDeck() {
  S.deck = {};
  S.deckScene = null;
  saveDeck();
  renderScenes();
  renderBoard(true);
}

function unstage(target) {
  delete S.deck[target];
  saveDeck();
  renderBoard(true);
}

// The deck as scene entries: one per pattern + params + brightness. unsaved: lines holding unsaved code.
function deckEntries() {
  const groups = new Map();
  const unsaved = [];
  for (const [t, l] of Object.entries(S.deck)) {
    if (!l) continue;
    if (l.code) { unsaved.push(t); continue; }
    const key = JSON.stringify([l.pattern, paramsKey(l.params, schemaFor(l.pattern)), l.brightness ?? 1]);
    if (!groups.has(key)) groups.set(key, { targets: [], pattern: l.pattern, params: { ...l.params }, brightness: l.brightness ?? 1 });
    groups.get(key).targets.push(t);
  }
  return { entries: [...groups.values()], unsaved };
}

// The deck is exactly this saved scene: the same lines, nothing more.
function deckIsScene(name) {
  const sc = S.scenes.find((x) => x.name === name);
  if (!sc) return false;
  const want = {};
  for (const e of sc.entries) for (const t of e.targets) want[t] = e;
  const targets = new Set([...Object.keys(want), ...Object.keys(S.deck).filter((t) => S.deck[t])]);
  for (const t of targets) {
    const e = want[t];
    const l = S.deck[t];
    if (!e || !l || l.code || l.pattern !== e.pattern || !sameLevel(l.brightness, e.brightness) ||
        !sameParams(l.params, e.params, schemaFor(e.pattern))) return false;
  }
  return true;
}

const deckModified = () => !!S.deckScene && !deckIsScene(S.deckScene);

function describeEntries(entries) {
  return entries.map((e) => `• ${e.pattern} on ${e.targets.map(chName).join(", ")}` +
    (e.brightness < 1 ? ` at ${Math.round(e.brightness * 100)}%` : "")).join("\n");
}

// The deck's entries, or null (after saying why) when it can't become a scene.
function sceneEntriesOrExplain() {
  const { entries, unsaved } = deckEntries();
  if (unsaved.length) {
    const names = [...new Set(unsaved.map((t) => S.deck[t].pattern))];
    alert(`Only library patterns can go in a scene. Save ${names.join(", ")} to the library first ` +
      `(click the deck line, then Save to library), then save the scene.\n\nUnsaved code on: ${unsaved.map(chName).join(", ")}`);
    return null;
  }
  if (!entries.length) { banner("The deck is empty: load something onto it first.", "err"); return null; }
  return entries;
}

// Scenes can't mix inside and outside lights unless they say zone "both".
function zoneFor(entries, existing) {
  const zones = new Set(entries.flatMap((e) => e.targets).map((t) => channel(t)?.zone).filter(Boolean));
  if (zones.size < 2) return existing?.zone ?? undefined;
  if (existing?.zone === "both") return "both";
  return confirm("This deck mixes inside and outside lights. Save the scene for both zones?") ? "both" : null;
}

async function storeScene(name, entries, existing) {
  const zone = zoneFor(entries, existing);
  if (zone === null) return false;
  try {
    await api("PUT", `/scenes/${encodeURIComponent(name)}`, zone ? { entries, zone } : { entries });
  } catch (e) {
    banner(`Scene not saved: ${e.message}`, "err");
    return false;
  }
  S.deckScene = name;
  saveDeck();
  banner(`Saved scene ${name}.`, "ok", 2500);
  await loadScenes();
  renderBoard(true);
  return true;
}

async function saveScene() {
  const name = S.deckScene;
  if (!name) return saveSceneAs();
  const entries = sceneEntriesOrExplain();
  if (!entries) return;
  const was = S.scenes.find((sc) => sc.name === name);
  const question = `Overwrite scene ${name} with the deck?\n\nNew:\n${describeEntries(entries)}` +
    (was ? `\n\nReplaces:\n${describeEntries(was.entries)}` : "");
  if (!confirm(question)) return;
  await storeScene(name, entries, was);
}

async function saveSceneAs() {
  const entries = sceneEntriesOrExplain();
  if (!entries) return;
  const name = (prompt("New scene name (lowercase letters, digits, - and _):", "") || "").trim();
  if (!name) return;
  if (!NAME_RE.test(name)) { banner("Scene names use lowercase letters, digits, - and _.", "err"); return; }
  const was = S.scenes.find((sc) => sc.name === name);
  if (was && !confirm(`Overwrite scene ${name} with the deck?\n\n${describeEntries(entries)}`)) return;
  await storeScene(name, entries, was);
}

async function deleteScene() {
  const name = S.deckScene;
  if (!name || !confirm(`Delete scene ${name}? Whatever it started keeps playing, and the deck stays as it is.`)) return;
  try {
    await api("DELETE", `/scenes/${encodeURIComponent(name)}`);
    S.deckScene = null;
    saveDeck();
    banner(`Deleted scene ${name}.`, "ok", 2500);
  } catch (e) {
    banner(`Could not delete: ${e.message}`, "err");
  }
  await loadScenes();
  renderBoard(true);
}

// ---------- controllers, runs & lights ----------

// Polls and post-action refreshes overlap; only the newest request's answer is kept, so a slow poll that started
// before a Send can't put stale runs back on screen.
const latest = { controllers: [0, 0], runs: [0, 0] };
async function fresh(kind, load, apply) {
  const n = ++latest[kind][0];
  let value;
  try { value = await load(); } catch (e) { value = e; }
  if (n <= latest[kind][1]) return;
  latest[kind][1] = n;
  apply(value);
}

async function loadControllers() {
  await fresh("controllers", () => api("GET", "/controllers"), (res) => {
    if (res instanceof Error) { S.online = false; return; }
    S.controllers = res.controllers;
    S.online = true;
  });
}

async function loadRuns() {
  await fresh("runs", () => api("GET", "/runs"), (res) => {
    S.runs = res instanceof Error ? [] : res.runs;
  });
}

function channels() {
  return S.controllers.flatMap((c) => c.channels);
}

function channel(target) {
  for (const c of S.controllers) {
    const ch = c.channels.find((x) => x.target === target);
    if (ch) return ch;
  }
  return null;
}

function chName(target) {
  const ch = channel(target);
  return ch?.name || ch?.segment_name || target;
}

function runOn(target) {
  return S.runs.find((r) => r.targets.includes(target));
}

// The deck line already matches what's playing on its strip (so Send would change nothing there).
function lineMatches(target, line) {
  const run = runOn(target);
  if (!run || !line) return false;
  if (line.code ? run.pattern || S.sentCode.get(run.id) !== line.code : run.pattern !== line.pattern) return false;
  return sameParams(line.params, run.params, run.schema || schemaFor(line.pattern)) &&
    sameLevel(line.brightness, channel(target)?.brightness);
}

function pendingTargets() {
  return Object.keys(S.deck).filter((t) => S.deck[t] && channel(t)?.on_board && !lineMatches(t, S.deck[t]));
}

function renderBoard(force = false) {
  const sig = JSON.stringify([S.online, S.controllers, S.runs.map((r) => [r.id, r.targets, r.pattern, r.scene, r.params, !!r.error]),
    S.deck, [...S.deckOn], S.deckScene, S.sel && [S.sel.source, S.sel.target], [...S.sources.keys()].length, S.scenes.map((s) => s.name)]);
  if (!force && sig === S.boardSig) return;
  S.boardSig = sig;
  const rows = [];
  if (!S.online) {
    rows.push(el("div", { class: "board-empty" }, "lightd isn't reachable."));
  } else if (!S.controllers.length) {
    rows.push(el("div", { class: "board-empty" }, "No controllers. Adopt boards on the ", el("a", { href: "/config" }, "setup page"), "."));
  } else {
    for (const c of S.controllers) {
      rows.push(el("div", { class: "grp-head" },
        el("strong", {}, c.id),
        c.name ? el("span", { class: "muted small" }, c.name) : null,
        el("span", { class: "spacer" }),
        el("span", { class: `tag ${c.online ? "ok" : "warn"}`, title: c.error || "" }, c.online ? "online" : "offline")));
      if (!c.channels.length) {
        rows.push(el("div", { class: "grp-note muted small" }, c.online ? "No LED outputs reported." : "Channels appear once the board is reachable."));
      }
      for (const ch of c.channels) rows.push(stripRow(ch));
    }
  }
  $("board-rows").replaceChildren(...rows);
  // Every row gets an explicit grid row (--r), so the Send rail can span the whole gutter column and the
  // button stays sticky in it while the arrows sit underneath.
  rows.forEach((r, i) => r.style.setProperty("--r", String(i + 2)));
  $("send-rail").style.gridRow = `1 / span ${rows.length + 1}`;
  renderDeckHead();
  updateSendButton();
  ensureSources();
}

function stripRow(ch) {
  const t = ch.target;
  const usable = ch.on_board;
  const line = S.deck[t];
  const run = runOn(t);
  const on = S.deckOn.has(t);
  const pending = usable && line && !lineMatches(t, line);
  const isSel = S.sel?.source === "deck" && S.sel.target === t;
  const name = ch.name || ch.segment_name || `Ch ${ch.number}`;

  const box = el("input", {
    type: "checkbox", class: "dk-chk", checked: on, disabled: !usable,
    title: usable ? `Load to deck fills ${name}` : "This channel isn't on the board",
    "aria-label": `Deck line for ${name}: Load to deck fills it`,
    onchange: (e) => {
      e.target.checked ? S.deckOn.add(t) : S.deckOn.delete(t);
      saveDeck();
      updateSelButtons();
      renderBoard(true);
      if (S.sel?.source === "deck") renderSelected();
    },
  });

  const slot = line
    ? el("div", {
        class: `dk staged${on ? " match" : ""}${isSel ? " sel" : ""}`, role: "button", tabindex: 0,
        title: `${line.pattern} · click to select it`,
        onclick: (e) => { if (!e.target.closest("button")) selectDeck(t); },
        onkeydown: (e) => { if ((e.key === "Enter" || e.key === " ") && e.target === e.currentTarget) { e.preventDefault(); selectDeck(t); } },
      },
      el("div", { class: "dk-main" },
        el("span", { class: "dk-name" }, line.pattern,
          el("span", { class: "muted small" }, ` · ${line.from || "on deck"}`),
          line.code ? el("span", { class: "tag warn dk-unsaved", title: "Unsaved code: Send plays it, but it can't go in a scene until it's saved to the library" }, "unsaved") : null,
          (line.brightness ?? 1) < 1 ? el("span", { class: "muted small" }, ` · ${Math.round(line.brightness * 100)}%`) : null),
        miniCanvas({ pattern: line.code ? null : line.pattern, code: line.code, params: line.params, count: ch.length, reversed: ch.reversed })),
      el("button", {
        class: "btn btn-small btn-ghost dk-x", type: "button", title: `Empty ${name}'s deck line (Send leaves this strip alone)`,
        "aria-label": `Empty ${name}'s deck line`, onclick: () => unstage(t),
      }, "×"))
    : el("div", { class: `dk empty${on ? " match" : ""}` }, !usable ? "" : on ? "ticked · Load to deck fills this" : "empty");

  const arrow = el("div", {
    class: `dk-arrow${pending ? " on" : ""}`,
    title: !line ? "Nothing on deck for this strip" : pending ? "Send changes this strip" : "Already playing",
    "aria-hidden": "true",
  }, pending ? "➜" : "→");

  const light = el("div", { class: `pe-chan${usable ? "" : " disabled"}${S.sel?.source === "live" && S.sel.target === t ? " sel" : ""}`, title: t },
    el("span", { class: "pe-chan-text" },
      el("span", { class: "pe-chan-name", title: name }, name),
      run ? playingTag(run, t) : el("span", { class: "idle" }, usable ? "off" : ""),
      run ? miniCanvas({ pattern: run.pattern, code: run.pattern ? null : S.sentCode.get(run.id) ?? null, params: run.params, count: ch.length, reversed: ch.reversed }, "live-bar") : null),
    el("span", { class: "meta" }, usable
      // a linked Govee white segment makes the channel RGBW too
      ? `${ch.length}px ${ch.rgbw || ch.linked ? "RGBW" : "RGB"}${ch.reversed ? " · rev" : ""}${ch.brightness < 1 ? ` · ${Math.round(ch.brightness * 100)}%` : ""}`
      : "not on board"),
    run ? el("button", {
      class: "btn btn-small btn-ghost pe-stop", type: "button",
      title: `Stop ${run.pattern || "editor code"} on ${name} (it goes black)`,
      "aria-label": `Stop ${name}`,
      onclick: () => stopTargets([t]),
    }, "×") : el("span", { class: "pe-stop-slot" }));

  return el("div", { class: `dk-row${usable ? "" : " disabled"}`, dataset: { target: t } }, box, slot, arrow, light);
}

// "▶ pattern · scene" on a playing strip; click it to select the live run.
function playingTag(run, target) {
  const label = `▶ ${run.pattern || "editor code"}${run.scene ? ` · ${run.scene}` : ""}`;
  const known = run.pattern || S.sentCode.has(run.id);
  const title = run.error ? `Error: ${run.error.message}` : known ? "Playing now. Click to select it" : "Playing now (code started elsewhere)";
  if (!known) return el("span", { class: `busy${run.error ? " err" : ""}`, title }, label);
  return el("button", {
    class: `busy${run.error ? " err" : ""}`, type: "button", title,
    onclick: () => selectLive(target),
  }, label);
}

function renderDeckHead() {
  const name = S.deckScene;
  const modified = deckModified();
  const label = $("deck-scene");
  label.replaceChildren(...(name
    ? [el("span", {}, "scene: "), el("strong", {}, name), modified ? el("span", { class: "deck-mod" }, " · modified") : null]
    : [Object.keys(S.deck).length ? "not a saved scene" : ""]));
  const any = Object.values(S.deck).some(Boolean);
  $("scene-save-btn").disabled = !S.online || !name || !modified;
  $("scene-save-btn").title = name ? (modified ? `Overwrite ${name} with the deck` : `The deck is exactly ${name}`) : "Stage a scene first (or use Save as…)";
  $("scene-saveas-btn").disabled = !S.online || !any;
  $("scene-delete-btn").disabled = !S.online || !name;
  $("scene-delete-btn").title = name ? `Delete scene ${name}` : "";
  $("deck-clear-btn").disabled = !any && !name;
  $("stop-all-btn").disabled = !S.online || !S.runs.length;
}

function updateSendButton() {
  const n = pendingTargets().length;
  const btn = $("send-btn");
  btn.disabled = !S.online || !n;
  btn.textContent = n ? `Send ${n} ▶` : "Send ▶";
  const scene = S.deckScene && !deckModified() ? S.deckScene : null;
  btn.title = !Object.values(S.deck).some(Boolean) ? "Nothing on deck"
    : !n ? "Everything on deck is already playing"
    : scene ? `Plays scene ${scene} (${plural(n, "strip")} change)` : `Plays the deck on ${plural(n, "strip")}`;
}

// ---------- Send / Stop ----------

async function send() {
  const targets = pendingTargets();
  if (!targets.length) return;
  $("send-btn").disabled = true;
  const errors = [];
  const scene = S.deckScene && deckIsScene(S.deckScene) ? S.deckScene : null;
  if (scene) {
    // Exactly a saved scene: activate it, so lightd (and Home Assistant) see the scene as active.
    try {
      const res = await api("POST", `/scenes/${encodeURIComponent(scene)}/activate`);
      errors.push(...(res.errors || []));
    } catch (e) {
      errors.push(e.message);
    }
  } else {
    const groups = new Map();
    for (const t of targets) {
      const l = S.deck[t];
      const key = JSON.stringify([l.pattern, l.code, paramsKey(l.params, schemaFor(l.pattern)), l.brightness ?? 1]);
      if (!groups.has(key)) groups.set(key, { line: l, targets: [] });
      groups.get(key).targets.push(t);
    }
    for (const { line, targets: ts } of groups.values()) {
      const body = { targets: ts, params: line.params || {} };
      if (line.code) body.source = line.code;
      else body.pattern = line.pattern;
      try {
        const run = await api("POST", "/runs", body);
        if (line.code) S.sentCode.set(run.id, line.code);
        await api("PUT", "/brightness", { targets: ts, brightness: line.brightness ?? 1 });
      } catch (e) {
        errors.push(`${line.pattern} on ${ts.map(chName).join(", ")}: ${e.message}`);
      }
    }
  }
  if (errors.length) banner(`Not everything started: ${errors.join("; ")}`, "err");
  else banner(scene ? `Playing scene ${scene}.` : `Sent to ${plural(targets.length, "strip")}.`, "ok", 2000);
  await refreshLive();
  saveDeck();
}

// Stop just these channels (they go black); other channels of the same run keep playing.
async function stopTargets(targets) {
  try {
    await api("POST", "/stop", { targets });
  } catch (e) {
    banner(`Could not stop: ${e.message}`, "err");
  }
  await refreshLive();
}

async function stopAll() {
  if (!confirm("Stop everything? Every channel, inside and outside, goes black.")) return;
  try {
    await api("POST", "/blackout");
  } catch (e) {
    banner(`Could not stop: ${e.message}`, "err");
  }
  await refreshLive();
}

// ---------- editor modal ----------

let lastFocus = null;
function openEditor() {
  if (!S.sel) return;
  lastFocus = document.activeElement;
  $("ed-scrim").hidden = false;
  document.body.classList.add("modal-open");
  $("name-input").value = S.sel.name;
  updateSelMarkers();
  setTimeout(() => { editor.refresh(); editor.focus(); }, 0); // CodeMirror measures nothing while hidden
}

function closeEditor() {
  if (!modalOpen()) return;
  $("ed-scrim").hidden = true;
  document.body.classList.remove("modal-open");
  renderSelected();
  lastFocus?.focus?.();
}

// Reset in the editor: the code back to where the selection came from (Ctrl-Z undoes it).
function resetCode() {
  const s = S.sel;
  if (!s || !codeChanged(s)) return;
  editor.setValue(s.base.code);
}

// ---------- polling ----------

async function pollStatus() {
  const pill = $("lightd-status");
  try {
    const s = await api("GET", "/status");
    S.online = true;
    // With the switch's label gone, the pill says when output is off (boards showing their own WLED effects).
    pill.className = s.output === false ? "pill warn" : "pill ok";
    pill.textContent = `lightd · ${s.output === false ? "output off" : `${Math.round(s.actual_fps)} fps`} · ${s.controllers.online}/${s.controllers.total} online`;
    showOutput(s.output);
  } catch {
    S.online = false;
    pill.className = "pill err";
    pill.textContent = "lightd offline";
  }
}

async function refreshLive() {
  await Promise.all([loadControllers(), loadRuns(), loadScenes()]);
  renderBoard(true);
}

async function poll() {
  await pollStatus();
  await Promise.all([loadControllers(), loadRuns(), loadScenes()]);
  renderBoard();
}

// Pausing freezes t; playing again jumps back to the current time.
function togglePlay() {
  const p = S.preview;
  if (p.playing) p.pausedAt = Date.now() / 1000;
  p.playing = !p.playing;
  $("preview-play").textContent = p.playing ? "Pause" : "Play";
}

async function loadExamples() {
  let examples = [];
  try {
    examples = (await api("GET", "/examples")).examples;
  } catch { /* older lightd without examples */ }
  const select = $("example-select");
  select.hidden = !examples.length;
  select.replaceChildren(
    el("option", { value: "", disabled: true, selected: true }, "start from an example ▾"),
    ...examples.map((e) => el("option", { value: e.name }, e.name)));
  select.onchange = () => {
    const ex = examples.find((e) => e.name === select.value);
    select.value = "";
    if (ex) loadExample(ex);
  };
}

// ---------- boot ----------

async function bootstrap() {
  editor = CodeMirror($("editor"), {
    value: "",
    mode: "lua",
    lineNumbers: true,
    indentUnit: 2,
    tabSize: 2,
    matchBrackets: true,
    extraKeys: {
      "Ctrl-S": () => saveToLibrary(),
      "Cmd-S": () => saveToLibrary(),
      "Ctrl-Enter": () => loadToDeck(),
      "Cmd-Enter": () => loadToDeck(),
      Tab: (cm) => cm.replaceSelection("  "),
    },
  });
  let timer = null;
  editor.on("change", () => {
    if (settingEditor || !S.sel) return;
    S.sel.code = editor.getValue();
    updateSelMarkers();
    clearTimeout(timer);
    timer = setTimeout(compilePreview, 250);
  });

  $("new-btn").addEventListener("click", newPattern);
  $("open-editor-btn").addEventListener("click", openEditor);
  $("ask-ai-btn").addEventListener("click", () => toggleWiz());
  $("wiz").addEventListener("submit", sendWiz);
  $("wiz-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) sendWiz(e);
    if (e.key === "Escape") { e.stopPropagation(); toggleWiz(false); }
  });
  $("wiz-close").addEventListener("click", () => toggleWiz(false));
  $("save-lib-btn").addEventListener("click", saveToLibrary);
  $("save-btn").addEventListener("click", saveToLibrary);
  $("load-btn").addEventListener("click", loadToDeck);
  $("params-reset").addEventListener("click", resetParams);
  $("delete-btn").addEventListener("click", deletePattern);
  $("ed-reset-btn").addEventListener("click", resetCode);
  $("ed-done").addEventListener("click", closeEditor);
  $("ed-close").addEventListener("click", closeEditor);
  $("ed-scrim").addEventListener("mousedown", (e) => { if (e.target === e.currentTarget) closeEditor(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && modalOpen()) closeEditor(); });
  $("name-input").addEventListener("input", () => {
    if (!S.sel) return;
    S.sel.name = $("name-input").value.trim().toLowerCase() || "untitled";
    $("sel-name").textContent = S.sel.name;
    updateSelButtons();
  });
  $("send-btn").addEventListener("click", send);
  $("stop-all-btn").addEventListener("click", stopAll);
  $("scene-save-btn").addEventListener("click", saveScene);
  $("scene-saveas-btn").addEventListener("click", saveSceneAs);
  $("scene-delete-btn").addEventListener("click", deleteScene);
  $("deck-clear-btn").addEventListener("click", clearDeck);
  initOutputSwitch(api, (msg) => banner(msg, "err"));
  $("preview-play").addEventListener("click", togglePlay);
  $("library-select").addEventListener("change", async (e) => {
    if (!(await selectLibrary(e.target.value))) renderLibrary(); // cancelled: put the dropdown back
  });
  $("scene-select").addEventListener("change", (e) => {
    S.scenePick = e.target.value;
    stageScene(S.scenePick);
  });
  $("scene-play").addEventListener("click", () => { if (S.scenePick) activateScene(S.scenePick); });
  window.addEventListener("beforeunload", (e) => {
    const s = S.sel;
    if (s && codeChanged(s) && S.sources.get(s.name) !== s.code && !Object.values(S.deck).some((l) => l?.code === s.code)) e.preventDefault();
  });

  loadDeck();
  renderSelected();

  try {
    await initPreview();
  } catch (e) {
    banner(`Preview unavailable: ${e.message}`, "err");
  }
  requestAnimationFrame(drawPreview);

  initAssistant({
    getSource: () => S.sel?.code ?? "",
    getName: () => S.sel?.name ?? "",
    // Replace the working copy's code; undoable with Ctrl-Z in the editor.
    apply: (code) => {
      if (!S.sel) { setSel(makeSel({ name: "untitled", source: "new", code })); return; }
      editor.setValue(code);
      if (modalOpen()) editor.focus();
    },
    // Start a new working copy with this code (for "make me a new pattern" answers).
    applyAsNew: (code) => {
      if (!confirmDiscard()) return false;
      setSel(makeSel({ name: "untitled", source: "new", code }));
      return true;
    },
    // "" if the code compiles and renders, else the error; null if the preview runtime isn't loaded.
    check: (code) => {
      if (!S.preview.check) return null;
      const count = Math.max(1, Math.min(2000, parseInt($("preview-count").value, 10) || 150));
      return S.preview.check(code, count, Date.now() / 1000);
    },
    banner,
  }).then((enabled) => { $("ask-ai-btn").hidden = !enabled; });

  await Promise.all([loadLibrary(), poll(), loadExamples()]);
  renderScenes();
  renderBoard(true);
  setInterval(poll, POLL_MS);
  pollApiValues();
}

bootstrap();
