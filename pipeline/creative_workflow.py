"""Resumable local creative workflow around locked_edit / blind_review.

A job is an immutable request plus JSON state on disk. The parent song and the
phrase sources are hash-bound at submit time; render work is performed by the
real locked_edit engine, reviewed through the real blind_review artifact, and
exported through ableton_export.export_song. This module adds orchestration
only: bounded attempts, an exclusive job lock, a human review gate, and
delivery verification. It never invents decisions, taste judgments, lyrics
recognition, loudness, or Ableton Live verification.

States: queued / running / awaiting_confirmation / awaiting_review /
completed / failed / cancelled.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
import numpy as np
import soundfile as sf

# The sibling modules use flat ("from song import ...") imports; keep this
# directory importable whether the module is imported as package member or file.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import ableton_export  # noqa: E402
import blind_review as br  # noqa: E402
import locked_edit  # noqa: E402

VERSION = 1
LOCK_NAME = ".job.lock"
CANCEL_NAME = "cancel.json"
REQUEST_NAME = "request.json"
STATE_NAME = "state.json"
SET_NAME = "locked_workflow"
DEFAULT_MAX_CANDIDATES = 2
DEFAULT_MAX_ATTEMPTS = 6
MAX_REASON = 1000

REQUEST_ID_RE = re.compile(r"[a-z][a-z0-9_-]{0,63}")
ID_RE = re.compile(r"[a-z][a-z0-9_-]*")
HASH_RE = re.compile(r"[a-f0-9]{64}")
STATES = ("queued", "running", "awaiting_confirmation", "awaiting_review",
          "completed", "failed", "cancelled")
# Re-entering one of these performs no new work.
STICKY_STATES = ("awaiting_confirmation", "completed", "failed")

# Test seam: wraps the real render callable (phrase_id, invoke) -> invoke result.
_RENDER_HOOK = None


class WorkflowError(Exception):
    """Actionable workflow-level failure."""


# --------------------------------------------------------------------------
# Basic helpers
# --------------------------------------------------------------------------

def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _sha(path) -> str:
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _is_num(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(float(value)))


def _is_text(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _error_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:MAX_REASON]


def _write_json(path, obj) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix="." + path.name + ".",
                                    dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(obj, stream, ensure_ascii=False, indent=2,
                      allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def write_attempt(job, record: dict) -> None:
    """Persist one attempt record (also used to seed recovery scenarios)."""
    no = int(record["attempt"])
    _write_json(Path(job) / "attempts" / f"attempt-{no:03d}.json", record)


def _write_state(job, state: dict) -> None:
    state["updated_at"] = utcnow()
    _write_json(Path(job) / STATE_NAME, state)


def _read_attempts(job) -> list[dict]:
    directory = Path(job) / "attempts"
    if not directory.is_dir():
        return []
    return [json.loads(p.read_text())
            for p in sorted(directory.glob("attempt-*.json"))]


def _next_attempt_no(job) -> int:
    return max((a["attempt"] for a in _read_attempts(job)), default=0) + 1


def _load_job(job):
    job = Path(job).expanduser().resolve()
    request_path = job / REQUEST_NAME
    state_path = job / STATE_NAME
    if not request_path.is_file() or not state_path.is_file():
        raise FileNotFoundError(f"not a valid job directory: {job}")
    request = json.loads(request_path.read_text())
    state = json.loads(state_path.read_text())
    if state.get('request_sha256') != _fingerprint(request):
        raise WorkflowError('request contents changed after submit')
    return state, request


def _view(job, state: dict) -> dict:
    result = dict(state)
    result["job"] = str(Path(job).resolve())
    return result


def _locked_view(job, state: dict) -> dict:
    result = _view(job, state)
    result["locked"] = True
    result["note"] = "another process currently holds the job run lock"
    return result


def _fail(job, state: dict, reason) -> dict:
    state["status"] = "failed"
    state["reason"] = str(reason)[:MAX_REASON]
    _write_state(job, state)
    return _view(job, state)


# --------------------------------------------------------------------------
# Locking and cancellation
# --------------------------------------------------------------------------

class _JobLock:
    """Nonblocking exclusive fcntl lock; released automatically on exit."""

    def __init__(self, job):
        self.path = Path(job) / LOCK_NAME
        self.file = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = open(self.path, "a+")
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            stream.close()
            return False
        self.file = stream
        return True

    def release(self) -> None:
        if self.file is not None:
            fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
            self.file.close()
            self.file = None


def _is_cancelled(job) -> bool:
    return (Path(job) / CANCEL_NAME).is_file()


def _write_cancel_marker(job) -> None:
    # Separate atomic marker; never acquires the long render lock.
    _write_json(Path(job) / CANCEL_NAME,
                 {"created_at": utcnow(), "source": "user"})


# --------------------------------------------------------------------------
# Request schema validation
# --------------------------------------------------------------------------

def _validate_edit(edit) -> None:
    if not isinstance(edit, dict):
        raise ValueError("request.edit must be an object")
    track_id = edit.get("track_id")
    if not isinstance(track_id, str) or not ID_RE.fullmatch(track_id):
        raise ValueError("edit.track_id must match [a-z][a-z0-9_-]*")
    mix_hash = edit.get("expected_parent_mix_sha256")
    if not isinstance(mix_hash, str) or not HASH_RE.fullmatch(mix_hash.lower()):
        raise ValueError("edit.expected_parent_mix_sha256 must be 64 hex chars")
    if not _is_text(edit.get("current_family_id")):
        raise ValueError("edit.current_family_id must be a nonempty string")
    if edit.get("identity_verified") not in (None, False, True):
        raise ValueError("edit.identity_verified must be boolean")
    if not _is_text(edit.get("purpose")):
        raise ValueError("edit.purpose must be a nonempty string")
    for name in ("start_beat", "end_beat"):
        if name in edit and not _is_num(edit[name]):
            raise ValueError(f"edit.{name} must be a finite non-boolean number")


def _validate_phrases(phrases) -> None:
    if not isinstance(phrases, list) or not phrases:
        raise ValueError("request.phrases must be a nonempty list")
    ids: set[str] = set()
    families: set[str] = set()
    for phrase in phrases:
        if not isinstance(phrase, dict):
            raise ValueError("each phrase must be an object")
        pid = phrase.get("id")
        if not isinstance(pid, str) or not ID_RE.fullmatch(pid):
            raise ValueError(f"phrase.id must match [a-z][a-z0-9_-]*: {pid!r}")
        if pid in ids:
            raise ValueError(f"duplicate phrase id: {pid}")
        ids.add(pid)
        path = phrase.get("path")
        if not isinstance(path, str) or not path.strip() or not Path(path).is_absolute():
            raise ValueError(f"phrase.path must be an absolute path string: {pid}")
        sha = phrase.get("sha256")
        if not isinstance(sha, str) or not HASH_RE.fullmatch(sha.lower()):
            raise ValueError(f"phrase.sha256 must be 64 hex chars: {pid}")
        family = phrase.get("family_id")
        if not _is_text(family):
            raise ValueError(f"phrase.family_id must be a nonempty string: {pid}")
        family = family.strip()
        if family in families:
            raise ValueError(f"duplicate phrase family_id: {family}")
        families.add(family)
        if phrase.get("identity_verified") not in (None, False, True):
            raise ValueError(f"phrase.identity_verified must be boolean: {pid}")
        uses = phrase.get("allowed_uses")
        if not isinstance(uses, list) or not all(isinstance(u, str) for u in uses):
            raise ValueError(f"phrase.allowed_uses must be a list of strings: {pid}")
        if "root_midi" in phrase:
            root = phrase["root_midi"]
            if isinstance(root, bool) or not isinstance(root, int) or not 0 <= root <= 127:
                raise ValueError(f"phrase.root_midi must be an integer in [0,127]: {pid}")
        if phrase.get("role") != "answer":
            raise ValueError(f"phrase.role must be 'answer': {pid}")


def _validate_budget(budget) -> dict:
    if not isinstance(budget, dict):
        raise ValueError("budget must be an object")
    if set(budget) - {"max_candidates", "max_attempts"}:
        raise ValueError("budget allows only max_candidates and max_attempts")
    for name, low, high in (("max_candidates", 1, 2),
                            ("max_attempts", 1, 6)):
        if name not in budget:
            continue
        value = budget[name]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f"budget.{name} must be an integer in [{low},{high}]")


def _validate_request_schema(request) -> None:
    if not isinstance(request, dict):
        raise ValueError("request must be a JSON object")
    extra = set(request) - {"version", "request_id", "parent", "edit",
                            "phrases", "budget"}
    if extra:
        raise ValueError(f"unknown request keys: {sorted(extra)}")
    if request.get("version") != VERSION:
        raise ValueError("request.version must be 1")
    request_id = request.get("request_id")
    if not isinstance(request_id, str) or not REQUEST_ID_RE.fullmatch(request_id):
        raise ValueError("request.request_id must match [a-z][a-z0-9_-]{0,63}")
    parent = request.get("parent")
    if not isinstance(parent, str) or not parent.strip() or not Path(parent).is_absolute():
        raise ValueError("request.parent must be an absolute path string")
    _validate_edit(request.get("edit"))
    _validate_phrases(request.get("phrases"))
    if "budget" in request:
        _validate_budget(request["budget"])


def _fingerprint(request: dict) -> str:
    canonical = json.dumps(request, sort_keys=True, ensure_ascii=False,
                           separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# submit
# --------------------------------------------------------------------------

def submit(request: dict, jobs_root) -> dict:
    """Validate and reserve a new job; identical submissions are idempotent."""
    _validate_request_schema(request)
    jobs_root = Path(jobs_root).expanduser().resolve()
    parent = Path(request["parent"]).expanduser().resolve()
    if not parent.is_dir():
        raise FileNotFoundError(f"parent not found: {parent}")
    try:
        checked = ableton_export.validate_song(parent)
    except Exception as exc:
        raise ValueError(f"parent fails validate_song: {exc}") from exc
    if request["edit"]["expected_parent_mix_sha256"].lower() != checked["mix_sha256"]:
        raise ValueError(
            "edit.expected_parent_mix_sha256 does not match the current parent mix")

    phrase_binding = []
    for phrase in request["phrases"]:
        source = Path(phrase["path"]).expanduser().resolve()
        error = None
        if not source.is_file():
            error = f"phrase source file missing: {source}"
        elif _sha(source) != phrase["sha256"].lower():
            error = f"phrase sha256 mismatch: {phrase['id']}"
        phrase_binding.append(
            {"id": phrase["id"], "path": str(source),
             "sha256": phrase["sha256"].lower(),
             "family_id": phrase["family_id"].strip(), "error": error})

    # Location safety: the job tree must not be able to mutate protected input.
    if jobs_root == parent or jobs_root.is_relative_to(parent):
        raise ValueError("jobs_root must not be inside the protected parent")
    for entry in phrase_binding:
        source_dir = Path(entry["path"]).parent
        if jobs_root == source_dir or jobs_root.is_relative_to(source_dir):
            raise ValueError(
                "jobs_root must not be inside a phrase source location: "
                f"{source_dir}")
    jobs_root.mkdir(parents=True, exist_ok=True)
    if not jobs_root.is_dir():
        raise ValueError(f"jobs_root is not a directory: {jobs_root}")

    fingerprint = _fingerprint(request)
    job_dir = jobs_root / request['request_id']

    def existing_match(directory: Path):
        try:
            earlier = json.loads((directory / REQUEST_NAME).read_text())
        except FileNotFoundError as exc:
            raise ValueError(f"existing job is missing request.json: {directory}") from exc
        except ValueError as exc:
            raise ValueError(f"existing job request is unreadable: {directory}") from exc
        if _fingerprint(earlier) == fingerprint:
            state, _ = _load_job(directory)
            return _view(directory, state)
        raise ValueError(
            f"request_id {request['request_id']} already exists with different "
            f"content: {directory}")

    try:
        job_dir.mkdir()
    except FileExistsError:
        return existing_match(job_dir)

    (job_dir / "attempts").mkdir()
    (job_dir / "candidates").mkdir()
    now = utcnow()
    binding = {
        "path": str(parent),
        "mix_sha256": checked["mix_sha256"],
        "manifest_sha256": checked["manifest_sha256"],
        "score_sha256": checked["score_sha256"],
        "tracks": [{"id": t["id"], "sha256": t["sha256"],
                    "midi_sha256": t["midi_sha256"],
                    "source_sha256": t["source_sha256"]}
                   for t in checked["tracks"]],
        "phrases": phrase_binding}
    budget = {"max_candidates": DEFAULT_MAX_CANDIDATES,
              "max_attempts": DEFAULT_MAX_ATTEMPTS}
    budget.update(request.get("budget", {}))
    state = {"version": VERSION, "request_id": request["request_id"],
             "request_sha256": fingerprint,
             "status": "queued", "reason": None,
             "created_at": now, "updated_at": now,
             "budget": budget, "binding": binding, "attempt_count": 0,
             "candidates": [], "review": None, "decision": None,
             "delivery": None, "keep": None}
    _write_json(job_dir / REQUEST_NAME, request)
    os.chmod(job_dir / REQUEST_NAME, 0o444)  # immutable request evidence
    _write_state(job_dir, state)
    return _view(job_dir, state)


# --------------------------------------------------------------------------
# Input verification
# --------------------------------------------------------------------------

def _verify_inputs(job, state, request) -> None:
    binding = state["binding"]
    parent = Path(binding["path"])
    if Path(request["parent"]).expanduser().resolve() != parent:
        raise WorkflowError("parent path no longer resolves to the submitted location")
    try:
        checked = ableton_export.validate_song(parent)
    except Exception as exc:
        raise WorkflowError(f"parent fails validation after submit: {exc}") from exc
    for key in ("mix_sha256", "manifest_sha256", "score_sha256"):
        if checked[key] != binding[key]:
            raise WorkflowError(f"parent {key} changed after submit")
    records = {t["id"]: t for t in checked["tracks"]}
    for bound in binding["tracks"]:
        record = records.get(bound["id"])
        if (record is None
                or record["sha256"] != bound["sha256"]
                or record["midi_sha256"] != bound["midi_sha256"]
                or record["source_sha256"] != bound["source_sha256"]):
            raise WorkflowError(
                f"parent track {bound['id']} (stem/MIDI/source) changed after submit")
    by_id = {p["id"]: p for p in binding["phrases"]}
    for phrase in request["phrases"]:
        bound = by_id[phrase["id"]]
        if bound.get('error'):
            continue  # This unusable input is a recorded per-source failure.
        source = Path(phrase["path"]).expanduser().resolve()
        if str(source) != bound["path"]:
            raise WorkflowError(f"phrase {phrase['id']} path changed after submit")
        if not source.is_file():
            raise WorkflowError(f"phrase {phrase['id']} is missing after submit")
        if _sha(source) != bound["sha256"]:
            raise WorkflowError(f"phrase {phrase['id']} bytes changed after submit")


# --------------------------------------------------------------------------
# Candidate verification and recovery
# --------------------------------------------------------------------------

def _phrase_for_id(request: dict, phrase_id: str) -> dict | None:
    return next((p for p in request["phrases"] if p["id"] == phrase_id), None)


def _verify_child(child, request: dict, phrase_id: str, binding: dict) -> dict:
    """Validate an existing child song and its binding to this request."""
    child = Path(child)
    phrase = _phrase_for_id(request, phrase_id)
    if phrase is None:
        raise ValueError(f"no request phrase with id {phrase_id}")
    checked = ableton_export.validate_song(child)
    recipe_path = child / "locked_edit_recipe.json"
    protection_path = child / "protection.json"
    if not recipe_path.is_file() or not protection_path.is_file():
        raise ValueError("child is missing locked_edit_recipe.json/protection.json")
    recipe = json.loads(recipe_path.read_text())
    if recipe.get("engine") != "stem_splice":
        raise ValueError("child recipe engine is not stem_splice")
    parent_record = recipe["parent"]
    if parent_record["path"] != binding["path"]:
        raise ValueError("child recipe is bound to another parent path")
    for name in ("mix_sha256", "manifest_sha256", "score_sha256"):
        if parent_record[name] != binding[name]:
            raise ValueError(f"child recipe is bound to another parent ({name})")
    if recipe["edit"] != request["edit"]:
        raise ValueError("child recipe edit differs from the request edit")
    recipe_phrase = recipe["phrase"]
    if (recipe_phrase["id"] != phrase["id"]
            or recipe_phrase["sha256"] != phrase["sha256"].lower()
            or recipe_phrase["family_id"] != phrase["family_id"].strip()):
        raise ValueError("child recipe phrase is not this request phrase")
    protection = json.loads(protection_path.read_text())
    if protection["child"]["mix_sha256"] != checked["mix_sha256"]:
        raise ValueError("protection record mix hash does not match child mix")
    if protection["phrase"]["id"] != phrase["id"]:
        raise ValueError("protection record is bound to another phrase")
    return checked


def _candidate_record(phrase: dict, relative: str, checked: dict,
                      recovered: bool) -> dict:
    child = Path(checked["song"])
    return {"phrase_id": phrase["id"],
            "family_id": phrase["family_id"].strip(),
            "path": relative,
            "mix_sha256": checked["mix_sha256"],
            "manifest_sha256": checked["manifest_sha256"],
            "score_sha256": checked["score_sha256"],
            "recipe_sha256": _sha(child / "locked_edit_recipe.json"),
            "protection_sha256": _sha(child / "protection.json"),
            "recovered": recovered}


def _verify_frozen_child(job, state, request, candidate):
    child = job / candidate['path']
    checked = _verify_child(child, request, candidate['phrase_id'], state['binding'])
    for key in ('mix_sha256', 'manifest_sha256', 'score_sha256'):
        if checked[key] != candidate[key]:
            raise WorkflowError(f'candidate {key} changed after rendering')
    for filename, key in [('locked_edit_recipe.json', 'recipe_sha256'),
                          ('protection.json', 'protection_sha256')]:
        if _sha(child / filename) != candidate[key]:
            raise WorkflowError(f'candidate {filename} changed after rendering')
    phrase = _phrase_for_id(request, candidate['phrase_id'])
    if _sha(child / 'sources' / f"{phrase['id']}.wav") != phrase['sha256'].lower():
        raise WorkflowError('collected replacement source changed')
    return checked


def _recover(job, state: dict, request: dict) -> bool:
    """Mark interrupted attempts and adopt recoverable orphan children."""
    changed = False
    attempts = _read_attempts(job)
    by_phrase = {a["phrase_id"]: a for a in attempts}

    for attempt in attempts:  # process death while a render was in progress
        if attempt["status"] == "rendering":
            attempt["status"] = "interrupted"
            attempt["finished_at"] = utcnow()
            attempt["error"] = "process ended while the render was in progress"
            write_attempt(job, attempt)
            changed = True

    for candidate in list(state["candidates"]):  # verify known candidates
        try:
            _verify_frozen_child(job, state, request, candidate)
        except Exception as exc:
            raise WorkflowError(
                f"recorded candidate {candidate['phrase_id']} no longer "
                f"verifies: {exc}") from exc

    candidates_dir = job / "candidates"
    recorded = {c["path"] for c in state["candidates"]}
    if candidates_dir.is_dir():
        for child in sorted(p for p in candidates_dir.iterdir() if p.is_dir()):
            if child.name.startswith('.'):
                continue  # Interrupted renderer staging is evidence, not a new attempt.
            relative = str(child.relative_to(job))
            if relative in recorded:
                continue
            phrase = _phrase_for_id(request, child.name)
            try:
                if phrase is None:
                    raise ValueError("directory name is not any request phrase id")
                checked = _verify_child(child, request, phrase["id"],
                                        state["binding"])
            except Exception as exc:
                raise WorkflowError(f'unmatched orphan candidate: {exc}') from exc
            earlier = by_phrase.get(phrase["id"])
            if earlier is not None and earlier["status"] in (
                    "rendering", "interrupted"):
                earlier["status"] = "rendered_recovered"
                earlier["finished_at"] = utcnow()
                earlier["candidate"] = relative
                earlier["error"] = None
                write_attempt(job, earlier)
            else:
                if len(_read_attempts(job)) >= state['budget']['max_attempts']:
                    raise WorkflowError('orphan candidate exceeds attempt budget')
                no = _next_attempt_no(job)
                write_attempt(job, {
                    "attempt": no, "phrase_id": phrase["id"],
                    "family_id": phrase["family_id"].strip(),
                    "status": "rendered_recovered", "started_at": utcnow(),
                    "finished_at": utcnow(), "error": None,
                    "candidate": relative})
            state["candidates"].append(
                _candidate_record(phrase, relative, checked, recovered=True))
            changed = True
    state["attempt_count"] = len(_read_attempts(job))
    return changed


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def _one_attempt(job, state, request, phrase):
    no = _next_attempt_no(job)
    record = {"attempt": no, "phrase_id": phrase["id"],
              "family_id": phrase["family_id"].strip(),
              "status": "rendering", "started_at": utcnow(),
              "finished_at": None, "error": None, "candidate": None}
    write_attempt(job, record)
    child_path = job / "candidates" / phrase["id"]

    def invoke():
        bound = next(p for p in state['binding']['phrases'] if p['id'] == phrase['id'])
        if bound.get('error'):
            raise WorkflowError(bound['error'])
        return locked_edit.render_locked_edit(
            state["binding"]["path"], child_path, request["edit"], phrase)

    try:
        if _RENDER_HOOK is not None:
            result = _RENDER_HOOK(phrase["id"], invoke)
        else:
            result = invoke()
    except BaseException as exc:
        record["status"] = "failed"
        record["finished_at"] = utcnow()
        record["error"] = _error_text(exc)
        write_attempt(job, record)
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        return None

    relative = f"candidates/{phrase['id']}"

    def publish_cancel():
        record["status"] = "cancelled_unpublished"
        record["finished_at"] = utcnow()
        record["candidate"] = relative if child_path.is_dir() else None
        write_attempt(job, record)
        return "cancelled"

    # Cancellation check after the renderer returns.
    if _is_cancelled(job):
        return publish_cancel()
    try:
        checked = _verify_child(child_path, request, phrase["id"],
                                state["binding"])
    except BaseException as exc:
        record["status"] = "failed"
        record["finished_at"] = utcnow()
        record["error"] = _error_text(exc)
        write_attempt(job, record)
        return None
    # Cancellation check again immediately before publication.
    if _is_cancelled(job):
        return publish_cancel()

    record["status"] = "rendered"
    record["finished_at"] = utcnow()
    record["candidate"] = relative
    write_attempt(job, record)
    return _candidate_record(phrase, relative, checked, recovered=False)


def _render_loop(job, state, request):
    notes: list[str] = []
    used_families = {c["family_id"] for c in state["candidates"]}
    for phrase in request["phrases"]:
        if _is_cancelled(job):
            return "cancelled", notes
        if len(state["candidates"]) >= state["budget"]["max_candidates"]:
            notes.append("candidate budget reached")
            break
        if len(_read_attempts(job)) >= state["budget"]["max_attempts"]:
            notes.append(
                f"attempt budget exhausted "
                f"({len(_read_attempts(job))}/{state['budget']['max_attempts']})")
            break
        already = _read_attempts(job)
        if any(a["phrase_id"] == phrase["id"] for a in already):
            continue  # never repeat a phrase that has an attempt record
        if phrase["family_id"].strip() in used_families:
            notes.append(f"{phrase['id']}: family already represented; skipped")
            continue
        try:  # inputs must still be exactly the submitted bytes
            _verify_inputs(job, state, request)
        except WorkflowError as exc:
            return "failed", str(exc)
        candidate = _one_attempt(job, state, request, phrase)
        if candidate == "cancelled":
            return "cancelled", notes
        if candidate is None:
            failed = next(a for a in reversed(_read_attempts(job))
                          if a["phrase_id"] == phrase["id"])
            notes.append(f"{phrase['id']}: {failed.get('error') or 'failed'}")
            continue
        state["candidates"].append(candidate)
        used_families.add(candidate["family_id"])
        _write_state(job, state)
    state["attempt_count"] = len(_read_attempts(job))
    return "progressed", notes


def _no_candidate_reason(job, notes) -> str:
    errors = []
    for attempt in _read_attempts(job):
        if attempt["status"] in ("failed", "interrupted",
                                 "cancelled_unpublished"):
            detail = attempt.get("error") or ""
            errors.append(f"{attempt['phrase_id']}: {attempt['status']} {detail}".strip())
    body = "no viable candidate; fix the reviewed metadata/permission and "
    body += "resubmit. Details: " + "; ".join(errors + notes)
    return body[:MAX_REASON]


# --------------------------------------------------------------------------
# Blind review build / verification
# --------------------------------------------------------------------------

def _expected_version_hashes(state) -> dict:
    expected = {"parent": state["binding"]["mix_sha256"]}
    expected.update({c["phrase_id"]: c["mix_sha256"]
                     for c in state["candidates"]})
    return expected


def _verify_review(job, state) -> None:
    review = job / "review"
    mapping_path = review / "private" / "mapping.json"
    if not mapping_path.is_file():
        raise WorkflowError("review mapping.json is missing")
    try:
        mapping = json.loads(mapping_path.read_text())
    except ValueError as exc:
        raise WorkflowError(f"review mapping.json is corrupt: {exc}") from exc
    assignment = mapping.get("assignment")
    labels = mapping.get("labels")
    if not isinstance(assignment, dict) or not isinstance(labels, list):
        raise WorkflowError("review mapping structure is invalid")
    expected = _expected_version_hashes(state)
    actual = {entry.get("id") for entry in assignment.values()}
    if actual != set(expected):
        raise WorkflowError(
            "review versions differ from the recorded parent/candidates")
    frozen = (state.get('review') or {}).get('files', {})
    for relative, digest in frozen.items():
        file = review / relative
        if not file.is_file() or _sha(file) != digest:
            raise WorkflowError(f'review {relative} changed after build')
    for label, entry in assignment.items():
        mix_path = Path(entry.get("mix_path", ""))
        if not mix_path.is_file():
            raise WorkflowError("review source mix file is missing")
        if _sha(mix_path) != expected[entry["id"]]:
            raise WorkflowError("review source bytes differ from the version")
        if not frozen:
            # A crash may leave a completed review before its state was saved.
            # Check actual audition samples against the bound mix and recorded gain.
            try:
                source, sr = sf.read(mix_path, always_2d=True)
                audition, ar = sf.read(review / 'public' / f'{label}.wav', always_2d=True)
                gain = 10 ** (mapping['level_match']['metrics'][label]['gain_db'] / 20)
                if sr != ar or source.shape != audition.shape or not np.allclose(
                        audition, source * gain, rtol=0, atol=6e-8):
                    raise ValueError('audition does not match its source')
            except Exception as exc:
                raise WorkflowError(f'review audio verification failed: {exc}') from exc
    decision = br.load_decision(review)
    if decision is not None:
        if decision.get("choice") not in labels + ["tie", "neither"]:
            raise WorkflowError("review decision is not bound to these labels")


def _ensure_review(job, state) -> None:
    if (job / "review").exists():
        _verify_review(job, state)
        _freeze_review(job, state)
        return
    parent = Path(state["binding"]["path"])
    items = [{"id": "parent", "mix_path": str(parent / "full_mix.wav")}]
    for candidate in state["candidates"]:
        items.append({"id": candidate["phrase_id"],
                      "mix_path": str(job / candidate["path"] / "full_mix.wav")})
    br.build_blind_review(items, job / "review")
    _verify_review(job, state)
    _freeze_review(job, state)


def _freeze_review(job, state):
    review = job / 'review'
    files = [review / 'private/mapping.json', review / 'public/index.html',
             *sorted((review / 'public').glob('*.wav'))]
    state['review'] = {'path': 'review', 'bound': True,
                       'files': {str(p.relative_to(review)): _sha(p) for p in files}}


# --------------------------------------------------------------------------
# Public lifecycle API
# --------------------------------------------------------------------------

def run(job) -> dict:
    """Render bounded candidates and build the review; safe to re-enter."""
    job = Path(job).expanduser().resolve()
    state, request = _load_job(job)

    if _is_cancelled(job):  # no automatic resume of a cancelled job
        if state["status"] != "cancelled":
            state["status"] = "cancelled"
            state["reason"] = "cancel marker present"
            _write_state(job, state)
        return _view(job, state)

    lock = _JobLock(job)
    if not lock.acquire():
        return _locked_view(job, state)
    try:
        state, request = _load_job(job)
        if _is_cancelled(job):
            return _cancelled(job, state)
        if state["status"] in STICKY_STATES:
            if state['status'] == 'completed':
                ok, reason = _verify_completion(job, state)
                if not ok:
                    return _fail(job, state, reason)
            return _view(job, state)
        if state["status"] != "running":
            state["status"] = "running"
            _write_state(job, state)

        try:
            _verify_inputs(job, state, request)
        except WorkflowError as exc:
            return _fail(job, state, str(exc))

        try:
            _recover(job, state, request)
        except WorkflowError as exc:
            return _fail(job, state, str(exc))

        outcome, notes = _render_loop(job, state, request)
        if outcome == "cancelled":
            state["status"] = "cancelled"
            state["reason"] = "cancelled during run"
            _write_state(job, state)
            return _view(job, state)
        if outcome == "failed":
            return _fail(job, state, notes)

        if state["candidates"]:
            if _is_cancelled(job):  # before review publication
                state["status"] = "cancelled"
                state["reason"] = "cancelled before review publication"
                _write_state(job, state)
                return _view(job, state)
            try:
                _ensure_review(job, state)
            except WorkflowError as exc:
                return _fail(job, state, str(exc))
            state["status"] = "awaiting_review"
            state["reason"] = None
            _write_state(job, state)
        else:
            state["status"] = "awaiting_confirmation"
            state["reason"] = _no_candidate_reason(job, notes)
            _write_state(job, state)
        return _view(job, state)
    finally:
        lock.release()


def status(job) -> dict:
    """Read-only state view; never writes to the job directory."""
    job = Path(job).expanduser().resolve()
    state, _ = _load_job(job)
    if _is_cancelled(job):
        state['status'] = 'cancelled'
    return _view(job, state)


def cancel(job) -> dict:
    """Atomically request cancellation without touching the render lock."""
    job = Path(job).expanduser().resolve()
    state, _ = _load_job(job)
    if state["status"] in ("completed", "failed", "cancelled"):
        return _view(job, state)  # terminal outcomes are not overridden
    _write_cancel_marker(job)
    lock = _JobLock(job)
    if lock.acquire():
        try:
            state, _ = _load_job(job)
            return _cancelled(job, state)
        finally:
            lock.release()
    state['status'] = 'cancelled'
    state['reason'] = 'cancel requested; in-flight work will stop before publication'
    return _view(job, state)


def _cancelled(job, state):
    state.update(status='cancelled', reason='cancel marker present', delivery=None)
    _write_state(job, state)
    return _view(job, state)


# --------------------------------------------------------------------------
# finish / delivery
# --------------------------------------------------------------------------

def _song_dir_for(job, state, selected_id: str) -> Path:
    if selected_id == "parent":
        return Path(state["binding"]["path"])
    for candidate in state["candidates"]:
        if candidate["phrase_id"] == selected_id:
            return job / candidate["path"]
    raise WorkflowError(f"selected id is not a job version: {selected_id}")


def _delivery_record(job, state, result, decision, selected_id) -> dict:
    return {
        "path": "delivery",
        "als": Path(result["als"]).name,
        "als_sha256": result["als_sha256"],
        "export_manifest_sha256": _sha(job / "delivery/export_manifest.json"),
        "media": result["media"],
        "selected_id": selected_id,
        "source_mix_sha256": result["mix_sha256"],
        "decision": {"request_id": decision["request_id"],
                     "choice": decision["choice"],
                     "selected_id": decision["selected_id"]}}


def _verify_delivery(job, state) -> tuple[bool, str | None]:
    delivery = state.get("delivery")
    if not isinstance(delivery, dict):
        return False, "no delivery record"
    delivery_dir = job / "delivery"
    manifest_path = delivery_dir / "export_manifest.json"
    if not manifest_path.is_file():
        return False, "export_manifest.json missing"
    try:
        manifest = json.loads(manifest_path.read_text())
    except ValueError:
        return False, "export_manifest.json corrupt"
    if _sha(manifest_path) != delivery.get('export_manifest_sha256'):
        return False, 'export manifest changed after collection'
    als_name = delivery.get("als")
    if not isinstance(als_name, str) or not (delivery_dir / als_name).is_file():
        return False, "ALS file missing"
    actual_als_sha = _sha(delivery_dir / als_name)
    if actual_als_sha != manifest.get("als_sha256"):
        return False, "ALS sha does not match export manifest"
    if actual_als_sha != delivery.get("als_sha256"):
        return False, "ALS sha does not match job record"
    for media in manifest.get("media", []):
        media_path = delivery_dir / media["relative_path"]
        if not media_path.is_file() or _sha(media_path) != media["sha256"]:
            return False, f"collected media hash mismatch: {media['track_id']}"
    selected_id = delivery.get("selected_id")
    try:
        song_dir = _song_dir_for(job, state, selected_id)
        checked = ableton_export.validate_song(song_dir)
    except Exception as exc:
        return False, f"source version no longer validates: {exc}"
    if checked["mix_sha256"] != manifest.get("mix_sha256"):
        return False, "delivery source identity differs from export manifest"
    if checked["mix_sha256"] != delivery.get("source_mix_sha256"):
        return False, "delivery source identity differs from job record"
    for key in ('mix_sha256', 'manifest_sha256', 'score_sha256'):
        if manifest.get(key) != checked[key]:
            return False, f'collected source {key} mismatch'
    copies = {'Reference/full_mix.wav': checked['mix_sha256'],
              'Source/score.json': checked['score_sha256'],
              'Source/run_manifest.json': checked['manifest_sha256']}
    for track in checked['tracks']:
        copies[f"Samples/Imported/{track['id']}.wav"] = track['sha256']
        copies[f"Source/midi/{track['id']}.mid"] = track['midi_sha256']
    for relative, digest in manifest.get('recipe_files', {}).items():
        source = (song_dir / relative).resolve()
        if not source.is_relative_to(song_dir.resolve()) or not source.is_file() or _sha(source) != digest:
            return False, 'recipe source changed or invalid'
        copies[f'Source/{relative}'] = digest
    for relative, digest in copies.items():
        path = (delivery_dir / relative).resolve()
        if not path.is_relative_to(delivery_dir.resolve()) or not path.is_file() or _sha(path) != digest:
            return False, f'collected file hash mismatch: {relative}'
    live_decision = br.load_decision(job / "review")
    if live_decision is None:
        return False, "review decision is missing"
    bound = delivery.get("decision", {})
    for key in ("request_id", "choice", "selected_id"):
        if bound.get(key) != live_decision.get(key):
            return False, f"delivery is not bound to the decision ({key})"
    return True, None


def _verify_completion(job, state) -> tuple[bool, str | None]:
    decision = br.load_decision(job / "review")
    if decision is None:
        return False, "completed job has no persisted decision"
    if state.get("decision") is None:
        return False, "state has no bound decision"
    for key in ("request_id", "choice", "selected_id"):
        if state["decision"].get(key) != decision.get(key):
            return False, f"state decision differs from review decision ({key})"
    if decision["keep"]:
        if not state.get("keep"):
            return False, "state/delivery keep flag inconsistent"
        return _verify_delivery(job, state)
    if state.get("keep") is not False:
        return False, "state keep flag inconsistent"
    if (job / "delivery").exists():
        return False, "keep-false job must not contain a delivery directory"
    return True, None


def finish(job) -> dict:
    """Apply the persisted blind decision; idempotent once completed."""
    job = Path(job).expanduser().resolve()
    state, request = _load_job(job)

    if _is_cancelled(job):  # respect cancellation before any side effect
        if state["status"] != "cancelled":
            state["status"] = "cancelled"
            state["reason"] = "cancel marker present"
            _write_state(job, state)
        return _view(job, state)

    lock = _JobLock(job)
    if not lock.acquire():
        return _locked_view(job, state)
    try:
        state, request = _load_job(job)
        if _is_cancelled(job):
            return _cancelled(job, state)
        if state["status"] in ("failed", "awaiting_confirmation", "cancelled"):
            return _view(job, state)

        if state["status"] == "completed":
            ok, reason = _verify_completion(job, state)
            if ok:
                return _view(job, state)
            return _fail(job, state, f"delivery no longer verifies: {reason}")

        if not (job / "review").is_dir():
            return _fail(job, state, "review artifact missing; run the job first")
        try:
            _verify_inputs(job, state, request)
            _verify_review(job, state)
            for candidate in state['candidates']:
                _verify_frozen_child(job, state, request, candidate)
        except (WorkflowError, ValueError, OSError) as exc:
            return _fail(job, state, str(exc))

        decision = br.load_decision(job / "review")
        if decision is None:
            if state["status"] != "awaiting_review":
                state["status"] = "awaiting_review"
                state["reason"] = None
                _write_state(job, state)
            return _view(job, state)

        mapping = json.loads(
            (job / "review/private/mapping.json").read_text())
        choice = decision["choice"]
        if choice in ("tie", "neither"):
            if decision.get("keep") is not False:
                return _fail(job, state, "decision keep/choice inconsistent")
        else:
            labels = mapping["labels"]
            if choice not in labels:
                return _fail(job, state,
                             f"decision label {choice} is not in this review")
            entry = mapping["assignment"][choice]
            if decision.get("selected_id") != entry["id"]:
                return _fail(job, state,
                             "decision selected_id conflicts with review mapping")
            valid_ids = {"parent"} | {c["phrase_id"]
                                      for c in state["candidates"]}
            if entry["id"] not in valid_ids:
                return _fail(job, state,
                             "decision selected version is not a job version")

        state["decision"] = {
            "request_id": decision["request_id"],
            "choice": decision["choice"], "keep": decision["keep"],
            "selected_id": decision["selected_id"]}

        if not decision["keep"]:
            state["status"] = "completed"
            state["keep"] = False
            state["delivery"] = None
            _write_state(job, state)
            return _view(job, state)

        selected_id = decision["selected_id"]
        try:
            song_dir = _song_dir_for(job, state, selected_id)
            checked = ableton_export.validate_song(song_dir)
        except Exception as exc:
            return _fail(job, state,
                         f"selected version fails validation: {exc}")
        expected_sha = (state["binding"]["mix_sha256"] if selected_id == "parent"
                        else next(c["mix_sha256"] for c in state["candidates"]
                                  if c["phrase_id"] == selected_id))
        if checked["mix_sha256"] != expected_sha:
            return _fail(job, state,
                         "selected version bytes differ from the reviewed version")

        delivery_dir = job / "delivery"
        if _is_cancelled(job):
            return _cancelled(job, state)
        if delivery_dir.exists():
            if not state.get('delivery'):
                try:
                    result = json.loads((delivery_dir / 'export_manifest.json').read_text())
                    state['delivery'] = _delivery_record(job, state, result, decision, selected_id)
                except (KeyError, ValueError, OSError) as exc:
                    return _fail(job, state, f'incomplete existing delivery: {exc}')
            ok, reason = _verify_delivery(job, state)
            if not ok:
                return _fail(job, state, f"existing delivery is invalid: {reason}")
        else:
            try:
                result = ableton_export.export_song(
                    song_dir, delivery_dir, set_name=SET_NAME)
            except Exception as exc:
                state.update(status='awaiting_review', reason=f'export failed: {_error_text(exc)}')
                _write_state(job, state)
                return _view(job, state)
            if _is_cancelled(job):
                return _cancelled(job, state)
            state["delivery"] = _delivery_record(job, state, result,
                                                 decision, selected_id)

        state["status"] = "completed"
        state["keep"] = True
        state['reason'] = None
        _write_state(job, state)
        return _view(job, state)
    finally:
        lock.release()


# --------------------------------------------------------------------------
# Local review server
# --------------------------------------------------------------------------

def review_callback(job):
    """on_decision callback: safely run finish after a human vote."""
    job = Path(job).expanduser().resolve()

    def on_decision(decision):
        if _is_cancelled(job):
            raise RuntimeError(
                "job is cancelled; the decision is saved but finish performs "
                "no side effect")
        try:
            result = finish(job)
        except Exception as exc:
            raise RuntimeError(f"finish failed: {exc}") from exc
        if result["status"] not in ('completed', 'cancelled'):
            raise RuntimeError(result.get("reason") or "finish failed")
        if result["status"] == "cancelled":
            raise RuntimeError("job was cancelled before finish")
        return result

    return on_decision


def serve(job, port: int = 8798) -> None:
    """Serve the review artifact on 127.0.0.1 only; blocking."""
    job = Path(job).expanduser().resolve()
    _load_job(job)
    if not (job / "review/private/mapping.json").is_file():
        raise WorkflowError("review artifact missing; run the job first")
    state, _ = _load_job(job)
    _verify_review(job, state)
    handler = br.make_handler(job / "review",
                              on_decision=review_callback(job), on_status=lambda: status(job))
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    print(f"盲听评审地址（仅本机可访问）: http://127.0.0.1:{port}/", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _print(result) -> None:
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    submit_parser = subparsers.add_parser(
        "submit", help="validate a request and reserve a job")
    submit_parser.add_argument("--request", required=True,
                               help="path to the request JSON file "
                                    "(or an inline JSON string)")
    submit_parser.add_argument("--jobs-root", type=Path, required=True)

    for name in ("run", "status", "cancel", "finish"):
        command_parser = subparsers.add_parser(name)
        command_parser.add_argument("--job", type=Path, required=True)

    review_parser = subparsers.add_parser(
        "review", help="serve the blind review on 127.0.0.1")
    review_parser.add_argument("--job", type=Path, required=True)
    review_parser.add_argument("--port", type=int, default=8798)

    args = parser.parse_args(argv)
    try:
        if args.command == "submit":
            request_arg = args.request
            request_path = Path(request_arg)
            if request_path.is_file():
                request = json.loads(request_path.read_text())
            else:
                request = json.loads(request_arg)
            _print(submit(request, args.jobs_root))
        elif args.command == "run":
            _print(run(args.job))
        elif args.command == "status":
            _print(status(args.job))
        elif args.command == "cancel":
            _print(cancel(args.job))
        elif args.command == "finish":
            _print(finish(args.job))
        elif args.command == "review":
            serve(args.job, args.port)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
