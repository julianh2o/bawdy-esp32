/**
 * Central Reactive State Container for Pattern Sequencer
 */

export class SequencerState {
  constructor() {
    this.sequence = {
      name: "Garage Architectural Light Show",
      description: "Default demo sequence",
      author: "WLED Studio",
      loop_mode: "infinite", // infinite, count, once_hold, once_blackout
      loop_count: 1,
      time_dilation: 1.0,
      steps: []
    };

    this.filename = "garage_light_show.json";
    this.selectedIndex = 0;

    // Playback Runtime State
    this.isPlaying = false;
    this.isPaused = false;
    this.currentPlayingIndex = -1;
    this.activeStepIds = new Set();
    this.totalElapsed = 0;      // global sequence timeline clock in seconds
    this.currentLoopIteration = 0;

    // Engine Telemetry
    // Same-origin path proxied by the sequencer web server to the daemon (see ENGINE_URL / --engine).
    this.engineUrl = "/engine";
    this.engineOnline = false;
    this.engineFps = 0.0;
    this.statusMessage = "Ready";

    this.listeners = new Set();
  }

  subscribe(listener) {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  notify(event, payload) {
    for (const listener of this.listeners) {
      listener(event, payload, this);
    }
  }

  get selectedStep() {
    if (this.selectedIndex >= 0 && this.selectedIndex < this.sequence.steps.length) {
      return this.sequence.steps[this.selectedIndex];
    }
    return null;
  }

  get totalSequenceDuration() {
    if (!this.sequence.steps || this.sequence.steps.length === 0) return 0;
    let maxStop = 0;
    for (const s of this.sequence.steps) {
      const start = s.start_time_sec ?? 0;
      const dur = s.duration_sec ?? s.hold_duration_sec ?? 4.0;
      const stop = start + dur;
      if (stop > maxStop) maxStop = stop;
    }
    return Math.round(maxStop * 1000) / 1000;
  }

  setSequence(newSeq, filename = null) {
    this.sequence = newSeq;
    if (filename) this.filename = filename;

    // Normalize steps for timeline sections
    let runningTime = 0;
    for (const s of this.sequence.steps || []) {
      if (!s.section) s.section = "Main";
      if (s.duration_sec === undefined) {
        s.duration_sec = s.hold_duration_sec !== undefined ? Number(s.hold_duration_sec) : 4.0;
      }
      if (s.start_time_sec === undefined) {
        s.start_time_sec = runningTime;
        runningTime = Math.round((s.start_time_sec + s.duration_sec) * 1000) / 1000;
      }
      if (s.fade_out_sec === undefined) s.fade_out_sec = 0.5;
      s.stop_time_sec = Math.round((s.start_time_sec + s.duration_sec) * 1000) / 1000;
    }

    this.selectedIndex = this.sequence.steps.length > 0 ? 0 : -1;
    this.activeStepIds.clear();
    this.notify("sequence_loaded", this.sequence);
  }

  selectStep(index) {
    if (index >= 0 && index < this.sequence.steps.length) {
      this.selectedIndex = index;
      this.notify("step_selected", index);
    }
  }

  updateSelectedStep(updates) {
    const step = this.selectedStep;
    if (!step) return;
    Object.assign(step, updates);

    if (step.start_time_sec !== undefined && step.duration_sec !== undefined) {
      step.stop_time_sec = Math.round((step.start_time_sec + step.duration_sec) * 1000) / 1000;
    }

    this.notify("step_updated", { index: this.selectedIndex, step });
  }

  addStep(customStep = null) {
    const newId = `step_${Date.now()}`;
    let defaultStart = 0.0;
    let defaultSection = "Main";
    let defaultLayer = 0;

    if (this.sequence.steps.length > 0) {
      const last = this.sequence.steps[this.sequence.steps.length - 1];
      defaultStart = Math.round((last.start_time_sec + last.duration_sec) * 10) / 10;
      defaultSection = last.section || "Main";
      defaultLayer = (last.target_layer + 1) % 10;
    }

    const step = customStep || {
      id: newId,
      name: `Cue ${this.sequence.steps.length + 1}`,
      section: defaultSection,
      target_layer: defaultLayer,
      pattern_id: "rainbow",
      primary_color: "#FF0000",
      palette: null,
      speed: 1.0,
      brightness: 1.0,
      blend_mode: "OVERWRITE",
      channels: null,
      target_opacity: 1.0,
      start_time_sec: defaultStart,
      duration_sec: 4.0,
      transition_sec: 1.0,
      fade_out_sec: 0.5
    };

    step.stop_time_sec = Math.round((step.start_time_sec + step.duration_sec) * 1000) / 1000;

    this.sequence.steps.push(step);
    this.selectedIndex = this.sequence.steps.length - 1;
    this.notify("step_added", { index: this.selectedIndex, step });
  }

  duplicateStep(index) {
    if (index < 0 || index >= this.sequence.steps.length) return;
    const source = this.sequence.steps[index];
    const clone = JSON.parse(JSON.stringify(source));
    clone.id = `step_${Date.now()}`;
    clone.name = `${source.name} (Copy)`;
    clone.stop_time_sec = Math.round((clone.start_time_sec + clone.duration_sec) * 1000) / 1000;
    this.sequence.steps.splice(index + 1, 0, clone);
    this.selectedIndex = index + 1;
    this.notify("step_added", { index: this.selectedIndex, step: clone });
  }

  deleteStep(index) {
    if (index < 0 || index >= this.sequence.steps.length) return;
    this.sequence.steps.splice(index, 1);
    if (this.selectedIndex >= this.sequence.steps.length) {
      this.selectedIndex = this.sequence.steps.length - 1;
    }
    this.notify("step_deleted", index);
  }

  moveStep(index, direction) {
    const target = index + direction;
    if (index < 0 || index >= this.sequence.steps.length) return;
    if (target < 0 || target >= this.sequence.steps.length) return;
    const [item] = this.sequence.steps.splice(index, 1);
    this.sequence.steps.splice(target, 1, item);
    this.selectedIndex = target;
    this.notify("steps_reordered", { from: index, to: target });
  }
}

export const state = new SequencerState();
