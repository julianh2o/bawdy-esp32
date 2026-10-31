/**
 * Dashboard (/): per-area brightness sliders (live while dragging), long-press an area name for its scenes, and
 * whole-house scene buttons. Talks only to /api/dashboard on this server, which holds the Home Assistant token.
 */
(() => {
  "use strict";

  const API = "/api/dashboard";
  const LONG_PRESS_MS = 450;
  const SEND_EVERY_MS = 150;   // live updates while dragging, at most this often per area
  const POLL_MS = 5000;
  const QUIET_AFTER_TOUCH_MS = 4000;  // don't let polling yank a slider the user just moved
  const SETTLE_MS = 1500;  // after letting go, read the real state once HA has settled

  const $ = (id) => document.getElementById(id);
  const areas = new Map();  // id -> { el, slider, value, name, toggle, lastTouched, dragging, sender, settleTimer }
  const chips = new Map();  // house scene id -> chip button
  let activeHouseScene = null;
  let areaScenes = [];
  let toastTimer = 0;
  // ---------- music (telemeter on demeter, proxied by this server) ----------
  const BUSY_TEXT = { connecting_speaker: "Connecting to the Polk…", switching_output: "Switching audio…",
    launching_spotify: "Opening Spotify…", starting_playback: "Starting…" };
  const ICON_PLAY = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 4v16l13-8z"/></svg>';
  const ICON_PAUSE = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 4h4v16H6zM14 4h4v16h-4z"/></svg>';
  const ICON_WAIT = '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="6" cy="12" r="2"/><circle cx="12" cy="12" r="2"/><circle cx="18" cy="12" r="2"/></svg>';
  function setPlayIcon(state) {  // "play" | "pause" | "wait"
    const btn = $("music-play");
    btn.innerHTML = state === "pause" ? ICON_PAUSE : state === "wait" ? ICON_WAIT : ICON_PLAY;
    btn.setAttribute("aria-label", state === "pause" ? "Pause" : state === "wait" ? "Starting music" : "Play");
  }
  const music = { enabled: false, playing: false, pending: false, onScreen: true, volTouched: 0, volDragging: false,
    volSender: 0, volQueued: null, fastPoll: 0 };

  function renderMusic(s) {
    music.onScreen = !!s.on_screen;
    music.playing = s.state === "playing";
    $("music-title").textContent = s.track || "Nothing playing";
    $("music-artist").textContent = s.artist || "";
    const img = $("music-art");
    if (s.artwork_url) { if (img.getAttribute("src") !== s.artwork_url) img.src = s.artwork_url; img.hidden = false; $("music-art-ph").hidden = true; }
    else { img.hidden = true; img.removeAttribute("src"); $("music-art-ph").hidden = false; }
    const play = $("music-play");
    if (!music.pending) {
      setPlayIcon(music.playing ? "pause" : "play");
      play.classList.remove("busy");
    }
    const vol = $("music-volume");
    const quiet = music.volDragging || Date.now() - music.volTouched < QUIET_AFTER_TOUCH_MS;
    if (!quiet && typeof s.volume === "number") vol.value = s.volume;
    for (const id of ["music-prev", "music-play", "music-next", "music-volume"]) $(id).disabled = !music.onScreen;
    if (music.pending) $("music-play").disabled = true;
    const text = !music.onScreen ? "The shop account isn't on screen on demeter." : (s.busy ? (BUSY_TEXT[s.busy] || "Working…") : "");
    if (text && text !== music.lastStatus) toast(text, true);
    music.lastStatus = text;
    renderJam(s);
  }

  function renderJam(s) {
    const button = $("music-jam");
    const jam = s.jam || {};
    music.jamUrl = jam.url || "";
    music.jamStatus = jam.extension_online ? "Starting the Jam…" : "Jam unavailable (Spotify extension offline)";
    button.classList.toggle("dimmed", !music.jamUrl);
    button.setAttribute("aria-label", music.jamUrl ? "Open Spotify Jam (hold to copy link)" : "Spotify Jam unavailable");
  }

  async function copyText(text) {
    try { await navigator.clipboard.writeText(text); toast("Link copied"); }
    catch (_) {
      const ta = document.createElement("textarea"); ta.value = text; document.body.appendChild(ta); ta.select();
      try { document.execCommand("copy"); toast("Link copied"); } catch (e) { toast("Couldn't copy the link", true); }
      ta.remove();
    }
  }

  async function musicRefresh(force = false) {
    if (!music.enabled || (document.hidden && !force)) return;
    try { renderMusic(await call("GET", "/music")); }
    catch (_) { /* keep the dashboard usable during a transient poll failure */ }
  }

  async function musicAction(action) {
    try { renderMusic(await call("POST", `/music/${action}`, {})); }
    catch (e) { toast(e.message, true); }
  }

  async function musicPlayPause() {
    if (music.pending) return;
    if (music.playing) {  // optimistic pause
      music.playing = false; setPlayIcon("play");
      return musicAction("pause");
    }
    music.pending = true;
    const btn = $("music-play"); btn.classList.add("busy"); btn.disabled = true; setPlayIcon("wait");
    music.fastPoll = setInterval(() => musicRefresh(true), 1000);  // live step text while play runs (~30 s max)
    try { renderMusic(await call("POST", "/music/play", {})); }
    catch (e) { toast(e.message, true); }
    finally {
      clearInterval(music.fastPoll); music.pending = false;
      btn.disabled = !music.onScreen; btn.classList.remove("busy");
      setPlayIcon(music.playing ? "pause" : "play");
      musicRefresh(true);
    }
  }

  function sendVolume(value) {  // at most one PUT per SEND_EVERY_MS while dragging; the last value always goes out
    music.volQueued = value;
    if (music.volSender) return;
    const flush = () => {
      if (music.volQueued === null) { music.volSender = 0; return; }
      const v = music.volQueued; music.volQueued = null;
      call("PUT", "/music/volume", { volume: v }).catch((x) => toast(x.message, true));
      music.volSender = setTimeout(flush, SEND_EVERY_MS);
    };
    flush();
  }

  function setupMusic() {
    $("music-play").addEventListener("click", musicPlayPause);
    $("music-prev").addEventListener("click", () => musicAction("previous"));
    $("music-next").addEventListener("click", () => musicAction("next"));
    attachLongPress($("music-jam"), () => {
      if (music.jamUrl) copyText(music.jamUrl);
    }, () => {
      if (music.jamUrl) window.open(music.jamUrl, "_blank", "noopener");
      else toast(music.jamStatus);
    });
    const vol = $("music-volume");
    vol.addEventListener("pointerdown", () => { music.volDragging = true; music.volTouched = Date.now(); });
    const release = () => { music.volDragging = false; music.volTouched = Date.now(); };
    vol.addEventListener("pointerup", release); vol.addEventListener("pointercancel", release); vol.addEventListener("change", release);
    vol.addEventListener("input", () => { music.volTouched = Date.now(); sendVolume(Math.round(Number(vol.value))); });
    document.addEventListener("visibilitychange", () => { if (!document.hidden) musicRefresh(); });
    setInterval(musicRefresh, POLL_MS);
    musicRefresh();
  }

  // ---------- helpers ----------

  async function call(method, path, body) {
    const opts = { method, headers: {} };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const resp = await fetch(API + path, opts);
    let data = null;
    try { data = await resp.json(); } catch (_) { /* empty body */ }
    if (!resp.ok) throw new Error((data && data.message) || `HTTP ${resp.status}`);
    if (data && data.warnings && data.warnings.length) toast(data.warnings[0], true);
    return data;
  }

  function toast(msg, isError = false) {
    const t = $("toast");
    t.textContent = msg;
    t.classList.toggle("error", isError);
    t.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.remove("show"), isError ? 4500 : 1800);
  }

  function banner(msg) {
    const b = $("banner");
    b.hidden = !msg;
    b.textContent = msg || "";
  }

  function buzz() {
    if (navigator.vibrate) navigator.vibrate(12);
  }

  function showValue(area, pct) {
    area.slider.style.setProperty("--pct", `${pct}%`);
    area.value.textContent = area.toggleOnly ? (pct > 0 ? "On" : "Off") : (pct > 0 ? `${pct}%` : "Off");
    area.value.classList.toggle("on", pct > 0);
    area.toggle.textContent = pct > 0 ? "Turn off" : "Turn on";
    area.toggle.setAttribute("aria-pressed", String(pct > 0));
  }

  /** Sends the latest value at most every SEND_EVERY_MS, never two requests in flight, and always the final value. */
  function makeSender(id) {
    let pending = null, inFlight = false, last = 0, timer = 0;
    async function flush() {
      timer = 0;
      if (inFlight || pending === null) return;
      const wait = SEND_EVERY_MS - (Date.now() - last);
      if (wait > 0) { timer = setTimeout(flush, wait); return; }
      const value = pending;
      pending = null;
      inFlight = true;
      last = Date.now();
      try {
        await call("PUT", `/areas/${encodeURIComponent(id)}/brightness`, { brightness: value });
      } catch (e) {
        toast(e.message, true);
      } finally {
        inFlight = false;
        if (pending !== null && !timer) flush();
      }
    }
    const send = (value) => {
      pending = value;
      if (!timer) flush();
    };
    send.busy = () => inFlight || pending !== null;
    return send;
  }

  /** Once the user lets go (and the last value is sent), wait for HA to settle, then show the real level. */
  function scheduleSettle(area) {
    clearTimeout(area.settleTimer);
    area.settleTimer = setTimeout(() => {
      if (area.dragging) return;
      if (area.sender.busy()) { scheduleSettle(area); return; }
      area.lastTouched = 0;
      refresh();
    }, SETTLE_MS);
  }

  // ---------- areas ----------

  function buildArea(info) {
    const el = document.createElement("div");
    el.className = "area";
    el.innerHTML = `
      <div class="area-head">
        <button type="button" class="area-name"><span class="label"></span><span class="scene-dot" hidden></span></button>
        <span class="area-value">–</span>
      </div>
      <input type="range" min="0" max="100" step="1" value="0">
      <button type="button" class="area-toggle" hidden>Turn on</button>`;
    const name = el.querySelector(".area-name");
    const slider = el.querySelector("input");
    const value = el.querySelector(".area-value");
    const toggle = el.querySelector(".area-toggle");
    name.querySelector(".label").textContent = info.name;
    slider.setAttribute("aria-label", `${info.name} brightness`);

    const area = { el, slider, value, name, toggle, toggleOnly: false, lastTouched: 0, dragging: false,
                   sender: makeSender(info.id), settleTimer: 0 };
    areas.set(info.id, area);

    slider.addEventListener("input", () => {
      const pct = Number(slider.value);
      area.lastTouched = Date.now();
      clearTimeout(area.settleTimer);
      showValue(area, pct);
      area.sender(pct);
    });
    slider.addEventListener("pointerdown", () => { area.dragging = true; clearTimeout(area.settleTimer); });
    const release = () => { area.dragging = false; area.lastTouched = Date.now(); scheduleSettle(area); };
    slider.addEventListener("pointerup", release);
    slider.addEventListener("pointercancel", release);
    slider.addEventListener("change", release);

    // Areas with only on/off lights get a toggle instead of a slider.
    toggle.addEventListener("click", () => {
      const pct = Number(slider.value) > 0 ? 0 : 100;
      slider.value = pct;
      area.lastTouched = Date.now();
      showValue(area, pct);
      area.sender(pct);
      scheduleSettle(area);
    });

    attachLongPress(name, () => openSheet(info), () => toast("Hold the name for scenes ✿"));
    return el;
  }

  /** Long press (touch or mouse) or right-click opens the scene sheet; a short tap gives a hint. */
  function attachLongPress(el, onLong, onTap = () => {}) {
    let timer = 0, fired = false, startX = 0, startY = 0;
    const cancel = () => { clearTimeout(timer); timer = 0; el.classList.remove("pressing"); };
    el.addEventListener("pointerdown", (e) => {
      fired = false;
      startX = e.clientX; startY = e.clientY;
      el.classList.add("pressing");
      timer = setTimeout(() => { fired = true; cancel(); buzz(); onLong(); }, LONG_PRESS_MS);
    });
    el.addEventListener("pointermove", (e) => {
      if (timer && Math.hypot(e.clientX - startX, e.clientY - startY) > 10) cancel();
    });
    el.addEventListener("pointerup", cancel);
    el.addEventListener("pointerleave", cancel);
    el.addEventListener("pointercancel", cancel);
    el.addEventListener("contextmenu", (e) => { e.preventDefault(); if (!fired) { fired = true; cancel(); onLong(); } });
    el.addEventListener("click", (e) => {
      if (fired) { e.preventDefault(); return; }
      onTap();
    });
    el.addEventListener("keydown", (e) => {  // keyboard: Enter/Space opens scenes
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onLong(); }
    });
  }

  // ---------- scene sheet ----------

  function openSheet(info) {
    $("sheet-title").textContent = info.name;
    const box = $("sheet-scenes");
    box.innerHTML = "";
    for (const scene of areaScenes) {
      const b = document.createElement("button");
      b.type = "button";
      b.dataset.scene = scene.id;
      b.textContent = scene.label;
      b.addEventListener("click", async () => {
        closeSheet();
        const area = areas.get(info.id);
        try {
          const res = await call("POST", `/areas/${encodeURIComponent(info.id)}/scenes/${encodeURIComponent(scene.id)}`);
          toast(`${info.name}: ${scene.label}`);
          showAreaScene(area, res && res.active_scene ? res.active_scene : scene.id);
          area.lastTouched = 0;
          setTimeout(refresh, 600);
        } catch (e) {
          toast(e.message, true);
        }
      });
      box.appendChild(b);
    }
    $("sheet-backdrop").hidden = false;
    $("sheet").hidden = false;
  }

  function closeSheet() {
    $("sheet-backdrop").hidden = true;
    $("sheet").hidden = true;
  }

  $("sheet-backdrop").addEventListener("click", closeSheet);
  $("sheet-cancel").addEventListener("click", closeSheet);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeSheet(); });

  // ---------- whole-house scenes ----------

  /** The chip of the house scene that's on now gets `active`; the others lose it. */
  function setActiveChip(id) {
    activeHouseScene = id || null;
    for (const [sid, chip] of chips) {
      chip.classList.toggle("active", sid === activeHouseScene);
      chip.setAttribute("aria-pressed", String(sid === activeHouseScene));
    }
  }

  /** A small dot after an area's name while one of its scenes (long-press sheet) is what it shows. */
  function showAreaScene(area, sceneId) {
    if (!area) return;
    const dot = area.name.querySelector(".scene-dot");
    const label = sceneId ? (areaScenes.find((s) => s.id === sceneId) || {}).label || sceneId : "";
    dot.hidden = !sceneId;
    dot.dataset.scene = sceneId || "";
    dot.title = label ? `Scene: ${label}` : "";
  }

  function buildHouseScenes(scenes) {
    const box = $("house-scenes");
    box.innerHTML = "";
    chips.clear();
    for (const scene of scenes) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "chip";
      b.dataset.scene = scene.id;
      b.textContent = scene.label;
      b.setAttribute("aria-pressed", "false");
      chips.set(scene.id, b);
      b.addEventListener("click", async () => {
        if (b.classList.contains("busy")) return;
        const before = activeHouseScene;
        for (const c of chips.values()) c.classList.remove("active");
        b.classList.add("busy");
        try {
          // The server answers once HA's scene timestamp is newer than the click and lightd reports it active.
          const res = await call("POST", `/scenes/${encodeURIComponent(scene.id)}`);
          if (!res || !res.confirmed) throw new Error(`${scene.label} didn't confirm (Home Assistant or lightd didn't report it on)`);
          setActiveChip(scene.id);
          toast(`Whole house: ${scene.label}`);
          for (const a of areas.values()) { a.lastTouched = 0; showAreaScene(a, null); }
          setTimeout(refresh, 600);
        } catch (e) {
          setActiveChip(before);
          toast(e.message, true);
        } finally {
          b.classList.remove("busy");
        }
      });
      box.appendChild(b);
    }
  }

  // ---------- state ----------

  async function refresh() {
    if (document.hidden) return;
    let data;
    try {
      const resp = await fetch(API + "/state");
      data = await resp.json();
      if (!resp.ok) throw new Error(data.message || `HTTP ${resp.status}`);
    } catch (e) {
      banner(`Can't read light state: ${e.message}`);
      return;
    }
    banner(data.warnings && data.warnings.length ? data.warnings.join(" · ") : "");
    const anyBusy = [...chips.values()].some((c) => c.classList.contains("busy"));
    if (!anyBusy && "active_scene" in data) setActiveChip(data.active_scene);
    const setup = data.setup || [];
    const sb = $("setup-banner");
    sb.hidden = !setup.length;
    sb.textContent = setup.length ? `${setup.map((s) => s.name).join(", ")} ${setup.length > 1 ? "need" : "needs"} setup →` : "";
    sb.title = setup.map((s) => `${s.controller}: ${s.message}`).join("\n");
    const now = Date.now();
    for (const [id, st] of Object.entries(data.areas || {})) {
      const area = areas.get(id);
      if (!area) continue;
      area.el.classList.toggle("unavailable", st.lights > 0 && st.available === 0 && st.brightness === null);
      showAreaScene(area, st.active_scene || null);
      const toggleOnly = st.dimmable === false;
      if (toggleOnly !== area.toggleOnly) {
        area.toggleOnly = toggleOnly;
        area.el.classList.toggle("toggle-only", toggleOnly);
        area.slider.hidden = toggleOnly;
        area.toggle.hidden = !toggleOnly;
      }
      if (area.dragging || now - area.lastTouched < QUIET_AFTER_TOUCH_MS) continue;
      if (st.brightness === null || st.brightness === undefined) {
        area.value.textContent = "–";
        continue;
      }
      const pct = toggleOnly ? (st.on ? 100 : 0) : st.brightness;
      area.slider.value = pct;
      showValue(area, pct);
    }
  }

  async function init() {
    let info;
    try {
      info = await call("GET", "");
    } catch (e) {
      banner(`Dashboard unavailable: ${e.message}`);
      return;
    }
    if (!info.ha) banner("Home Assistant isn't configured (HA_URL / HA_TOKEN), so only the LED strips will respond.");
    areaScenes = info.area_scenes || [];
    music.enabled = Boolean(info.music);
    $("music").hidden = !music.enabled;
    if (music.enabled) { setPlayIcon("play"); setupMusic(); }
    buildHouseScenes(info.house_scenes || []);
    const box = $("areas");
    for (const a of info.areas || []) box.appendChild(buildArea(a));
    await refresh();
    setInterval(refresh, POLL_MS);
    document.addEventListener("visibilitychange", refresh);
  }

  init();
})();
