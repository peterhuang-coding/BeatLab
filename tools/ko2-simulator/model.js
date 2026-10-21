'use strict';

/*
 * Pure model for the KO II browser practice pad.
 * Physical pad layout, per-pad 16-step patterns, built-in example and
 * step timing live here so they can be unit-tested in Node.
 *
 * Works as a CommonJS module (module.exports) and as a browser global
 * (window.SimModel); no DOM or Web Audio in this file.
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) {
    module.exports = api;
  } else {
    root.SimModel = api;
  }
})(typeof self !== 'undefined' ? self : this, function () {
  const STEP_COUNT = 16;
  const STEPS_PER_BEAT = 4;
  const BEAT_COUNT = STEP_COUNT / STEPS_PER_BEAT;

  // Four sample groups, in display order.
  const GROUPS = ['A', 'B', 'C', 'D'];
  const GROUP_TITLES = {
    A: '鼓',
    B: '主句',
    C: '回答',
    D: '低音与键盘'
  };

  // Physical KO II pad face: 3 columns x 4 rows, reading order
  // (top-left to bottom-right). PAD_LABELS is what is printed on each
  // physical pad; PAD_SLOTS is the manifest slot behind that position.
  const PAD_LABELS = ['7', '8', '9', '4', '5', '6', '1', '2', '3', '.', '0', 'ENTER'];
  const PAD_SLOTS  = [ 9,  10,  11,  6,   7,   8,   3,   4,   5,   0,   1,   2];
  const PAD_COLUMNS = 3;

  // Transport constants.
  const MIN_BPM = 60;
  const MAX_BPM = 160;
  const DEFAULT_BPM = 88;
  const DEFAULT_VOLUME = 0.65;
  const SCHED_INTERVAL_MS = 25;   // lookahead timer tick
  const SCHED_LOOKAHEAD_S = 0.1;  // schedule audio 100 ms ahead
  const MASTER_HEADROOM = 0.9;    // conservative master gain ceiling

  // Built-in starter pattern. Step positions, not pad slots.
  // Only ids that exist in the loaded manifest are used (see loadExample).
  const EXAMPLE_STEPS = {
    kick:     [0, 8, 10],
    snare:    [4, 12],
    hat:      [0, 2, 4, 6, 8, 10, 12, 14],
    lead_a:   [0],
    answer_a: [8],
    bass_30:  [0, 8],
    piano_42: [4]
  };

  function isIntegerIn(n, lo, hi) {
    return Number.isInteger(n) && n >= lo && n < hi;
  }

  function clampBpm(bpm) {
    const n = Number(bpm);
    if (!Number.isFinite(n)) return DEFAULT_BPM;
    return Math.min(MAX_BPM, Math.max(MIN_BPM, Math.round(n)));
  }

  // One sixteenth note in seconds at the given tempo.
  function stepDurationSeconds(bpm) {
    return 60 / clampBpm(bpm) / STEPS_PER_BEAT;
  }

  // Per-pad gain metadata is in dB; Web Audio wants linear amplitude.
  function gainDbToLinear(gainDb) {
    const db = Number(gainDb);
    if (!Number.isFinite(db)) return 1;
    return Math.pow(10, db / 20);
  }

  function emptySteps() {
    return new Array(STEP_COUNT).fill(false);
  }

  function isValidPad(pad) {
    return !!pad && typeof pad.id === 'string' && pad.id.length > 0;
  }

  // One empty 16-step row per manifest pad, existing ids only.
  function createPatterns(pads) {
    const out = {};
    (pads || []).forEach(function (pad) {
      if (isValidPad(pad) && !(pad.id in out)) {
        out[pad.id] = emptySteps();
      }
    });
    return out;
  }

  function clonePatterns(patterns) {
    const out = {};
    patterns = patterns || {};
    Object.keys(patterns).forEach(function (id) {
      const steps = patterns[id];
      out[id] = Array.isArray(steps) ? steps.slice() : emptySteps();
    });
    return out;
  }

  // Returns a new patterns object with one step flipped.
  // Unknown pad / out-of-range step leaves patterns unchanged (still a copy).
  function toggleStep(patterns, id, step) {
    const next = clonePatterns(patterns);
    if (!Array.isArray(next[id]) || !isIntegerIn(step, 0, STEP_COUNT)) return next;
    next[id][step] = !next[id][step];
    return next;
  }

  function clearPattern(patterns, id) {
    const next = clonePatterns(patterns);
    if (Array.isArray(next[id])) next[id] = emptySteps();
    return next;
  }

  // Built-in example restricted to pads the manifest actually provides.
  function loadExample(pads) {
    const patterns = createPatterns(pads);
    const available = {};
    Object.keys(patterns).forEach(function (id) { available[id] = true; });
    Object.keys(EXAMPLE_STEPS).forEach(function (id) {
      if (!available[id]) return;
      const row = emptySteps();
      EXAMPLE_STEPS[id].forEach(function (step) {
        if (isIntegerIn(step, 0, STEP_COUNT)) row[step] = true;
      });
      patterns[id] = row;
    });
    return patterns;
  }

  function countActive(steps) {
    if (!Array.isArray(steps)) return 0;
    return steps.reduce(function (n, on) { return n + (on ? 1 : 0); }, 0);
  }

  function hasActiveSteps(patterns, id) {
    return countActive(patterns && patterns[id]) > 0;
  }

  // Physical positions in reading order: {index, label, slot, row, column}.
  function physicalLayout() {
    return PAD_LABELS.map(function (label, index) {
      return {
        index: index,
        label: label,
        slot: PAD_SLOTS[index],
        row: Math.floor(index / PAD_COLUMNS),
        column: index % PAD_COLUMNS
      };
    });
  }

  function findPad(pads, group, slot) {
    const list = pads || [];
    for (let i = 0; i < list.length; i++) {
      if (list[i] && list[i].group === group && list[i].slot === slot) return list[i];
    }
    return null;
  }

  // Stable ordering for cross-group combination rows: A..D then slot 0..11.
  function orderedPads(pads) {
    return (pads || []).slice().sort(function (a, b) {
      const ga = GROUPS.indexOf(a.group);
      const gb = GROUPS.indexOf(b.group);
      if (ga !== gb) return (ga < 0 ? GROUPS.length : ga) - (gb < 0 ? GROUPS.length : gb);
      return a.slot - b.slot;
    });
  }

  return {
    STEP_COUNT: STEP_COUNT,
    STEPS_PER_BEAT: STEPS_PER_BEAT,
    BEAT_COUNT: BEAT_COUNT,
    GROUPS: GROUPS,
    GROUP_TITLES: GROUP_TITLES,
    PAD_LABELS: PAD_LABELS,
    PAD_SLOTS: PAD_SLOTS,
    PAD_COLUMNS: PAD_COLUMNS,
    MIN_BPM: MIN_BPM,
    MAX_BPM: MAX_BPM,
    DEFAULT_BPM: DEFAULT_BPM,
    DEFAULT_VOLUME: DEFAULT_VOLUME,
    SCHED_INTERVAL_MS: SCHED_INTERVAL_MS,
    SCHED_LOOKAHEAD_S: SCHED_LOOKAHEAD_S,
    MASTER_HEADROOM: MASTER_HEADROOM,
    EXAMPLE_STEPS: EXAMPLE_STEPS,
    clampBpm: clampBpm,
    stepDurationSeconds: stepDurationSeconds,
    gainDbToLinear: gainDbToLinear,
    emptySteps: emptySteps,
    createPatterns: createPatterns,
    clonePatterns: clonePatterns,
    toggleStep: toggleStep,
    clearPattern: clearPattern,
    loadExample: loadExample,
    countActive: countActive,
    hasActiveSteps: hasActiveSteps,
    physicalLayout: physicalLayout,
    findPad: findPad,
    orderedPads: orderedPads
  };
});
