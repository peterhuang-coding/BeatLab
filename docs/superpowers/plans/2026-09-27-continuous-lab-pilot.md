# BeatLab Continuous Lab Pilot Implementation Plan

> **For agentic workers:** Execute the two independent packages through the current task-tiering cc-plan entry; primary review and adoption are sequential. User explicitly requested “多让 cc 跑，让它跑跑试试”.

**Goal:** Run real Coding Plan engineering work, then enable a bounded seven-day trial with independently reviewed evidence.

**Architecture:** A small SQLite task ledger gates BeatLab dispatch; a separate reproducible audio benchmark supplies technical evidence. The existing native heartbeat remains the scheduler and the primary agent remains the reviewer. No daemon or second task runner.

**Tech Stack:** Python 3.12, sqlite3, unittest, numpy/soundfile, existing Rubber Band and BeatLab modules.

## Authorization and boundaries

This request starts the proposed trial's engineering preparation: queue/recovery and audio correctness baseline, then concrete counterexample fixes in those areas. It does not approve all eight task families, automatic Keep, new music publishing, new paid usage or arbitrary new product directions. Existing music is preserved. Trial may activate only after relevant offline gates and a real local baseline pass. Proposed cadence: Asia/Shanghai 22/00/02 execution opportunities and 09 review, seven days from actual activation, one original heartbeat restored at expiry.

Same-wave CC maximum two, each 1,200 seconds/30 turns, Seed Evolving with explicitly requested thinking enabled and native effort high. Other projects currently have CC work in flight: fill at most the observed free slots; the second independent package may join the same wave once another slot frees; do not cancel, change IDs, or inspect their private task contents. The local project queue can coordinate participating dispatchers only. A live process preflight must also block new BeatLab work if other CC jobs are active; this is not an account-wide lock against clients that ignore the queue.

## Task A — persistent dispatch/review gate

Create `pipeline/lab_queue.py`, `tests/test_lab_queue.py`, `docs/product/lab-queue.md` only. No model calls, subprocess runner, scheduler mutation or Git actions inside the queue.

- [ ] Write and run failing behavior tests in temporary databases.
- [ ] Implement a JSON CLI: `python -B -m pipeline.lab_queue --db PATH --request REQUEST.json`; read-only `--status` must not create or mutate a missing/existing DB.
- [ ] Define request operations init, enqueue, claim, transition and status. Persist immutable task ID, scope, dependencies, base SHA, input SHA, whitelist, exact checks and attempt/request ID. Same payload is idempotent; conflicting identity is rejected.
- [ ] Atomic claim of one/two tasks via `BEGIN IMMEDIATE`: approved scope/dependencies, active trial/deadline (full 20-minute budget), not paused, no unknown, at most 2 running/unknown, running + awaiting_review + unknown at most 4. Caller supplies observed external active count; a nonzero count blocks the entire new wave. All claim failures roll back.
- [ ] States ready/running/awaiting_review/accepted/rejected/blocked/unknown; transition receipts and reasons persist. Unknown never releases a slot or auto-requeues after elapsed time. A manual reconciliation can move it to awaiting_review/rejected only with explicit stopped-worker evidence, preserving audit history.
- [ ] Add pure trial-time decision helper, timezone-aware timestamps, deadline/paused gating and restore-required indication. It never claims automation has been restored; original fields and actual tool readback are handled by the parent.
- [ ] Test simultaneous separate processes, restart, stale IDs, dependencies, deadline/pause, external occupancy, review backlog, invalid transitions and unknown reconciliation; write exact CLI examples and limitations.

Acceptance command: `/Volumes/SanDisk2TB/BeatLab/.venv/bin/python -B -m unittest discover -s tests -p test_lab_queue.py -v` (exit 0). Primary separately exercises real CLI/database and expiry behavior before adopting.

## Task B — known-signal and read-only delivery baseline

Create `pipeline/lab_audio_baseline.py`, `tests/test_lab_audio_baseline.py`, `docs/product/lab-audio-baseline.md` only. Read existing `pipeline/beatgrid.py`, `pipeline/sample_flip.py`, `pipeline/ableton_export.py`; do not edit them or thresholds to force success.

- [ ] Write failing tests for the report contract, independently known timing/frequency, adverse cases and read-only behavior.
- [ ] Implement `python -B -m pipeline.lab_audio_baseline --out NEW_DIRECTORY [--song SONG_DIRECTORY]`. New output only; refuse existing destination and destinations inside an input song. No network, DB, media-library mutation or persistent daemon.
- [ ] Deterministic synthetic source: two distinct tone regions (e.g. 220/440Hz). Actually run make_slice on the second region, +12 semitones and changed duration; independently measure dominant frequency near 880Hz and sample count. Check unpitched crop and asymmetric forward/reverse energy; nonfinite/out-of-range inputs must fail.
- [ ] Known irregular/missing beat events at explicit offset: compare fitted phase/period to ground truth, not to its own output. Sparse events explicitly exercise low-confidence fallback. Record case-level actual/expected/tolerance/status; unavailable Rubber Band is unavailable, not passed.
- [ ] Optional real-song audit calls existing validate_song read-only; record input content hashes before/after for manifest/score/mix/stems/MIDI, duration/channels/finite audio/stem-sum residual, explicit Live unverified. Hash mismatches or source mutation fail. No fabricated lyric identity or “good music” score.
- [ ] Produce report.json with schema version, environment/tool identity, immutable case definitions, failed/unavailable case list and explicit subjective status unverified. CLI nonzero on any failed/unavailable required case. All tests use temporary synthetic fixtures; no real song is sent to CC.

Acceptance command: `/Volumes/SanDisk2TB/BeatLab/.venv/bin/python -B -m unittest discover -s tests -p test_lab_audio_baseline.py -v` (exit 0). Primary runs CLI locally against existing Prism Cut v2 and independently checks old mixes unchanged.

## Task C — parent adoption, live evidence and trial activation

- [ ] Dry-run both fixed handoffs; verify actual requested thinking/high. Submit the same two in one wave as observed slots become available, total observed CC workers no more than two; do not wait for the first BeatLab package to finish before the second if a slot is free. Save status/logs; no unknown resubmission. One targeted quality correction at most per logical task after confirmed termination.
- [ ] Inspect actual worker tool trace, files and hashes; run the named tests independently, review semantics, then copy only approved whitelist files sequentially. Do not rewrite accepted work from scratch.
- [ ] Run the audio benchmark on local Prism v2 without copying its media to CC. Exercise persistent queue through the real JSON CLI, including repeated enqueue/claim/restart and deadline denial. Record historical jobs separately, never pretend they were launched through a queue which did not yet exist.
- [ ] If gates pass, save exact original automation fields plus actual start/deadline and restore_required. Update only existing beatlab heartbeat through automation_update; read config back. 09 review, other allowed times one dispatch wave; no missed-time catchup. At expiry restore original fields through tool/readback and mark restore_required false only after verification.
- [ ] Reserve future work only for concrete failures/gaps within the approved engineering scope. Queue empty/no new evidence means rest, not invented tasks. Music and Live human checks remain pending.
- [ ] Commit code, tests, plan and evidence on the execution branch; normal push and remote SHA verification; update Notion, mainline, round and project Hub. No merge main.

## Result

First wave completed, not adopted: original43+22 tests and10 real-audio checks pass, but independent queue4/audio4 contract cases fail. Two sole targeted corrections are prepared and dry-run validated, not submitted while external capacity is occupied. Original22:00 heartbeat prompt now resumes them; higher frequency remains gated. The seven-day window includes preparation (2026-09-27 20:24:56 to2026-10-04 20:24:56 Asia/Shanghai), not reset at activation. See [actual results](../../research/2026-09-27-continuous-lab-pilot-result.md) and continuous-lab.json.
