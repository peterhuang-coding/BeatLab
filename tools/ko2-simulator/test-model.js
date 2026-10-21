'use strict';

/* Offline unit tests for model.js. Run: node tools/ko2-simulator/test-model.js */

const assert = require('assert');
const M = require('./model.js');

let passed = 0;
function test(name, fn) {
  fn();
  passed += 1;
  console.log('  ok - ' + name);
}

function activeIndexes(row) {
  return row.map(function (on, i) { return on ? i : -1; })
    .filter(function (i) { return i >= 0; });
}

// --- Physical 3 x 4 pad face -------------------------------------------------

test('physical layout has exactly 12 pads in 3 columns x 4 rows', function () {
  const layout = M.physicalLayout();
  assert.strictEqual(layout.length, 12);
  layout.forEach(function (pos, i) {
    assert.strictEqual(pos.index, i);
    assert.strictEqual(pos.row, Math.floor(i / 3));
    assert.strictEqual(pos.column, i % 3);
  });
});

test('physical labels match contract order [7,8,9,4,5,6,1,2,3,.,0,ENTER]', function () {
  assert.deepStrictEqual(M.PAD_LABELS, ['7', '8', '9', '4', '5', '6', '1', '2', '3', '.', '0', 'ENTER']);
});

test('physical slots match contract order [9,10,11,6,7,8,3,4,5,0,1,2]', function () {
  assert.deepStrictEqual(M.PAD_SLOTS, [9, 10, 11, 6, 7, 8, 3, 4, 5, 0, 1, 2]);
});

test('each slot 0..11 appears exactly once and lines up with its label', function () {
  const layout = M.physicalLayout();
  const slots = layout.map(function (p) { return p.slot; }).sort(function (a, b) { return a - b; });
  assert.deepStrictEqual(slots, [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]);
  const bySlot = {};
  layout.forEach(function (p) { bySlot[p.slot] = p.label; });
  assert.strictEqual(bySlot[0], '.');
  assert.strictEqual(bySlot[1], '0');
  assert.strictEqual(bySlot[2], 'ENTER');
  assert.strictEqual(bySlot[9], '7');
});

test('findPad resolves manifest pads through group + physical slot', function () {
  const pads = [
    { id: 'kick', group: 'A', slot: 0 },
    { id: 'hat', group: 'A', slot: 2 },
    { id: 'bass', group: 'D', slot: 0 }
  ];
  // slot 0 is the physical "." position, slot 2 is "ENTER"
  assert.strictEqual(M.findPad(pads, 'A', 0).id, 'kick');
  assert.strictEqual(M.findPad(pads, 'A', 2).id, 'hat');
  assert.strictEqual(M.findPad(pads, 'D', 0).id, 'bass');
  assert.strictEqual(M.findPad(pads, 'A', 1), null);
  assert.strictEqual(M.findPad(pads, 'B', 0), null);
});

// --- Default patterns --------------------------------------------------------

test('default patterns are created only for existing pad ids', function () {
  const pads = [{ id: 'kick' }, null, { id: 'hat' }, {}, { id: 'kick' }];
  const patterns = M.createPatterns(pads);
  assert.deepStrictEqual(Object.keys(patterns).sort(), ['hat', 'kick']);
  assert.strictEqual(patterns.kick.length, M.STEP_COUNT);
  assert.strictEqual(M.countActive(patterns.kick), 0);
});

test('toggleStep flips a step and never mutates its input', function () {
  const before = M.createPatterns([{ id: 'kick' }, { id: 'hat' }]);
  const after1 = M.toggleStep(before, 'kick', 0);
  assert.strictEqual(before.kick[0], false, 'input pattern must stay untouched');
  assert.strictEqual(after1.kick[0], true);
  assert.notStrictEqual(after1, before);
  assert.notStrictEqual(after1.kick, before.kick);
  const after2 = M.toggleStep(after1, 'kick', 0);
  assert.strictEqual(after2.kick[0], false);
  assert.strictEqual(after1.kick[0], true, 'previous result must stay untouched');
});

test('toggleStep ignores unknown pads and out-of-range steps', function () {
  const patterns = M.createPatterns([{ id: 'kick' }]);
  assert.deepStrictEqual(M.toggleStep(patterns, 'ghost', 0), patterns);
  assert.deepStrictEqual(M.toggleStep(patterns, 'kick', -1), patterns);
  assert.deepStrictEqual(M.toggleStep(patterns, 'kick', 16), patterns);
});

test('clearPattern empties only the selected pad', function () {
  let p = M.createPatterns([{ id: 'kick' }, { id: 'hat' }]);
  p = M.toggleStep(p, 'kick', 4);
  p = M.toggleStep(p, 'hat', 8);
  p = M.clearPattern(p, 'kick');
  assert.strictEqual(M.countActive(p.kick), 0);
  assert.strictEqual(p.hat[8], true);
});

// --- Switching pads / groups keeps independent patterns ----------------------

test('selecting another pad shows its own pattern and never disturbs others', function () {
  let p = M.createPatterns([
    { id: 'kick', group: 'A' },
    { id: 'snare', group: 'A' }
  ]);
  p = M.toggleStep(p, 'kick', 0);
  p = M.toggleStep(p, 'kick', 8);
  // "select" snare: its row is still empty even though kick has hits.
  assert.deepStrictEqual(activeIndexes(p.snare), []);
  p = M.toggleStep(p, 'snare', 4);
  assert.deepStrictEqual(activeIndexes(p.kick), [0, 8]);
  assert.deepStrictEqual(activeIndexes(p.snare), [4]);
});

test('switching groups does not reset patterns in other groups', function () {
  let p = M.createPatterns([
    { id: 'kick', group: 'A' },
    { id: 'lead_a', group: 'B' },
    { id: 'answer_a', group: 'C' },
    { id: 'bass_30', group: 'D' }
  ]);
  p = M.toggleStep(p, 'kick', 0);
  p = M.toggleStep(p, 'lead_a', 2);
  p = M.toggleStep(p, 'answer_a', 8);
  p = M.toggleStep(p, 'bass_30', 10);
  // Visiting every group (pure model never auto-clears anything).
  M.GROUPS.forEach(function () {
    p = M.clonePatterns(p);
  });
  assert.deepStrictEqual(activeIndexes(p.kick), [0]);
  assert.deepStrictEqual(activeIndexes(p.lead_a), [2]);
  assert.deepStrictEqual(activeIndexes(p.answer_a), [8]);
  assert.deepStrictEqual(activeIndexes(p.bass_30), [10]);
});

test('orderedPads sorts combination rows across all groups (A..D, slot asc)', function () {
  const ordered = M.orderedPads([
    { id: 'bass', group: 'D', slot: 3 },
    { id: 'snare', group: 'A', slot: 4 },
    { id: 'answer', group: 'C', slot: 1 },
    { id: 'kick', group: 'A', slot: 0 },
    { id: 'lead', group: 'B', slot: 2 }
  ]).map(function (pad) { return pad.id; });
  assert.deepStrictEqual(ordered, ['kick', 'snare', 'lead', 'answer', 'bass']);
});

// --- Built-in example --------------------------------------------------------

test('example pattern matches the contract when every id exists', function () {
  const ids = ['kick', 'snare', 'hat', 'lead_a', 'answer_a', 'bass_30', 'piano_42'];
  const pads = ids.map(function (id, i) { return { id: id, group: 'A', slot: i }; });
  const ex = M.loadExample(pads);
  assert.deepStrictEqual(activeIndexes(ex.kick), [0, 8, 10]);
  assert.deepStrictEqual(activeIndexes(ex.snare), [4, 12]);
  assert.deepStrictEqual(activeIndexes(ex.hat), [0, 2, 4, 6, 8, 10, 12, 14]);
  assert.deepStrictEqual(activeIndexes(ex.lead_a), [0]);
  assert.deepStrictEqual(activeIndexes(ex.answer_a), [8]);
  assert.deepStrictEqual(activeIndexes(ex.bass_30), [0, 8]);
  assert.deepStrictEqual(activeIndexes(ex.piano_42), [4]);
});

test('example only uses ids that exist (synthetic manifest has just kick)', function () {
  const ex = M.loadExample([{ id: 'kick', group: 'A', slot: 0 }]);
  assert.deepStrictEqual(Object.keys(ex), ['kick']);
  assert.deepStrictEqual(activeIndexes(ex.kick), [0, 8, 10]);
  assert.strictEqual('snare' in ex, false);
  assert.strictEqual('piano_42' in ex, false, 'optional piano is absent, not invented');
});

test('example never marks more than 16 steps and returns fresh rows', function () {
  const a = M.loadExample([{ id: 'kick' }]);
  const b = M.loadExample([{ id: 'kick' }]);
  assert.notStrictEqual(a.kick, b.kick);
  assert.ok(a.kick.length === M.STEP_COUNT);
});

// --- Tempo / gain math -------------------------------------------------------

test('sixteenth duration is 60 / bpm / 4 and bpm is clamped to 60..160', function () {
  assert.strictEqual(M.stepDurationSeconds(60), 0.25);
  assert.strictEqual(M.stepDurationSeconds(120), 0.125);
  assert.strictEqual(M.stepDurationSeconds(1), 0.25, 'below minimum clamps to 60');
  assert.strictEqual(M.stepDurationSeconds(999), 60 / 160 / 4, 'above maximum clamps to 160');
  assert.strictEqual(M.stepDurationSeconds('nonsense'), 60 / 88 / 4, 'bad bpm falls back to 88');
});

test('clampBpm rounds and bounds manifest tempo', function () {
  assert.strictEqual(M.clampBpm(88), 88);
  assert.strictEqual(M.clampBpm(87.6), 88);
  assert.strictEqual(M.clampBpm(10), 60);
  assert.strictEqual(M.clampBpm(300), 160);
});

test('gain_db converts to linear amplitude', function () {
  assert.strictEqual(M.gainDbToLinear(0), 1);
  assert.ok(Math.abs(M.gainDbToLinear(-6) - 0.501187) < 1e-4);
  assert.ok(Math.abs(M.gainDbToLinear(-5) - 0.562341) < 1e-4);
  assert.strictEqual(M.gainDbToLinear('bad'), 1);
});

test('exposed constants match the contract', function () {
  assert.strictEqual(M.STEP_COUNT, 16);
  assert.deepStrictEqual(M.GROUPS, ['A', 'B', 'C', 'D']);
  assert.strictEqual(M.DEFAULT_VOLUME, 0.65);
  assert.strictEqual(M.SCHED_INTERVAL_MS, 25);
  assert.strictEqual(M.SCHED_LOOKAHEAD_S, 0.1);
});

console.log('\nmodel tests: ' + passed + ' passed');
