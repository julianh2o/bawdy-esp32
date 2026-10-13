/**
 * Lighting setup page (/config): adopted controllers with their channels, plus discovery.
 *
 * lightd owns the configuration: everything here goes through its REST API (via the web
 * server's /engine/* proxy). Edits stay in a local draft until "Save & apply", which sends
 * controllers.json to lightd (PUT /api/config); lightd validates, saves and applies it.
 * A channel is one of the board's LED outputs; its name is stored as
 * {"channels": [{"name": ...}, ...]} in output order.
 */

const API = "/engine/api";
const POLL_MS = 3000;
const STATUS_POLL_MS = 5000;
const IDENTIFY_SECONDS = 6;
const ID_RE = /^[A-Za-z0-9_.-]{1,64}$/;

const S = {
  meta: null, // last GET /api/config: {path, writable, error, controllers}
  original: {}, // controllers as loaded, for Revert
  controllers: [], // [{key, id, ip, port, channels: [{name, ...}], extra}]
  saved: "",
  live: [], // GET /api/controllers: what lightd knows about each saved controller's board
  discovery: { boards: [] },
  open: new Set(), // controller keys whose channels are expanded
  identifying: new Map(), // "ip:port" -> {timer, channel}
};
let nextKey = 1;

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
const tag = (text, kind = "", title) => el("span", { class: `tag ${kind}`, title }, text);
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

function normMac(mac) {
  return (mac || "").toLowerCase().replace(/[^0-9a-f]/g, "");
}

function fmtMac(mac) {
  const m = normMac(mac);
  return m.length === 12 ? m.match(/../g).join(":") : mac || "";
}

function suggestId(name) {
  const s = (name || "").toLowerCase().replace(/[^a-z0-9_.-]+/g, "_").replace(/^_+|_+$/g, "");
  return s || "controller";
}

// Key-order-independent JSON, so reordered-but-equal drafts don't look dirty.
function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value).sort().map((k) => `${JSON.stringify(k)}:${canonical(value[k])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function banner(message, kind = "") {
  const b = $("banner");
  if (!message) {
    b.hidden = true;
    return;
  }
  b.hidden = false;
  b.className = `banner ${kind}`;
  b.replaceChildren(...(Array.isArray(message) ? message : [message]));
}

async function api(method, path, body) {
  const resp = await fetch(API + path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.message || `${resp.status} ${resp.statusText}`);
  return data;
}

// ---------- draft <-> file ----------

function controllersFromFile(controllers) {
  return Object.entries(controllers || {}).map(([id, entry]) => {
    const { ip, port, channels, ...extra } = entry;
    return {
      key: nextKey++,
      id,
      ip: ip || "",
      port: port ?? 4048,
      channels: (channels || []).map((ch) => ({ ...ch, name: ch.name || "" })),
      extra,
    };
  });
}

function controllersToFile() {
  const out = {};
  for (const c of S.controllers) {
    const entry = { ...c.extra, ip: c.ip.trim(), port: Number(c.port) };
    // Keep channels up to the last one that has a name (or other saved keys).
    const channels = c.channels.map(({ name, ...rest }) => ({ ...rest, name: name.trim() }));
    while (channels.length && !channels[channels.length - 1].name && Object.keys(channels[channels.length - 1]).length === 1) {
      channels.pop();
    }
    if (channels.length) entry.channels = channels;
    out[c.id.trim()] = entry;
  }
  return out;
}

const isDirty = () => canonical(controllersToFile()) !== S.saved;

// ---------- board matching ----------

function nodeForController(c) {
  const nodes = S.discovery.boards || [];
  const mac = normMac(c.extra.mac);
  const host = (c.extra.host || "").toLowerCase();
  const byIdentity =
    (mac && nodes.find((n) => normMac(n.mac || n.info?.mac) === mac)) ||
    (host && nodes.find((n) => (n.hostname || "").toLowerCase() === host)) ||
    null;
  const byIp = nodes.find((n) => n.ip === c.ip.trim()) || null;
  return { node: byIdentity || byIp, moved: byIdentity && byIdentity.ip !== c.ip.trim() ? byIdentity.ip : null };
}

function controllerForNode(n) {
  const mac = normMac(n.mac || n.info?.mac);
  const host = (n.hostname || "").toLowerCase();
  return S.controllers.find(
    (c) =>
      c.ip.trim() === n.ip ||
      (mac && normMac(c.extra.mac) === mac) ||
      (host && (c.extra.host || "").toLowerCase() === host),
  );
}

// What's known about a draft controller's board: lightd's live view when the controller is saved
// with this IP, otherwise whatever discovery found. channels: [{start, length, pins}] or [].
function boardFor(c) {
  const live = S.live.find((l) => l.id === c.id.trim() && l.configured_ip === c.ip.trim() && l.port === Number(c.port));
  if (live) {
    return {
      known: live.online || !!live.board,
      online: live.online,
      error: live.error,
      ledCount: live.board?.led_count,
      channels: live.channels.filter((ch) => ch.on_board),
      moved: live.ip !== live.configured_ip ? live.ip : null,
    };
  }
  const { node, moved } = nodeForController(c);
  const info = node?.info || {};
  return {
    known: !!node,
    online: !!node?.online,
    error: node?.probe_error,
    ledCount: info.led_count,
    channels: info.channels || [],
    moved,
  };
}

// ---------- identify ----------

async function identify(ip, port, { ledCount, ledStart = 0, ranges, channel = null } = {}) {
  const key = `${ip}:${port}`;
  const current = S.identifying.get(key);
  const stopping = current && current.channel === channel;
  try {
    await api("POST", "/identify", {
      ip,
      port,
      led_count: ledCount || undefined,
      led_start: ledStart || undefined,
      ranges,
      seconds: IDENTIFY_SECONDS,
      stop: stopping || undefined,
    });
    clearTimeout(current?.timer);
    S.identifying.delete(key);
    if (!stopping) {
      const timer = setTimeout(() => { S.identifying.delete(key); renderControllers(); renderDiscovery(); }, IDENTIFY_SECONDS * 1000);
      S.identifying.set(key, { timer, channel });
    }
  } catch (e) {
    banner(`Identify failed: ${e.message}`, "err");
  }
  renderControllers();
  renderDiscovery();
}

// channel: null for the whole board, else the channel index being blinked.
function identifyButton(ip, port, opts = {}) {
  if (!ip) return null;
  const channel = opts.channel ?? null;
  const current = S.identifying.get(`${ip}:${port}`);
  const active = !!current && current.channel === channel;
  return el("button", {
    class: `btn btn-small${active ? " active" : ""}`,
    title: active ? "Stop blinking" : channel === null ? "Blink this board's LEDs so you can spot it" : "Blink only this output's LEDs",
    onclick: (e) => { e.stopPropagation(); identify(ip, Number(port) || 4048, { ...opts, channel }); },
  }, active ? "Blinking…" : "Identify");
}

// ---------- rendering: controllers ----------

function renderControllers() {
  const list = $("controllers-list");
  const typing = list.contains(document.activeElement) && document.activeElement.matches("input[type=text]");
  if (typing) return; // don't clobber a name being typed
  list.replaceChildren(
    ...(S.controllers.length
      ? S.controllers.map(renderControllerCard)
      : [el("div", { class: "empty" }, "No controllers yet. Adopt a board found on the network below, or add one by hand.")]),
  );
}

function renderControllerCard(c) {
  const board = boardFor(c);
  const { moved } = board;
  const outputs = board.channels;
  const open = S.open.has(c.key);
  const ip = c.ip.trim();

  let status;
  if (moved) status = el("span", { class: "pill warn", title: `This board now answers at ${moved}` }, `moved to ${moved}`);
  else if (board.online) status = el("span", { class: "pill ok" }, "online");
  else if (board.known) status = el("span", { class: "pill warn", title: board.error || "" }, "offline");
  else status = el("span", { class: "pill", title: board.error || "" }, "not seen");

  const count = Math.max(outputs.length, c.channels.length);
  const named = c.channels.filter((ch) => ch.name.trim()).length;
  const meta = [
    ip || "no IP",
    c.extra.name,
    board.ledCount ? `${board.ledCount} LEDs` : null,
    count ? `${plural(count, "channel")}${named ? `, ${named} named` : ""}` : null,
  ].filter(Boolean).join(" · ");

  const head = el("div", { class: "ctrl-head", onclick: () => { open ? S.open.delete(c.key) : S.open.add(c.key); renderControllers(); } },
    el("span", { class: "chev" }, "▸"),
    el("div", { style: "min-width:0" },
      el("div", { class: "ctrl-id" }, c.id),
      el("div", { class: "ctrl-meta" }, meta)),
    el("span", { class: "spacer" }),
    status,
    identifyButton(ip, c.port, { ledCount: board.ledCount }),
    el("button", { class: "btn btn-small btn-ghost", title: "Edit controller", onclick: (e) => { e.stopPropagation(); openControllerDialog({ mode: "edit", controller: c }); } }, "Edit"),
  );

  const notes = [];
  if (moved) {
    notes.push(el("div", { class: "ctrl-note" },
      tag("IP changed", "warn"), `Board found at ${moved}.`,
      el("button", { class: "btn btn-small", onclick: () => { c.ip = moved; renderAll(); } }, "Use new IP")));
  }
  if (!outputs.length) {
    notes.push(el("div", { class: "ctrl-note muted" },
      board.online ? "The board didn't report its LED outputs." : "Board not reachable yet, so its LED outputs are unknown."));
  } else if (c.channels.length > outputs.length) {
    notes.push(el("div", { class: "ctrl-note" },
      tag("board", "warn"), `The board now has ${plural(outputs.length, "output")}; ${c.channels.length - outputs.length} named channel(s) have no output.`));
  }

  const channels = [];
  for (let ch = 0; ch < count; ch++) channels.push(renderChannel(c, ch, outputs[ch], ip));

  const body = el("div", { class: "ctrl-body" },
    notes,
    channels,
    !outputs.length
      ? el("div", { class: "row", style: "margin-top:8px" },
          el("button", { class: "btn btn-small btn-ghost", onclick: () => { c.channels.push({ name: "" }); renderAll(); } }, "+ Add channel"))
      : null,
  );

  return el("div", { class: `card ctrl${open ? " open" : ""}` }, head, body);
}

function renderChannel(c, ch, output, ip) {
  while (c.channels.length <= ch) c.channels.push({ name: "" });
  const entry = c.channels[ch];
  const nameInput = el("input", {
    type: "text",
    class: "channel-label",
    value: entry.name,
    maxlength: "64",
    placeholder: "Name this channel, e.g. Front truss",
    "aria-label": `Channel ${ch + 1} name`,
    oninput: (e) => { entry.name = e.target.value; renderHeader(); },
    onkeydown: (e) => { if (e.key === "Enter") e.target.blur(); },
  });

  // Channels come from the board's segments; a Govee RGB + Govee White pair is one linked channel.
  const segName = output ? (output.segment_name ?? output.name) : null;
  const white = output?.white;
  const details = output
    ? [
        output.pins?.length ? `GPIO ${output.pins.join(",")}` : null,
        white ? `${output.length} RGB + ${white.length} white LEDs` : `${output.length} LEDs`,
        output.rgbw && !white ? "RGBW" : null,
      ].filter(Boolean).join(" · ")
    : "no output on board";
  if (segName && !white) nameInput.placeholder = `Name this channel (segment: ${segName})`;
  const linkedTag = white
    ? tag("linked", "", `Segments "${segName}" and its white segment are driven as one channel: the pattern's r, g, b go to the RGB LEDs, its w to the white LEDs`)
    : null;

  const extra = !output
    ? el("button", {
        class: "btn btn-small btn-ghost",
        title: "Forget this channel",
        onclick: () => { c.channels.splice(ch, 1); renderAll(); },
      }, "Remove")
    : identifyButton(ip, c.port, white
        ? { ranges: [[output.start, output.length], [white.start, white.length]], channel: ch }
        : { ledStart: output.start, ledCount: output.length, channel: ch });

  // Stored only when true, so untouched channels stay {"name": ""} and get trimmed on save.
  const reversed = el("label", { class: "check small muted", title: "Pixel 0 is at the far end of the strip" },
    el("input", {
      type: "checkbox",
      checked: !!entry.reversed,
      onchange: (e) => {
        if (e.target.checked) entry.reversed = true;
        else delete entry.reversed;
        renderHeader();
      },
    }),
    "Reversed");

  return el("div", { class: "channel" },
    el("div", { class: "channel-top" },
      el("span", { class: "channel-name" }, `Ch ${ch + 1}`),
      nameInput,
      linkedTag,
      el("span", { class: "channel-stat" }, details),
      reversed,
      extra));
}

// ---------- rendering: discovery ----------

function renderDiscovery() {
  const d = S.discovery;
  const unadopted = (d.boards || []).filter((n) => !controllerForNode(n));
  const summary = $("discovery-summary");
  if (!d.enabled) summary.textContent = "Discovery is off. Add boards by IP";
  else if (unadopted.length) summary.textContent = `${plural(unadopted.length, "unadopted board")} discovered`;
  else summary.textContent = "No unadopted boards found";

  const status = $("discovery-status");
  if (d.error) status.textContent = d.error;
  else if (d.enabled) status.textContent = "Browsing mDNS for WLED boards. Other subnets/VLANs need Add by IP.";
  else status.textContent = "";

  $("discovered-list").replaceChildren(
    ...(unadopted.length
      ? unadopted.map((n) => {
          const info = n.info || {};
          const outputs = info.channels || [];
          let st;
          if (!n.online && n.source === "mdns") st = tag("offline", "warn");
          else if (n.probe_error && !n.info) st = tag("no HTTP answer", "warn", n.probe_error);
          else if (!n.info) st = tag("probing…");
          return el("div", { class: "disc-item" },
            el("div", { class: "disc-info" },
              el("div", {}, info.name || n.instance || n.ip, " ", st),
              el("div", { class: "small muted" },
                [n.hostname, n.ip, info.led_count ? `${info.led_count} LEDs` : null,
                  outputs.length ? plural(outputs.length, "output") : null].filter(Boolean).join(" · "))),
            identifyButton(n.ip, 4048, { ledCount: info.led_count }),
            el("button", { class: "btn btn-small btn-primary", onclick: () => openControllerDialog({ mode: "adopt", node: n }) }, "Adopt…"));
        })
      : [el("div", { class: "muted small" }, "Every board found so far is adopted.")]),
  );
}

// ---------- rendering: header ----------

function renderHeader() {
  const dirty = isDirty();
  $("dirty-indicator").textContent = dirty ? "Unsaved changes" : "";
  $("save-btn").disabled = !dirty || !S.meta?.writable;
  $("revert-btn").disabled = !dirty;
  const path = S.meta?.path;
  $("file-paths").textContent = path ? `Controllers (managed by lightd): ${path}` : "";
}

function renderAll() {
  renderHeader();
  renderControllers();
  renderDiscovery();
}

// ---------- controller dialog (adopt / add / edit) ----------

const cdlg = { mode: "adopt", node: null, controller: null };

function openControllerDialog({ mode, node = null, controller = null }) {
  Object.assign(cdlg, { mode, node, controller });
  const info = node?.info || {};
  if (mode === "edit") {
    $("c-title").textContent = `Edit ${controller.id}`;
    $("c-summary").textContent = [controller.extra.name, controller.extra.host, fmtMac(controller.extra.mac)].filter(Boolean).join(" · ");
    $("c-id").value = controller.id;
    $("c-ip").value = controller.ip;
    $("c-port").value = controller.port;
    $("c-ok").textContent = "Done";
    $("c-remove").hidden = false;
  } else {
    $("c-title").textContent = mode === "adopt" ? "Adopt board" : "Add controller";
    $("c-summary").textContent = node
      ? [info.name || node.instance, node.hostname, node.ip, info.led_count ? `${info.led_count} LEDs` : null].filter(Boolean).join(" · ")
      : "For a board discovery can't see.";
    $("c-id").value = suggestNewId(info.name || node?.instance);
    $("c-ip").value = node?.ip || "";
    $("c-port").value = 4048;
    $("c-ok").textContent = mode === "adopt" ? "Adopt" : "Add";
    $("c-remove").hidden = true;
  }
  $("c-errors").replaceChildren();
  $("controller-dialog").showModal();
}

function suggestNewId(name) {
  const taken = new Set(S.controllers.map((c) => c.id.trim()));
  if (name && !/^wled$/i.test(name) && !taken.has(suggestId(name))) return suggestId(name);
  for (let i = 1; i < 1000; i++) {
    const id = `wled_${String(i).padStart(2, "0")}`;
    if (!taken.has(id)) return id;
  }
  return suggestId(name);
}

function initControllerDialog() {
  $("c-cancel").addEventListener("click", () => $("controller-dialog").close());
  $("c-remove").addEventListener("click", () => {
    const c = cdlg.controller;
    if (!confirm(`Remove ${c.id}? Its channel names are removed too.`)) return;
    S.controllers = S.controllers.filter((x) => x !== c);
    $("controller-dialog").close();
    renderAll();
  });
  $("controller-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const id = $("c-id").value.trim();
    const ip = $("c-ip").value.trim();
    const port = parseInt($("c-port").value, 10);
    const errs = [];
    if (!ID_RE.test(id)) errs.push("ID can use letters, digits, '_', '-' and '.'");
    if (S.controllers.some((c) => c !== cdlg.controller && c.id.trim() === id)) errs.push(`"${id}" is already a controller`);
    if (!ip) errs.push("IP address is required");
    if (!(port >= 1 && port <= 65535)) errs.push("Port must be 1–65535");
    if (errs.length) {
      $("c-errors").replaceChildren(...errs.map((m) => el("li", {}, m)));
      return;
    }

    if (cdlg.mode === "edit") {
      Object.assign(cdlg.controller, { id, ip, port });
    } else {
      const node = cdlg.node;
      const info = node?.info || {};
      const extra = {};
      if (info.name || node?.instance) extra.name = info.name || node.instance;
      if (node?.mac || info.mac) extra.mac = fmtMac(node.mac || info.mac);
      if (node?.hostname) extra.host = node.hostname;
      const c = { key: nextKey++, id, ip, port, channels: [], extra };
      S.controllers.push(c);
      S.open.add(c.key); // show its channels straight away, ready for naming
    }
    $("controller-dialog").close();
    renderAll();
  });
}

// ---------- load / save / polling ----------

function showConfigProblems(meta) {
  if (meta.error) banner(`lightd can't read ${meta.path}: ${meta.error}`, "err");
  else if (!meta.writable) {
    banner([
      "lightd can't write ", el("code", {}, meta.path),
      ". Under Docker it runs as uid 10001: make the config directory writable, e.g. ",
      el("code", {}, "sudo chown -R 10001 config"), ".",
    ], "err");
  }
}

async function load() {
  const meta = await api("GET", "/config");
  S.meta = meta;
  S.original = meta.controllers || {};
  resetDraft();
  showConfigProblems(meta);
}

function resetDraft() {
  S.controllers = controllersFromFile(S.original);
  S.saved = canonical(controllersToFile());
  S.open = new Set(S.controllers.map((c) => c.key));
}

async function save() {
  $("save-btn").disabled = true;
  try {
    const res = await api("PUT", "/config", { controllers: controllersToFile() });
    S.meta = res;
    S.original = res.controllers;
    const openIds = new Set(S.controllers.filter((c) => S.open.has(c.key)).map((c) => c.id.trim()));
    resetDraft();
    S.open = new Set(S.controllers.filter((c) => openIds.has(c.id)).map((c) => c.key));
    banner("Saved and applied.", "ok");
    await pollLive();
    renderAll();
    pollStatus();
  } catch (e) {
    banner(`Not saved: ${e.message}`, "err");
    renderHeader();
  }
}

async function pollLive() {
  try {
    S.live = (await api("GET", "/controllers")).controllers;
  } catch {
    S.live = [];
  }
}

async function pollDiscovery() {
  try {
    S.discovery = await api("GET", "/discovered");
  } catch (e) {
    S.discovery = { ...S.discovery, error: `lightd unreachable: ${e.message}` };
  }
}

async function poll() {
  await Promise.all([pollLive(), pollDiscovery()]);
  renderControllers();
  renderDiscovery();
}

async function pollStatus() {
  const pill = $("daemon-status");
  try {
    const s = await api("GET", "/status");
    pill.className = "pill ok";
    pill.textContent = `lightd · ${s.controllers.online}/${s.controllers.total} online`;
    pill.title = `${Math.round(s.actual_fps)} fps, ${plural(s.runs, "pattern")} running`;
  } catch {
    pill.className = "pill err";
    pill.textContent = "lightd offline";
    pill.title = "The configuration can't be loaded or saved until lightd is running";
  }
}

async function bootstrap() {
  initControllerDialog();
  $("save-btn").addEventListener("click", save);
  $("revert-btn").addEventListener("click", () => {
    resetDraft();
    banner(null);
    renderAll();
  });
  $("add-controller-btn").addEventListener("click", () => openControllerDialog({ mode: "add" }));
  $("rescan-btn").addEventListener("click", async () => {
    try {
      await api("POST", "/discovered/rescan");
      await poll();
    } catch (e) {
      banner(`Rescan failed: ${e.message}`, "err");
    }
  });
  $("probe-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const ip = $("probe-ip").value.trim();
    if (!ip) return;
    try {
      await api("POST", "/discovered/probe", { ip });
      $("probe-ip").value = "";
      banner(null);
      await poll();
    } catch (err) {
      banner(err.message, "err");
    }
  });
  // Re-render once focus leaves a channel name (re-rendering is skipped while typing).
  $("controllers-list").addEventListener("focusout", (e) => setTimeout(() => {
    if (e.target.matches("input[type=text]") && document.activeElement !== e.target) renderAll();
  }));
  window.addEventListener("beforeunload", (e) => {
    if (S.meta && isDirty()) e.preventDefault();
  });

  pollStatus();
  setInterval(pollStatus, STATUS_POLL_MS);
  try {
    await load();
  } catch (e) {
    banner(`Could not load the configuration from lightd: ${e.message}`, "err");
    return;
  }
  renderAll();
  await poll();
  setInterval(poll, POLL_MS);
}

document.addEventListener("DOMContentLoaded", bootstrap);
