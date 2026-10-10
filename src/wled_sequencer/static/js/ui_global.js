/**
 * Global Settings Component: Time Dilation, Looping Modes & Engine Status
 */

import { engineClient } from "./engine_client.js";
import { state } from "./state.js";

export function initGlobalSettings() {
  const tbDilationSlider = document.getElementById("toolbar-dilation-slider");
  const tbDilationVal = document.getElementById("toolbar-dilation-val");
  const globalDilationSlider = document.getElementById("global-dilation-slider");
  const globalDilationVal = document.getElementById("global-dilation-val");

  const selLoopMode = document.getElementById("sel-loop-mode");
  const inpLoopCount = document.getElementById("inp-loop-count");

  const inpEngineUrl = document.getElementById("inp-engine-url");
  const engineDot = document.getElementById("engine-status-dot");
  const engineText = document.getElementById("engine-status-text");
  const statusEngineFps = document.getElementById("status-engine-fps");

  function setDilation(val) {
    const dilation = Math.max(0.1, Math.min(5.0, parseFloat(val) || 1.0));
    state.sequence.time_dilation = dilation;

    const display = `${dilation.toFixed(1)}x`;
    if (tbDilationSlider) tbDilationSlider.value = dilation;
    if (tbDilationVal) tbDilationVal.textContent = display;
    if (globalDilationSlider) globalDilationSlider.value = dilation;
    if (globalDilationVal) globalDilationVal.textContent = display;
  }

  // Dilation sliders sync
  if (tbDilationSlider) {
    tbDilationSlider.addEventListener("input", (e) => setDilation(e.target.value));
  }
  if (globalDilationSlider) {
    globalDilationSlider.addEventListener("input", (e) => setDilation(e.target.value));
  }

  // Looping modes
  if (selLoopMode) {
    selLoopMode.value = state.sequence.loop_mode || "infinite";
    selLoopMode.addEventListener("change", () => {
      state.sequence.loop_mode = selLoopMode.value;
      if (inpLoopCount) {
        inpLoopCount.style.display = selLoopMode.value === "count" ? "inline-block" : "none";
      }
    });
  }

  if (inpLoopCount) {
    inpLoopCount.value = state.sequence.loop_count || 1;
    inpLoopCount.addEventListener("change", () => {
      state.sequence.loop_count = Math.max(1, parseInt(inpLoopCount.value, 10) || 1);
    });
  }

  // Engine URL & ping
  if (inpEngineUrl) {
    inpEngineUrl.value = state.engineUrl;
    inpEngineUrl.addEventListener("change", () => {
      let url = inpEngineUrl.value.trim();
      if (!url.startsWith("/") && !url.startsWith("http://") && !url.startsWith("https://")) {
        url = "http://" + url;
      }
      state.engineUrl = url;
      engineClient.ping();
    });
  }

  function updateEngineStatus(online, fps) {
    if (engineDot) {
      engineDot.className = `status-dot ${online ? "online" : "offline"}`;
    }
    if (engineText) {
      engineText.textContent = online ? `Online (${fps.toFixed(1)} FPS)` : "Offline (Click to retry)";
    }
    if (statusEngineFps) {
      statusEngineFps.textContent = online ? `Engine: ${fps.toFixed(1)} FPS` : "Engine: Offline";
    }
  }

  // Allow clicking on engine status to retry connection
  const engineBadge = document.getElementById("engine-status-badge");
  if (engineBadge) {
    engineBadge.style.cursor = "pointer";
    engineBadge.addEventListener("click", () => {
      if (engineText) engineText.textContent = "Connecting...";
      engineClient.ping();
    });
  }

  // State Subscriptions
  state.subscribe((event, data) => {
    if (event === "engine_status" && data) {
      updateEngineStatus(data.online, data.fps);
    } else if (event === "sequence_loaded") {
      setDilation(state.sequence.time_dilation || 1.0);
      if (selLoopMode) selLoopMode.value = state.sequence.loop_mode || "infinite";
      if (inpLoopCount) {
        inpLoopCount.value = state.sequence.loop_count || 1;
        inpLoopCount.style.display = state.sequence.loop_mode === "count" ? "inline-block" : "none";
      }
    }
  });

  setDilation(state.sequence.time_dilation || 1.0);
}
