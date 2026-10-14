/**
 * The "Output" switch in the top bar: off = lightd sends nothing, so the boards show their own WLED effects.
 *
 * Markup: <label class="switch"><input type="checkbox" id="output-toggle" role="switch" aria-label="Output"></label>
 */

const $ = (id) => document.getElementById(id);

// Show the state lightd reports (from /api/status), unless a change is on its way.
let busy = false;
export function showOutput(enabled) {
  const box = $("output-toggle");
  if (!box || busy || enabled === undefined) return;
  box.checked = enabled;
  box.closest(".switch").classList.toggle("off", !enabled);
}

export function initOutputSwitch(api, onError) {
  const box = $("output-toggle");
  box.addEventListener("change", async () => {
    const enabled = box.checked;
    busy = true;
    try {
      const res = await api("PUT", "/output", { enabled });
      busy = false;
      showOutput(res.enabled);
    } catch (e) {
      busy = false;
      showOutput(!enabled);
      onError(`Could not turn output ${enabled ? "on" : "off"}: ${e.message}`);
    }
  });
}
