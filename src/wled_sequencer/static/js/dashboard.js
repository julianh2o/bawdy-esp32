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

  const $ = (id) => document.getElementById(id);
  const areas = new Map();  // id -> { el, slider, value, name, lastTouched, dragging, sender }
  let areaScenes = [];
  let toastTimer = 0;

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
    area.value.textContent = pct > 0 ? `${pct}%` : "Off";
    area.value.classList.toggle("on", pct > 0);
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
    return (value) => {
      pending = value;
      if (!timer) flush();
    };
  }

  // ---------- areas ----------

  function buildArea(info) {
    const el = document.createElement("div");
    el.className = "area";
    el.innerHTML = `
      <div class="area-head">
        <button type="button" class="area-name"></button>
        <span class="area-value">–</span>
      </div>
      <input type="range" min="0" max="100" step="1" value="0">`;
    const name = el.querySelector(".area-name");
    const slider = el.querySelector("input");
    const value = el.querySelector(".area-value");
    name.textContent = info.name;
    slider.setAttribute("aria-label", `${info.name} brightness`);

    const area = { el, slider, value, name, lastTouched: 0, dragging: false, sender: makeSender(info.id) };
    areas.set(info.id, area);

    slider.addEventListener("input", () => {
      const pct = Number(slider.value);
      area.lastTouched = Date.now();
      showValue(area, pct);
      area.sender(pct);
    });
    slider.addEventListener("pointerdown", () => { area.dragging = true; });
    const release = () => { area.dragging = false; area.lastTouched = Date.now(); };
    slider.addEventListener("pointerup", release);
    slider.addEventListener("pointercancel", release);
    slider.addEventListener("change", release);

    attachLongPress(name, () => openSheet(info));
    return el;
  }

  /** Long press (touch or mouse) or right-click opens the scene sheet; a short tap gives a hint. */
  function attachLongPress(el, onLong) {
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
    el.addEventListener("contextmenu", (e) => { e.preventDefault(); if (!fired) { cancel(); onLong(); } });
    el.addEventListener("click", (e) => {
      if (fired) { e.preventDefault(); return; }
      toast("Hold the name for scenes ✿");
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
        try {
          await call("POST", `/areas/${encodeURIComponent(info.id)}/scenes/${encodeURIComponent(scene.id)}`);
          toast(`${info.name}: ${scene.label}`);
          areas.get(info.id).lastTouched = 0;
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

  function buildHouseScenes(scenes) {
    const box = $("house-scenes");
    box.innerHTML = "";
    for (const scene of scenes) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "chip";
      b.dataset.scene = scene.id;
      b.textContent = scene.label;
      b.addEventListener("click", async () => {
        b.classList.add("busy");
        try {
          await call("POST", `/scenes/${encodeURIComponent(scene.id)}`);
          toast(`Whole house: ${scene.label}`);
          for (const a of areas.values()) a.lastTouched = 0;
          setTimeout(refresh, 1200);
        } catch (e) {
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
    const now = Date.now();
    for (const [id, st] of Object.entries(data.areas || {})) {
      const area = areas.get(id);
      if (!area) continue;
      area.el.classList.toggle("unavailable", st.lights > 0 && st.available === 0 && st.brightness === null);
      if (area.dragging || now - area.lastTouched < QUIET_AFTER_TOUCH_MS) continue;
      if (st.brightness === null || st.brightness === undefined) {
        area.value.textContent = "–";
        continue;
      }
      area.slider.value = st.brightness;
      showValue(area, st.brightness);
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
    buildHouseScenes(info.house_scenes || []);
    const box = $("areas");
    for (const a of info.areas || []) box.appendChild(buildArea(a));
    await refresh();
    setInterval(refresh, POLL_MS);
    document.addEventListener("visibilitychange", refresh);
  }

  init();
})();
