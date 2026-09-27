"""Resumable local locked-edit workflow tests.

Real audio throughout: song.render_score builds a voice/kick/bass parent from
synthetic oscillator WAVs; two genuinely different phrase families are rendered
by the actual locked_edit engine; blind_review serves the real review artifact,
exercised over loopback HTTP. No mocks for edit/music protection.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

import blind_review as br  # noqa: E402
import creative_workflow as cw  # noqa: E402
import locked_edit as le  # noqa: E402
from ableton_export import (  # noqa: E402
    DEFAULT_CLIP_TEMPLATE, DEFAULT_TEMPLATE, validate_song)
from song import render_score  # noqa: E402

SR = 22050
HAS_TEMPLATES = DEFAULT_TEMPLATE.is_file() and DEFAULT_CLIP_TEMPLATE.is_file()


def tone_wav(path: Path, freq: float, dur: float = 0.5, amp: float = 0.3,
             sr: int = SR) -> None:
    t = np.arange(round(dur * sr)) / sr
    sf.write(path, amp * np.sin(2 * np.pi * freq * t), sr, subtype="FLOAT")


class WorkflowBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self._clear_hook)
        self.root = Path(self.tmp.name)
        tone_wav(self.root / "tone440.wav", 440)
        tone_wav(self.root / "tone330.wav", 330)
        self.sources = self.root / "sources"
        self.sources.mkdir()
        tone_wav(self.sources / "phrase523.wav", 523)
        tone_wav(self.sources / "phrase659.wav", 659)
        self.parent = self.root / "parent"
        render_score(self.score(), self.parent, sr=SR)
        self.jobs = self.root / "jobs"

    def _clear_hook(self):
        cw._RENDER_HOOK = None

    def score(self):
        sample = str(self.root / "tone440.wav")
        return {"title": "fixture", "bpm": 120, "bars": 2, "tail_seconds": 0.2,
                "tracks": [
                    {"id": "voice", "sample": sample, "root_midi": 69, "gain_db": -12,
                     "role": "answer", "events": [
                         {"beat": 0, "note": 69, "duration_beats": 1, "velocity": 0.8},
                         {"beat": 4, "note": 69, "duration_beats": 1, "velocity": 0.8}]},
                    {"id": "kick", "sample": sample, "drum": True, "gain_db": -12,
                     "events": [
                         {"beat": 0, "note": 69, "duration_beats": 1, "velocity": 0.8},
                         {"beat": 4, "note": 69, "duration_beats": 1, "velocity": 0.8}]},
                    {"id": "bass", "sample": str(self.root / "tone330.wav"),
                     "root_midi": 69, "role": "bass", "gain_db": -12, "events": [
                         {"beat": 0, "note": 57, "duration_beats": 1, "velocity": 0.8},
                         {"beat": 4, "note": 57, "duration_beats": 1, "velocity": 0.8}]}]}

    def sha(self, path) -> str:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    def snapshot(self, directory: Path) -> dict:
        snap = {}
        for p in Path(directory).rglob("*"):
            if p.is_file():
                snap[str(p.relative_to(directory))] = (
                    hashlib.sha256(p.read_bytes()).hexdigest(),
                    p.stat().st_mtime_ns)
        return snap

    def edit_dict(self, **over) -> dict:
        d = {"track_id": "voice", "start_beat": 4, "end_beat": 8,
             "expected_parent_mix_sha256": self.sha(self.parent / "full_mix.wav"),
             "current_family_id": "fam-old", "identity_verified": True,
             "purpose": "vocal-replace"}
        d.update(over)
        return d

    def phrase_dict(self, which: str = "a", **over) -> dict:
        if which == "a":
            path = self.sources / "phrase523.wav"
            d = {"id": "answer-a", "family_id": "fam-a"}
        else:
            path = self.sources / "phrase659.wav"
            d = {"id": "answer-b", "family_id": "fam-b"}
        d.update({"path": str(path), "sha256": self.sha(path),
                  "identity_verified": True,
                  "allowed_uses": ["vocal-replace"],
                  "root_midi": 60, "role": "answer"})
        d.update(over)
        return d

    def make_request(self, phrases=("a", "b"), budget=None, **edit_over) -> dict:
        request = {"version": 1, "request_id": "wf-demo-1",
                   "parent": str(self.parent),
                   "edit": self.edit_dict(**edit_over),
                   "phrases": [self.phrase_dict(w) for w in phrases]}
        if budget is not None:
            request["budget"] = budget
        return request

    def submit(self, request=None):
        return cw.submit(request or self.make_request(), self.jobs)

    def run_to_review(self, request=None):
        submitted = self.submit(request)
        job = Path(submitted["job"])
        result = cw.run(job)
        self.assertEqual(result["status"], "awaiting_review")
        return job, result

    def review_dir(self, job: Path) -> Path:
        return Path(job) / "review"

    def label_for(self, job: Path, version_id: str) -> str:
        mapping = json.loads((self.review_dir(job) / "private" / "mapping.json")
                             .read_text())
        for label, entry in mapping["assignment"].items():
            if entry["id"] == version_id:
                return label
        raise AssertionError(f"version {version_id} not in review mapping")


class SubmitValidationTests(WorkflowBase):
    def test_invalid_budgets_rejected(self):
        bad = [
            {"max_candidates": 0}, {"max_candidates": 3},
            {"max_attempts": 0}, {"max_attempts": 7},
            {"max_candidates": 1.5}, {"max_attempts": "6"},
            {"max_candidates": None}, {"extra_key": 1},
        ]
        for budget in bad:
            with self.subTest(budget=budget):
                with self.assertRaises(ValueError):
                    cw.submit(self.make_request(budget=budget), self.jobs)
        self.assertFalse(self.jobs.exists())

    def test_bool_budget_rejected(self):
        with self.assertRaises(ValueError):
            cw.submit(self.make_request(
                budget={"max_candidates": True, "max_attempts": True}), self.jobs)

    def test_budget_defaults_applied(self):
        result = self.submit()
        self.assertEqual(result["status"], "queued")
        self.assertEqual(result["budget"],
                         {"max_candidates": 2, "max_attempts": 6})

    def test_duplicate_phrase_ids_rejected(self):
        request = self.make_request(phrases=("a", "a"))
        with self.assertRaisesRegex(ValueError, "duplicate|重复"):
            cw.submit(request, self.jobs)

    def test_duplicate_family_ids_rejected(self):
        phrase_b = self.phrase_dict("b", family_id="fam-a")
        request = self.make_request(phrases=("a",))
        request["phrases"].append(phrase_b)
        with self.assertRaisesRegex(ValueError, "family|famil"):
            cw.submit(request, self.jobs)

    def test_structural_schema_errors_rejected(self):
        good = self.make_request()
        mutations = [
            ("version", 2), ("request_id", "bad id!"), ("request_id", "UPPER"),
            ("parent", "relative/path"),
        ]
        for key, value in mutations:
            request = json.loads(json.dumps(good))
            request[key] = value
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    cw.submit(request, self.jobs)
        bad_edits = [
            {"track_id": "bad id"}, {"expected_parent_mix_sha256": "x" * 64},
            {"identity_verified": "unknown"}, {"purpose": ""},
            {"start_beat": True}, {"start_beat": float("nan")},
        ]
        for over in bad_edits:
            request = self.make_request(**over)
            with self.subTest(over=over):
                with self.assertRaises(ValueError):
                    cw.submit(request, self.jobs)
        bad_phrases = [
            {"id": "Bad"}, {"path": "relative.wav"},
            {"sha256": "z" * 64}, {"identity_verified": "unknown"},
            {"allowed_uses": "vocal-replace"}, {"role": "question"},
            {"root_midi": True},
        ]
        for over in bad_phrases:
            request = self.make_request(phrases=("a",))
            request["phrases"][0].update(over)
            with self.subTest(over=over):
                with self.assertRaises(ValueError):
                    cw.submit(request, self.jobs)

    def test_phrase_file_missing_or_wrong_sha_awaits_confirmation(self):
        for index, over in enumerate([{'path': str(self.sources / 'missing.wav')},
                                       {'sha256': '0' * 64}]):
            request = self.make_request(phrases=('a',))
            request['request_id'] = f'missing-source-{index}'
            request['phrases'][0].update(over)
            job = Path(cw.submit(request, self.jobs)['job'])
            self.assertEqual(cw.run(job)['status'], 'awaiting_confirmation')
            self.assertFalse((job / 'review').exists())

    def test_edit_expected_hash_must_match_parent(self):
        request = self.make_request(
            phrases=("a",), expected_parent_mix_sha256="0" * 64)
        with self.assertRaisesRegex(ValueError, "parent|mix"):
            cw.submit(request, self.jobs)

    def test_jobs_root_inside_parent_rejected(self):
        with self.assertRaisesRegex(ValueError, "inside|protected|保护"):
            cw.submit(self.make_request(), self.parent / "jobs")

    def test_jobs_root_inside_phrase_source_rejected(self):
        with self.assertRaisesRegex(ValueError, "inside|protected|保护"):
            cw.submit(self.make_request(), self.sources / "jobs")


class SubmitIdempotencyTests(WorkflowBase):
    def test_same_request_returns_original_job(self):
        first = self.submit()
        second = cw.submit(self.make_request(), self.jobs)
        self.assertEqual(second["job"], first["job"])
        self.assertEqual(second["status"], "queued")
        dirs = [p for p in self.jobs.iterdir() if p.is_dir()]
        self.assertEqual(len(dirs), 1)

    def test_same_id_different_content_rejected(self):
        first = self.submit()
        changed = self.make_request(budget={"max_candidates": 1,
                                            "max_attempts": 6})
        with self.assertRaisesRegex(ValueError, "different|differ|content|内容"):
            cw.submit(changed, self.jobs)
        # the original job is untouched and still queued
        self.assertEqual(cw.status(Path(first["job"]))["status"], "queued")

    def test_request_json_is_immutable(self):
        first = self.submit()
        request_path = Path(first["job"]) / "request.json"
        mode = request_path.stat().st_mode & 0o777
        self.assertEqual(mode, 0o444)


class RunLifecycleTests(WorkflowBase):
    def test_submit_run_awaiting_review_with_two_families(self):
        job, result = self.run_to_review()
        self.assertEqual(len(result["candidates"]), 2)
        ids = {c["phrase_id"] for c in result["candidates"]}
        self.assertEqual(ids, {"answer-a", "answer-b"})
        families = {c["family_id"] for c in result["candidates"]}
        self.assertEqual(families, {"fam-a", "fam-b"})
        for c in result["candidates"]:
            child = job / c["path"]
            checked = validate_song(child)
            self.assertEqual(checked["mix_sha256"], c["mix_sha256"])
            for name in ("locked_edit_recipe.json", "locked-edit.json",
                         "protection.json"):
                self.assertTrue((child / name).is_file())
        mapping = json.loads((job / "review/private/mapping.json").read_text())
        self.assertEqual(
            {entry["id"] for entry in mapping["assignment"].values()},
            {"parent", "answer-a", "answer-b"})

    def test_parent_preserved_bytes_and_mtimes(self):
        before = self.snapshot(self.parent)
        job, _ = self.run_to_review()
        self.assertEqual(self.snapshot(self.parent), before)
        br.record_decision(job / "review", "tie", "preserve-1")
        cw.finish(job)
        self.assertEqual(self.snapshot(self.parent), before)

    def test_repeated_run_creates_no_new_attempts_or_changes(self):
        job, first = self.run_to_review()
        review_snap = self.snapshot(job / "review")
        candidates_snap = self.snapshot(job / "candidates")
        attempts_before = sorted(p.name for p in (job / "attempts").iterdir())
        second = cw.run(job)
        self.assertEqual(second["status"], "awaiting_review")
        self.assertEqual(second["attempt_count"], first["attempt_count"])
        self.assertEqual(self.snapshot(job / "review"), review_snap)
        self.assertEqual(self.snapshot(job / "candidates"), candidates_snap)
        self.assertEqual(sorted(p.name for p in (job / "attempts").iterdir()),
                         attempts_before)

    def test_status_read_does_not_mutate(self):
        submitted = self.submit()
        job = Path(submitted["job"])
        state_path = job / "state.json"
        before = (state_path.read_bytes(), state_path.stat().st_mtime_ns)
        result = cw.status(job)
        self.assertEqual(result["status"], "queued")
        self.assertEqual(result["job"], str(job))
        self.assertEqual((state_path.read_bytes(), state_path.stat().st_mtime_ns),
                         before)


class DecisionFinishTests(WorkflowBase):
    def test_neither_completes_without_export(self):
        job, _ = self.run_to_review()
        br.record_decision(job / "review", "neither", "vote-neither-1")
        result = cw.finish(job)
        self.assertEqual(result["status"], "completed")
        self.assertFalse(result["keep"])
        self.assertIsNone(result["delivery"])
        self.assertFalse((job / "delivery").exists())

    def test_tie_completes_without_export(self):
        job, _ = self.run_to_review()
        br.record_decision(job / "review", "tie", "vote-tie-1")
        result = cw.finish(job)
        self.assertEqual(result["status"], "completed")
        self.assertFalse(result["keep"])
        self.assertFalse((job / "delivery").exists())

    @unittest.skipUnless(HAS_TEMPLATES, "Ableton Live templates unavailable")
    def test_keep_candidate_exports_frozen_child(self):
        job, _ = self.run_to_review()
        label = self.label_for(job, "answer-a")
        decision = br.record_decision(job / "review", label, "vote-keep-a-1")
        self.assertTrue(decision["keep"])
        result = cw.finish(job)
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["keep"])
        delivery = job / result["delivery"]["path"]
        self.assertTrue((delivery / result["delivery"]["als"]).is_file())
        manifest = json.loads((delivery / "export_manifest.json").read_text())
        self.assertEqual(len(manifest["media"]), 3)
        recipe_names = manifest["recipe_files"].keys()
        self.assertIn("locked-edit.json", recipe_names)
        self.assertIn("protection.json", recipe_names)
        self.assertIn("sources/answer-a.wav", recipe_names)

    @unittest.skipUnless(HAS_TEMPLATES, "keep parent needs templates")
    def test_keep_parent_exports_parent_song(self):
        job, _ = self.run_to_review()
        label = self.label_for(job, "parent")
        br.record_decision(job / "review", label, "vote-keep-parent-1")
        result = cw.finish(job)
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["keep"])
        self.assertTrue((job / "delivery").is_dir())

    @unittest.skipUnless(HAS_TEMPLATES, "repeat finish needs templates")
    def test_repeated_finish_verifies_delivery_idempotently(self):
        job, _ = self.run_to_review()
        label = self.label_for(job, "answer-b")
        br.record_decision(job / "review", label, "vote-keep-b-1")
        first = cw.finish(job)
        delivery_snap = self.snapshot(job / "delivery")
        second = cw.finish(job)
        self.assertEqual(second["status"], "completed")
        self.assertEqual(second["delivery"]["als_sha256"],
                         first["delivery"]["als_sha256"])
        self.assertEqual(self.snapshot(job / "delivery"), delivery_snap)

    def test_finish_without_vote_waits(self):
        job, _ = self.run_to_review()
        result = cw.finish(job)
        self.assertEqual(result["status"], "awaiting_review")
        self.assertFalse((job / "delivery").exists())


class StaleInputTests(WorkflowBase):
    def test_changed_parent_stem_stops_run(self):
        submitted = self.submit()
        job = Path(submitted["job"])
        stem = self.parent / "stems" / "voice.wav"
        audio, sr = sf.read(str(stem), always_2d=True)
        sf.write(stem, audio * 0.97, sr, subtype="PCM_24")
        result = cw.run(job)
        self.assertEqual(result["status"], "failed")
        self.assertIn("parent", result["reason"])
        self.assertFalse((job / "delivery").exists())
        self.assertEqual(cw.run(job)["status"], "failed")

    def test_changed_phrase_bytes_stops_run(self):
        submitted = self.submit()
        job = Path(submitted["job"])
        tone_wav(self.sources / "phrase523.wav", 880)
        result = cw.run(job)
        self.assertEqual(result["status"], "failed")
        self.assertRegex(result["reason"], r"phrase|sha|hash")
        self.assertEqual(result["candidates"], [])
        self.assertFalse((job / "review").exists())

    def test_changed_parent_mix_only_stops_run(self):
        submitted = self.submit()
        job = Path(submitted["job"])
        mix = self.parent / "full_mix.wav"
        audio, sr = sf.read(str(mix), always_2d=True)
        sf.write(mix, audio * 0.99, sr, subtype="PCM_24")
        result = cw.run(job)
        self.assertEqual(result["status"], "failed")
        self.assertFalse((job / "delivery").exists())


class InvalidMaterialTests(WorkflowBase):
    def test_same_family_as_current_awaiting_confirmation(self):
        request = self.make_request(phrases=("a",))
        request["phrases"][0]["family_id"] = "fam-old"
        submitted = cw.submit(request, self.jobs)
        result = cw.run(Path(submitted["job"]))
        self.assertEqual(result["status"], "awaiting_confirmation")
        self.assertRegex(result["reason"], r"family|famil")
        self.assertEqual(result["candidates"], [])
        self.assertFalse((Path(submitted["job"]) / "review").exists())

    def test_purpose_not_in_allowed_uses_awaiting_confirmation(self):
        request = self.make_request(phrases=("a",))
        request["phrases"][0]["allowed_uses"] = ["other-purpose"]
        submitted = cw.submit(request, self.jobs)
        result = cw.run(Path(submitted["job"]))
        self.assertEqual(result["status"], "awaiting_confirmation")
        self.assertRegex(result["reason"], r"allowed_uses|purpose|use")
        self.assertEqual(result["candidates"], [])

    def test_independent_material_not_stopped_by_one_failure(self):
        request = self.make_request()
        request["phrases"][0]["family_id"] = "fam-old"  # fails; answer-b valid
        submitted = cw.submit(request, self.jobs)
        result = cw.run(Path(submitted["job"]))
        self.assertEqual(result["status"], "awaiting_review")
        self.assertEqual([c["phrase_id"] for c in result["candidates"]],
                         ["answer-b"])
        self.assertEqual(result["attempt_count"], 2)


class BudgetTests(WorkflowBase):
    def test_max_candidates_one_stops_after_first(self):
        request = self.make_request(
            budget={"max_candidates": 1, "max_attempts": 6})
        submitted = cw.submit(request, self.jobs)
        result = cw.run(Path(submitted["job"]))
        self.assertEqual(result["status"], "awaiting_review")
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["attempt_count"], 1)
        mapping = json.loads(
            (Path(submitted["job"]) / "review/private/mapping.json").read_text())
        self.assertEqual(
            {entry["id"] for entry in mapping["assignment"].values()},
            {"parent", "answer-a"})

    def test_attempt_budget_exhausted_reports_reason(self):
        request = self.make_request(
            budget={"max_candidates": 2, "max_attempts": 1})
        request["phrases"][0]["family_id"] = "fam-old"  # first attempt fails
        submitted = cw.submit(request, self.jobs)
        result = cw.run(Path(submitted["job"]))
        self.assertEqual(result["status"], "awaiting_confirmation")
        self.assertRegex(result["reason"], r"attempt|budget|预算")
        self.assertEqual(result["candidates"], [])


class LockConflictTests(WorkflowBase):
    def test_second_runner_reports_already_running(self):
        submitted = self.submit()
        job = Path(submitted["job"])
        lock_path = job / cw.LOCK_NAME
        lock_file = open(lock_path, "a+")
        try:
            import fcntl
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            result = cw.run(job)
            self.assertTrue(result["locked"])
            self.assertEqual(result["status"], "queued")
            self.assertRegex(result["note"], r"running|lock|运行")
            # finish must also refuse while the long lock is held
            self.assertTrue(cw.finish(job)["locked"])
        finally:
            lock_file.close()
        self.assertEqual(cw.status(job)["status"], "queued")


class CancelTests(WorkflowBase):
    def test_cancel_before_run(self):
        submitted = self.submit()
        job = Path(submitted["job"])
        result = cw.cancel(job)
        self.assertEqual(result["status"], "cancelled")
        self.assertTrue((job / "cancel.json").is_file())
        run_result = cw.run(job)
        self.assertEqual(run_result["status"], "cancelled")
        # The reserved candidates directory remains, but it publishes nothing.
        self.assertEqual(
            [p for p in (job / "candidates").iterdir() if p.is_dir()], [])
        finish_result = cw.finish(job)
        self.assertEqual(finish_result["status"], "cancelled")
        self.assertFalse((job / "delivery").exists())

    def test_cancel_during_render_via_wrapper(self):
        submitted = self.submit()
        job = Path(submitted["job"])
        holder = {}

        def hook(phrase_id, invoke):
            def worker():
                try:
                    holder["result"] = invoke()  # real locked_edit engine
                except BaseException as exc:  # surface to the main thread
                    holder["exc"] = exc
            cw.cancel(job)
            thread = threading.Thread(target=worker)
            thread.start()
            thread.join(timeout=60)
            if "exc" in holder:
                raise holder["exc"]
            return holder.get("result")

        cw._RENDER_HOOK = hook
        result = cw.run(job)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["candidates"], [])
        self.assertFalse((job / "review").exists())
        self.assertFalse((job / "delivery").exists())
        attempts = sorted(p.name for p in (job / "attempts").iterdir())
        records = [json.loads((job / "attempts" / n).read_text())
                   for n in attempts]
        self.assertTrue(any(r["status"] == "cancelled_unpublished"
                            for r in records))
        # no automatic resume of a cancelled job
        self.assertEqual(cw.run(job)["status"], "cancelled")


class RecoveryTests(WorkflowBase):
    def test_interrupted_attempt_marked_and_orphan_child_recovered(self):
        submitted = self.submit()
        job = Path(submitted["job"])
        cw.write_attempt(job, {
            "attempt": 1, "phrase_id": "answer-a", "family_id": "fam-a",
            "status": "rendering", "started_at": cw.utcnow(),
            "finished_at": None, "error": None, "candidate": None})
        # A real completed render whose candidate record was never written.
        le.render_locked_edit(self.parent, job / "candidates/answer-b",
                              self.edit_dict(), self.phrase_dict("b"))
        result = cw.run(job)
        self.assertEqual(result["status"], "awaiting_review")
        records = {r["phrase_id"]: r for r in
                   (json.loads((job / "attempts" / n).read_text())
                    for n in sorted(p.name for p in (job / "attempts").iterdir()))}
        self.assertEqual(records["answer-a"]["status"], "interrupted")
        self.assertEqual(records["answer-b"]["status"], "rendered_recovered")
        self.assertEqual([c["phrase_id"] for c in result["candidates"]],
                         ["answer-b"])
        self.assertFalse((job / "candidates/answer-a").exists())
        mapping = json.loads((job / "review/private/mapping.json").read_text())
        self.assertEqual(
            {entry["id"] for entry in mapping["assignment"].values()},
            {"parent", "answer-b"})

    @unittest.skipUnless(HAS_TEMPLATES, "recovered child export needs templates")
    def test_recovered_candidate_votes_and_exports(self):
        submitted = self.submit()
        job = Path(submitted["job"])
        cw.write_attempt(job, {
            "attempt": 1, "phrase_id": "answer-a", "family_id": "fam-a",
            "status": "rendering", "started_at": cw.utcnow(),
            "finished_at": None, "error": None, "candidate": None})
        le.render_locked_edit(self.parent, job / "candidates/answer-b",
                              self.edit_dict(), self.phrase_dict("b"))
        cw.run(job)
        label = self.label_for(job, "answer-b")
        br.record_decision(job / "review", label, "vote-recovered-1")
        result = cw.finish(job)
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["keep"])
        self.assertTrue((job / "delivery").is_dir())


class CorruptReviewTests(WorkflowBase):
    def test_corrupt_mapping_blocks_finish_and_export(self):
        job, _ = self.run_to_review()
        label = self.label_for(job, "answer-a")
        br.record_decision(job / "review", label, "vote-before-corrupt-1")
        (job / "review/private/mapping.json").write_text("{not json")
        finish_result = cw.finish(job)
        self.assertEqual(finish_result["status"], "failed")
        self.assertRegex(finish_result["reason"], r"review|mapping|评审")
        self.assertFalse((job / "delivery").exists())
        run_result = cw.run(job)
        self.assertEqual(run_result["status"], "failed")

    def test_review_bound_to_other_versions_is_rejected(self):
        job, _ = self.run_to_review()
        mapping_path = job / "review/private/mapping.json"
        mapping = json.loads(mapping_path.read_text())
        for entry in mapping["assignment"].values():
            entry["id"] = "stranger-version"
        mapping_path.write_text(json.dumps(mapping) + "\n")
        result = cw.run(job)
        self.assertEqual(result["status"], "failed")
        self.assertFalse((job / "delivery").exists())


class LoopbackHttpTests(WorkflowBase):
    def start_server(self, job: Path):
        handler = br.make_handler(job / "review",
                                  on_decision=cw.review_callback(job))
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop():
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.addCleanup(stop)
        return server.server_address[1]

    def post_vote(self, port: int, payload: dict):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
        conn.request("POST", "/api/decision",
                     body=json.dumps(payload).encode(),
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        body = json.loads(resp.read().decode())
        conn.close()
        return resp.status, body

    def test_loopback_tie_callback_completes_job(self):
        job, _ = self.run_to_review()
        port = self.start_server(job)
        status, body = self.post_vote(
            port, {"choice": "tie", "request_id": "http-tie-1"})
        self.assertEqual(status, 200)
        self.assertEqual(body["callback"]["status"], "ok")
        self.assertEqual(cw.status(job)["status"], "completed")
        self.assertFalse((job / "delivery").exists())

    @unittest.skipUnless(HAS_TEMPLATES, "loopback keep needs templates")
    def test_loopback_keep_callback_finishes_delivery(self):
        job, _ = self.run_to_review()
        port = self.start_server(job)
        mapping = json.loads(
            (job / "review/private/mapping.json").read_text())
        candidate_label = next(
            label for label, entry in mapping["assignment"].items()
            if entry["id"] == "answer-a")
        status, body = self.post_vote(
            port, {"choice": candidate_label, "request_id": "http-keep-1"})
        self.assertEqual(status, 200)
        self.assertEqual(body["callback"]["status"], "ok")
        final = cw.status(job)
        self.assertEqual(final["status"], "completed")
        self.assertTrue(final["keep"])
        self.assertTrue((job / "delivery").is_dir())

    def test_loopback_after_cancel_records_vote_but_no_side_effect(self):
        job, _ = self.run_to_review()
        cw.cancel(job)
        port = self.start_server(job)
        status, body = self.post_vote(
            port, {"choice": "A", "request_id": "http-cancelled-1"})
        self.assertEqual(status, 200)
        # The decision is persisted, but the callback reports failure and no
        # export ever happens for a cancelled job.
        self.assertTrue(body["keep"])
        self.assertEqual(body["callback"]["status"], "failed")
        self.assertIsNotNone(br.load_decision(job / "review"))
        self.assertEqual(cw.status(job)["status"], "cancelled")
        self.assertFalse((job / "delivery").exists())


class WorkflowQualityTests(WorkflowBase):
    def keep(self, job):
        return br.record_decision(job / 'review', self.label_for(job, 'answer-a'),
                                  'technical-test-keep')

    def test_request_id_prefix_does_not_alias_another_request(self):
        request = self.make_request()
        request['request_id'] = 'wf-demo-1-long'
        first = self.submit(request)
        second = self.submit()
        self.assertNotEqual(first['job'], second['job'])

    def test_unconfirmed_identity_skips_only_that_material(self):
        request = self.make_request()
        request['phrases'][0]['identity_verified'] = False
        job, result = self.run_to_review(request)
        self.assertEqual([c['phrase_id'] for c in result['candidates']], ['answer-b'])

    def test_missing_source_skips_only_that_material(self):
        request = self.make_request()
        request['phrases'][0]['path'] = str(self.sources / 'missing.wav')
        _, result = self.run_to_review(request)
        self.assertEqual([c['phrase_id'] for c in result['candidates']], ['answer-b'])

    def test_request_file_drift_cannot_change_execution(self):
        job = Path(self.submit()['job'])
        path = job / 'request.json'
        request = json.loads(path.read_text())
        request['edit']['start_beat'] = 0
        path.chmod(0o644)
        path.write_text(json.dumps(request))
        with self.assertRaisesRegex(cw.WorkflowError, 'request.*changed'):
            cw.run(job)
        self.assertFalse((job / 'review').exists())

    def test_changed_audition_audio_blocks_keep_delivery(self):
        job, _ = self.run_to_review()
        self.keep(job)
        wav = job / 'review/public/A.wav'
        wav.write_bytes(wav.read_bytes()[:-4])
        result = cw.finish(job)
        self.assertEqual(result['status'], 'failed')
        self.assertFalse((job / 'delivery').exists())

    def test_changed_recipe_blocks_keep_delivery(self):
        job, _ = self.run_to_review()
        self.keep(job)
        recipe = job / 'candidates/answer-a/locked_edit_recipe.json'
        obj = json.loads(recipe.read_text())
        obj['extra'] = 'changed after review'
        recipe.write_text(json.dumps(obj))
        self.assertEqual(cw.finish(job)['status'], 'failed')
        self.assertFalse((job / 'delivery').exists())

    @unittest.skipUnless(HAS_TEMPLATES, 'Live templates unavailable')
    def test_completed_delivery_detects_source_midi_drift(self):
        job, _ = self.run_to_review()
        self.keep(job)
        self.assertEqual(cw.finish(job)['status'], 'completed')
        next((job / 'delivery/Source/midi').glob('*.mid')).write_bytes(b'broken')
        self.assertEqual(cw.finish(job)['status'], 'failed')

    @unittest.skipUnless(HAS_TEMPLATES, 'Live templates unavailable')
    def test_export_completed_before_state_write_is_recoverable(self):
        job, _ = self.run_to_review()
        self.keep(job)
        cw.ableton_export.export_song(job / 'candidates/answer-a', job / 'delivery',
                                      set_name=cw.SET_NAME)
        before = self.snapshot(job / 'delivery')
        result = cw.finish(job)
        self.assertEqual(result['status'], 'completed', result.get('reason'))
        self.assertEqual(before, self.snapshot(job / 'delivery'))

    @unittest.skipUnless(HAS_TEMPLATES, 'Live templates unavailable')
    def test_export_failure_can_retry_same_saved_decision(self):
        from unittest.mock import patch
        job, _ = self.run_to_review()
        self.keep(job)
        with patch.object(cw.ableton_export, 'export_song', side_effect=OSError('disk busy')):
            first = cw.finish(job)
        self.assertEqual(first['status'], 'awaiting_review')
        self.assertIn('export failed', first['reason'])
        self.assertEqual(cw.finish(job)['status'], 'completed')

    @unittest.skipUnless(HAS_TEMPLATES, 'Live templates unavailable')
    def test_cancel_during_export_never_publishes_completion(self):
        from unittest.mock import patch
        job, _ = self.run_to_review()
        self.keep(job)
        exporter = cw.ableton_export.export_song
        def cancelled_export(*args, **kwargs):
            result = exporter(*args, **kwargs)
            cw.cancel(job)
            return result
        with patch.object(cw.ableton_export, 'export_song', cancelled_export):
            result = cw.finish(job)
        self.assertEqual(result['status'], 'cancelled')
        self.assertIsNone(result['delivery'])

    def test_interrupted_pending_folder_does_not_spend_another_attempt(self):
        job = Path(self.submit(self.make_request(budget={'max_attempts': 1}))['job'])
        cw.write_attempt(job, {'attempt': 1, 'phrase_id': 'answer-a', 'status': 'rendering'})
        (job / 'candidates/.answer-a.pending-test').mkdir()
        result = cw.run(job)
        self.assertEqual(result['status'], 'awaiting_confirmation')
        self.assertEqual(result['attempt_count'], 1)


if __name__ == "__main__":
    unittest.main()
