/**
 * Lighting setup page (/config): adopted controllers with their channels, plus discovery.
 *
 * Edits stay in a local draft until "Save & apply", which writes controllers.json and has the
 * daemon reload it. A channel is one of the board's LED outputs; its name is stored in
 * controllers.json as {"channels": [{"name": ...}, ...]} in output order.
 */

const POLL_MS = 3000;
const DAEMON_POLL_MS = 5000;
const IDENTIFY_SECONDS = 6;
const ID_RE = /^[A-Za-z0-9_.-]{1,64}$/;

const S = {
  meta: null, // last GET /api/config (paths, writable flags, errors)
  original: {}, // controllers as loaded, for Revert
  controllers: [], // [{key, id, ip, port, channels: [{name, ...}], extra}]
  saved: "",
  discovery: { nodes: [] },
  open: new Set(), // controller keys whose channels are expanded
  identifying: new Map(), // "ip:port" -> {timer, channel}
  probed: new Set(),
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

async function api(method, url, body) {
  const resp = await fetch(url, {
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
  const nodes = S.discovery.nodes || [];
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

// ---------- identify ----------

async function identify(ip, port, { ledCount, ledStart = 0, channel = null } = {}) {
  const key = `${ip}:${port}`;
  const current = S.identifying.get(key);
  const stopping = current && current.channel === channel;
  try {
    const res = await api("POST", "/api/config/identify", {
      ip,
      port,
      led_count: ledCount || undefined,
      led_start: ledStart || undefined,
      seconds: IDENTIFY_SECONDS,
      stop: stopping || undefined,
    });
    clearTimeout(current?.timer);
    S.identifying.delete(key);
    if (!stopping) {
      const timer = setTimeout(() => { S.identifying.delete(key); renderControllers(); renderDiscovery(); }, IDENTIFY_SECONDS * 1000);
      S.identifying.set(key, { timer, channel });
      if (res.via === "direct") banner(`Daemon not reachable, so the web server is blinking ${ip} directly.`);
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
  if (list.contains(document.activeElement)) return; // don't clobber a field being typed in
  list.replaceChildren(
    ...(S.controllers.length
      ? S.controllers.map(renderControllerCard)
      : [el("div", { class: "empty" }, "No controllers yet. Adopt a board found on the network below, or add one by hand.")]),
  );
}

function renderControllerCard(c) {
  const { node, moved } = nodeForController(c);
  const info = node?.info || {};
  const outputs = info.outputs || [];
  const open = S.open.has(c.key);
  const ip = c.ip.trim();

  let status;
  if (moved) status = el("span", { class: "pill warn", title: `This board now answers at ${moved}` }, `moved to ${moved}`);
  else if (node?.online) status = el("span", { class: "pill ok" }, "online");
  else if (node) status = el("span", { class: "pill warn" }, "offline");
  else status = el("span", { class: "pill" }, "not seen");

  const count = Math.max(outputs.length, c.channels.length);
  const named = c.channels.filter((ch) => ch.name.trim()).length;
  const meta = [
    ip || "no IP",
    c.extra.name,
    info.led_count ? `${info.led_count} LEDs` : null,
    count ? `${plural(count, "channel")}${named ? `, ${named} named` : ""}` : null,
  ].filter(Boolean).join(" · ");

  const head = el("div", { class: "ctrl-head", onclick: () => { open ? S.open.delete(c.key) : S.open.add(c.key); renderControllers(); } },
    el("span", { class: "chev" }, "▸"),
    el("div", { style: "min-width:0" },
      el("div", { class: "ctrl-id" }, c.id),
      el("div", { class: "ctrl-meta" }, meta)),
    el("span", { class: "spacer" }),
    status,
    identifyButton(ip, c.port, { ledCount: info.led_count }),
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
      node ? "The board didn't report its LED outputs." : "Board not seen yet, so its LED outputs are unknown."));
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

  const details = output
    ? [output.pins?.length ? `GPIO ${output.pins.join(",")}` : null, `${output.len} LEDs`].filter(Boolean).join(" · ")
    : "no output on board";

  const extra = !output
    ? el("button", {
        class: "btn btn-small btn-ghost",
        title: "Forget this channel",
        onclick: () => { c.channels.splice(ch, 1); renderAll(); },
      }, "Remove")
    : identifyButton(ip, c.port, { ledStart: output.start, ledCount: output.len, channel: ch });

  return el("div", { class: "channel" },
    el("div", { class: "channel-top" },
      el("span", { class: "channel-name" }, `Ch ${ch + 1}`),
      nameInput,
      el("span", { class: "channel-stat" }, details),
      extra));
}

// ---------- rendering: discovery ----------

function renderDiscovery() {
  const d = S.discovery;
  const unadopted = (d.nodes || []).filter((n) => !controllerForNode(n));
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
          const outputs = info.outputs || [];
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
  $("save-btn").disabled = !dirty || !S.meta?.controllers?.writable;
  $("revert-btn").disabled = !dirty;
  const path = S.meta?.controllers?.path;
  $("file-paths").textContent = path ? `Controllers: ${path}` : "";
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

async function load() {
  const meta = await api("GET", "/api/config");
  S.meta = meta;
  S.original = meta.controllers.controllers || {};
  resetDraft();
  if (meta.controllers.error) banner(meta.controllers.error, "err");
  else if (meta.controllers.path && !meta.controllers.writable) {
    banner([
      "This server can't write ", el("code", {}, meta.controllers.path),
      ". Under Docker the web container runs as uid 10001: make it writable, e.g. ",
      el("code", {}, "chmod a+w config/controllers.json"), ".",
    ], "err");
  }
}

function resetDraft() {
  S.controllers = controllersFromFile(S.original);
  S.saved = canonical(controllersToFile());
  S.open = new Set(S.controllers.map((c) => c.key));
}

async function save() {
  $("save-btn").disabled = true;
  try {
    const res = await api("PUT", "/api/config", { controllers: controllersToFile() });
    if (res.controllers) S.original = res.controllers;
    const openIds = new Set(S.controllers.filter((c) => S.open.has(c.key)).map((c) => c.id.trim()));
    resetDraft();
    S.open = new Set(S.controllers.filter((c) => openIds.has(c.id)).map((c) => c.key));
    renderAll();
    const reload = res.reload || {};
    if (reload.ok) {
      banner("Saved and applied.", "ok");
    } else {
      banner([
        `Saved, but the daemon didn't apply it: ${reload.message || "unknown error"}. It will use the file on its next start (`,
        el("code", {}, "docker compose restart daemon"), ").",
      ], "err");
    }
    pollDaemon();
  } catch (e) {
    banner(`Save failed: ${e.message}`, "err");
    renderHeader();
  }
}

async function pollDiscovery() {
  try {
    S.discovery = await api("GET", "/api/config/discovered");
  } catch (e) {
    S.discovery = { ...S.discovery, error: `Server unreachable: ${e.message}` };
  }
  // Adopted boards discovery can't see (other subnet): ask them directly once, for their outputs.
  for (const c of S.controllers) {
    const ip = c.ip.trim();
    if (ip && !S.probed.has(ip) && !(S.discovery.nodes || []).some((n) => n.ip === ip)) {
      S.probed.add(ip);
      api("POST", "/api/config/probe", { ip }).catch(() => {});
    }
  }
  renderControllers();
  renderDiscovery();
}

async function pollDaemon() {
  const pill = $("daemon-status");
  try {
    const res = await api("GET", "/engine/api/config");
    const n = Object.keys(res.controllers || {}).length;
    pill.className = "pill ok";
    pill.textContent = `Daemon live · ${plural(n, "controller")}`;
    pill.title = "";
  } catch {
    pill.className = "pill err";
    pill.textContent = "Daemon offline";
    pill.title = "Saved changes apply when the daemon next starts";
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
      S.discovery = await api("POST", "/api/config/rescan");
      renderDiscovery();
    } catch (e) {
      banner(`Rescan failed: ${e.message}`, "err");
    }
  });
  $("probe-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const ip = $("probe-ip").value.trim();
    if (!ip) return;
    try {
      await api("POST", "/api/config/probe", { ip });
      $("probe-ip").value = "";
      banner(null);
      await pollDiscovery();
    } catch (err) {
      banner(err.message, "err");
    }
  });
  // Re-render once focus leaves a channel name (re-rendering is skipped while typing).
  $("controllers-list").addEventListener("focusout", () => setTimeout(() => {
    if (!$("controllers-list").contains(document.activeElement)) renderAll();
  }));
  window.addEventListener("beforeunload", (e) => {
    if (S.meta && isDirty()) e.preventDefault();
  });

  try {
    await load();
  } catch (e) {
    banner(`Could not load the configuration: ${e.message}`, "err");
    return;
  }
  renderAll();
  await pollDiscovery();
  pollDaemon();
  setInterval(pollDiscovery, POLL_MS);
  setInterval(pollDaemon, DAEMON_POLL_MS);
}

document.addEventListener("DOMContentLoaded", bootstrap);
