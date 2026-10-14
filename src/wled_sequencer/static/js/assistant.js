/**
 * Pattern assistant: the chat panel under the code editor. Describe a pattern or a change, and an LLM (configured on
 * the web server, see assistant.py) writes the Lua. Every code block in a reply is test-rendered with the preview's
 * Lua runtime; if the final one fails, the error goes back to the model for a fix (up to MAX_REPAIRS times).
 *
 * The web server streams the reply from /api/assistant/chat as server-sent events.
 */

const MAX_REPAIRS = 2;

let host = null; // editor hooks from patterns.js: getSource, getName, apply, applyAsNew, check, banner
const chat = {
  messages: [], // {role: "user"|"assistant", content, note?: true}; notes are shown but not sent
  busy: false,
  abort: null,
};

const $ = (id) => document.getElementById(id);

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false || c === "") continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

// ---------- reply parsing ----------

const stripThinking = (text) => text.replace(/<think>[\s\S]*?(<\/think>|$)/g, "").trim();

// Split a reply into prose and fenced code parts. An unclosed fence (still streaming) counts as code.
function parseReply(text) {
  const parts = [];
  const re = /```([\w-]*)[^\n]*\n([\s\S]*?)(```|$)/g;
  let last = 0;
  let m;
  while ((m = re.exec(text))) {
    if (m.index > last) parts.push({ kind: "text", text: text.slice(last, m.index) });
    parts.push({ kind: "code", lang: m[1].toLowerCase(), code: m[2].replace(/\s+$/, "") + "\n", closed: m[3] === "```" });
    last = re.lastIndex;
    if (m[3] !== "```") break;
  }
  if (last < text.length) parts.push({ kind: "text", text: text.slice(last) });
  return parts;
}

const isLua = (part) => part.kind === "code" && (part.lang === "lua" || (!part.lang && /function\s+pixel\s*\(/.test(part.code)));

// The code to use from a reply: its last Lua block.
function replyCode(text) {
  const blocks = parseReply(stripThinking(text)).filter((p) => isLua(p) && p.closed);
  return blocks.length ? blocks[blocks.length - 1].code : null;
}

// Light formatting for prose: paragraphs, `code`, **bold**. Built as DOM nodes (no innerHTML from the model).
function prose(text) {
  const out = [];
  for (const para of text.trim().split(/\n{2,}/)) {
    if (!para.trim()) continue;
    const p = el("p");
    for (const tok of para.split(/(`[^`\n]+`|\*\*[^*\n]+\*\*)/)) {
      if (tok.startsWith("`") && tok.endsWith("`") && tok.length > 2) p.append(el("code", {}, tok.slice(1, -1)));
      else if (tok.startsWith("**") && tok.endsWith("**") && tok.length > 4) p.append(el("strong", {}, tok.slice(2, -2)));
      else p.append(document.createTextNode(tok));
    }
    out.push(p);
  }
  return out;
}

// ---------- rendering ----------

const checks = new Map(); // code -> check result, so re-rendering the log doesn't re-run every block

function check(code) {
  if (!checks.has(code)) {
    const result = host.check(code);
    if (result === null) return null; // runtime not ready: try again next time
    checks.set(code, result);
  }
  return checks.get(code);
}

function checkLabel(result) {
  if (result === null) return el("span", { class: "tag" }, "not checked");
  if (result === "") return el("span", { class: "tag ok", title: "Compiles and renders with the default params" }, "works");
  return el("span", { class: "tag err", title: result }, "error");
}

function codeBlock(part, { final, streaming }) {
  const wrap = el("div", { class: "pe-chat-code" });
  wrap.append(el("pre", {}, el("code", {}, part.code)));
  if (!isLua(part) || !part.closed || streaming) return wrap;

  const result = check(part.code);
  const actions = el("div", { class: "row small" }, checkLabel(result));
  if (result) actions.append(el("span", { class: "err pe-chat-err" }, result));
  actions.append(el("span", { class: "spacer" }));
  if (final) {
    actions.append(
      el("button", {
        class: "btn btn-small btn-ghost", type: "button",
        title: "Start a new, unsaved pattern with this code",
        onclick: () => host.applyAsNew(part.code),
      }, "Open as new"),
      el("button", {
        class: "btn btn-small", type: "button",
        title: `Replace the editor's code${host.getName() ? ` (${host.getName()})` : ""}; Ctrl-Z undoes it`,
        onclick: () => host.apply(part.code),
      }, "Apply to editor"));
  }
  wrap.append(actions);
  return wrap;
}

function messageNode(m, { streaming = false } = {}) {
  if (m.note) return el("div", { class: `pe-chat-note${m.error ? " err" : ""}` }, m.content);
  if (m.role === "user") {
    return el("div", { class: `pe-chat-msg user${m.auto ? " auto" : ""}` },
      el("div", { class: "pe-chat-bubble", title: m.auto ? "Sent automatically because the code didn't work" : "" },
        m.auto ? el("span", { class: "tag" }, "auto-fix") : null, m.content));
  }

  const visible = stripThinking(m.content);
  const body = el("div", { class: "pe-chat-bubble" });
  if (!visible && streaming) body.append(el("span", { class: "muted" }, m.status || "…"));
  const parts = parseReply(visible);
  const lastLua = parts.map((p, i) => (isLua(p) ? i : -1)).filter((i) => i >= 0).pop();
  parts.forEach((part, i) => {
    if (part.kind === "text") body.append(...prose(part.text));
    else body.append(codeBlock(part, { final: i === lastLua, streaming }));
  });
  if (streaming && visible && m.status) body.append(el("div", { class: "muted small" }, m.status));
  return el("div", { class: "pe-chat-msg assistant" }, body);
}

function render(streamingMsg = null) {
  const log = $("assistant-log");
  const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
  const nodes = chat.messages.map((m) => messageNode(m, { streaming: m === streamingMsg }));
  if (!nodes.length) {
    nodes.push(el("div", { class: "muted small pe-chat-empty" },
      "Ask for a new pattern (“slow ocean waves in teal and purple”) or a change to the code above (“make it twice as fast and add a color param”)."));
  }
  log.replaceChildren(...nodes);
  if (atBottom || !streamingMsg) log.scrollTop = log.scrollHeight;
  $("assistant-send").textContent = chat.busy ? "Stop" : "Send";
  $("assistant-send").classList.toggle("btn-primary", !chat.busy);
}

// ---------- talking to the server ----------

async function streamReply(reply) {
  const history = chat.messages.filter((m) => !m.note && m !== reply).map(({ role, content }) => ({ role, content }));
  chat.abort = new AbortController();
  const resp = await fetch("/api/assistant/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ messages: history, source: host.getSource(), name: host.getName() }),
    signal: chat.abort.signal,
  });
  if (!resp.ok) {
    let msg = `${resp.status} ${resp.statusText}`;
    try { msg = (await resp.json()).message || msg; } catch { /* not JSON */ }
    throw new Error(msg);
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  let thinking = 0;
  let lastPaint = 0;
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const line = buf.slice(0, i).trim();
      buf = buf.slice(i + 2);
      if (!line.startsWith("data:")) continue;
      const ev = JSON.parse(line.slice(5));
      if (ev.type === "text") { reply.content += ev.text; reply.status = ""; }
      else if (ev.type === "reasoning") { thinking += ev.text.length; reply.status = `Thinking… (${thinking} chars)`; }
      else if (ev.type === "status") reply.status = ev.text;
      else if (ev.type === "error") throw new Error(ev.text);
    }
    const now = performance.now();
    if (now - lastPaint > 80) { lastPaint = now; render(reply); }
  }
}

async function ask(text, { repairs = 0, auto = false } = {}) {
  chat.messages.push({ role: "user", content: text, auto });
  const reply = { role: "assistant", content: "", status: "Sending…" };
  chat.messages.push(reply);
  chat.busy = true;
  render(reply);

  let failed = null;
  try {
    await streamReply(reply);
  } catch (e) {
    failed = e.name === "AbortError" ? "Stopped." : e.message;
  }
  chat.busy = false;
  chat.abort = null;
  reply.status = "";
  if (!stripThinking(reply.content)) chat.messages.pop(); // nothing came back
  if (failed) chat.messages.push({ role: "assistant", note: true, error: failed !== "Stopped.", content: failed });
  render();
  if (failed) return;

  // Test the code it sent; on failure, hand the error back for a fix.
  const code = replyCode(reply.content);
  if (!code) return;
  const result = check(code);
  if (result === "" && $("assistant-autoapply").checked) host.apply(code);
  if (result && repairs < MAX_REPAIRS) {
    await ask(`That code fails when I run it:\n\n${result}\n\nFix it and send the complete pattern again.`,
      { repairs: repairs + 1, auto: true });
  }
}

function send(e) {
  e?.preventDefault();
  if (chat.busy) { chat.abort?.abort(); return; }
  const input = $("assistant-input");
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  ask(text);
}

// ---------- setup ----------

export async function initAssistant(hooks) {
  host = hooks;
  let info;
  try {
    info = await (await fetch("/api/assistant")).json();
  } catch {
    return; // older server without the assistant
  }
  if (!info.enabled) return;
  $("assistant").hidden = false;
  $("assistant-model").textContent = `${info.model}${info.provider ? ` · ${info.provider}` : ""}`;
  $("assistant-form").addEventListener("submit", send);
  $("assistant-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) send(e);
  });
  $("assistant-clear").addEventListener("click", () => {
    chat.abort?.abort();
    chat.messages = [];
    render();
  });
  try {
    $("assistant-autoapply").checked = localStorage.getItem("lightd.assistant.autoapply") === "1";
  } catch { /* storage unavailable */ }
  $("assistant-autoapply").addEventListener("change", (e) => {
    try { localStorage.setItem("lightd.assistant.autoapply", e.target.checked ? "1" : "0"); } catch { /* ignore */ }
  });
  render();
}
