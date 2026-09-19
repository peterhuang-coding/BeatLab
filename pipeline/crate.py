"""BeatLab Citizen DJ crate batch runner."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
import re
import math
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import LIBRARY, ROOT, get_db, now_iso, upsert_source  # noqa: E402
import ingest  # noqa: E402
from connectors.citizen_dj import COLLECTIONS, CitizenDJConnector  # noqa: E402

SOURCE_ID = "citizen_dj"
VERSION = 1
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _md5_file(path: Path) -> str:
    h = hashlib.md5()  # nosec - content identity required by existing schema
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not isinstance(data, (bytes, bytearray)):
        data = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)
        data = data.encode("utf-8")
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def _crate_dir(batch_id: str) -> Path:
    return LIBRARY / "crates" / batch_id


def manifest_path(batch_id: str) -> Path:
    return _crate_dir(batch_id) / "manifest.json"


def _load(batch_id: str) -> dict | None:
    p = manifest_path(batch_id)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def _save(man: dict, state: str | None = None) -> dict:
    if state:
        man["state"] = state
    man["updated_at"] = now_iso()
    counts = {"ok": 0, "error": 0, "pending": 0, "downloaded": 0}
    for it in man.get("items", []):
        counts[it.get("state", "pending")] = counts.get(it.get("state", "pending"), 0) + 1
    man["counts"] = counts
    man["has_errors"] = bool(man.get("error")) or counts.get("error", 0) > 0
    _atomic(manifest_path(man["batch_id"]), man)
    return man


@contextmanager
def _lock(batch_id: str):
    # Different batches share the cache and ingestion staging files.
    folder = LIBRARY / "sources"
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / ".crate.lock").open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("another crate batch is running") from None
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def select_records(catalogs: dict[str, dict], limit: int, known_urls: set[str],
                   known_md5: set[str], cached: dict | None = None) -> list[dict]:
    """Round-robin collection selection; works before already-owned source URLs."""
    cached = cached or {}
    queues: dict[str, list[dict]] = {}
    for collection, cat in catalogs.items():
        records = list(cat.get("records", []))
        records.sort(key=lambda r: (
            1 if str(r.get("source_url", "")) in known_urls else 0,
        ))
        queues[collection] = records

    selected: list[dict] = []
    seen: set[str] = set()
    while len(selected) < limit and any(queues.values()):
        for collection in list(queues.keys()):
            if len(selected) >= limit:
                break
            while queues[collection]:
                rec = queues[collection].pop(0)
                url = rec.get("source_url") or rec.get("media_url")
                if not url or url in seen:
                    continue
                cached_md5 = cached.get(rec["media_url"], {}).get("md5")
                if cached_md5 and cached_md5 in known_md5:
                    continue
                seen.add(url)
                selected.append({
                    "collection": collection,
                    "record": rec,
                })
                break
            if not queues[collection]:
                del queues[collection]
    return selected


def _fresh_manifest(batch_id: str, collections: list[str], limit: int,
                    timeout_s: int, discover_only: bool) -> dict:
    return {
        "version": VERSION,
        "batch_id": batch_id,
        "state": "discovery_pending",
        "options": {
            "collections": list(collections), "limit": limit,
            "timeout_s": timeout_s, "discover_only": discover_only,
        },
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "error": None,
        "catalogs": {},
        "items": [],
        "counts": {},
    }


def _connector(timeout_s: int, deadline: float | None = None) -> CitizenDJConnector:
    c = CitizenDJConnector({"timeout_s": timeout_s})
    c.deadline = deadline if deadline is not None else time.monotonic() + timeout_s
    return c


def _discover(man: dict, deadline: float) -> None:
    opt = man["options"]
    conn = _connector(opt["timeout_s"], deadline)
    catalogs: dict[str, dict] = {}
    try:
        for collection in opt["collections"]:
            if time.monotonic() >= deadline:
                raise TimeoutError("discover timeout")
            catalogs[collection] = conn.discover(collection)
        man["catalogs"] = catalogs
        man["state"] = "discovered"
        man.pop("error", None)
        _save(man)
    except Exception as e:
        man["error"] = f"discovery failed: {type(e).__name__}: {e}"
        _save(man, "discovery_error")
        raise


def _freeze(man: dict) -> None:
    if man["state"] not in ("discovered", "ready"):
        return
    if man["state"] == "ready":
        return
    db = get_db()
    try:
        rows = db.execute("SELECT source_url FROM assets WHERE source_url IS NOT NULL").fetchall()
        known_urls = {r[0] for r in rows}
        known_md5 = {r[0] for r in db.execute("SELECT md5 FROM assets WHERE md5 IS NOT NULL")}
    finally:
        db.close()
    connector = CitizenDJConnector()
    cached = {rec["media_url"]: connector.cached_entry(rec) or {}
              for cat in man["catalogs"].values() for rec in cat["records"]}
    picks = select_records(man["catalogs"], man["options"]["limit"], known_urls, known_md5, cached)
    man["items"] = [{
        "id": f"{i + 1:03d}", "state": "pending", "error": None,
        "collection": p["collection"], "record": p["record"],
        "entry": None,
        "downloaded_sha256": None, "asset_id": None,
        "library_path": None, "md5": None,
    } for i, p in enumerate(picks)]
    _save(man, "ready")


def _verify_completed(item: dict, conn) -> str | None:
    entry = item.get("entry") or {}
    lib = Path(item["library_path"]) if item.get("library_path") else None
    saved_md5 = item.get("md5")
    if not lib or not saved_md5 or not lib.exists() or lib.stat().st_size <= 0:
        return "completed audio is missing"
    if _sha256_file(lib) != item.get("library_sha256"):
        return "completed normalized audio SHA256 mismatch"
    orig = entry.get("orig_path")
    if not orig or not Path(orig).exists() or _sha256_file(Path(orig)) != item.get("downloaded_sha256"):
        return "downloaded original is missing or changed; restore the saved original"
    row = conn.execute(
        "SELECT id, md5, library_path FROM assets WHERE md5 = ?", (saved_md5,)).fetchone()
    if not row:
        return "asset database row missing"
    if row["id"] != item.get("asset_id") or row["library_path"] != str(lib):
        return "asset database identity/path mismatch"
    return None


def _process_item(item: dict, man: dict, connector: CitizenDJConnector, conn) -> bool:
    if item.get("state") == "ok" or item.get("corrupt_completed"):
        try:
            error = _verify_completed(item, conn)
        except (OSError, ValueError) as exc:
            error = str(exc)
        item.update(state="error" if error else "ok", error=error,
                    corrupt_completed=bool(error))
        _save(man)
        return error is None
    try:
        entry = connector.fetch_entry(item["collection"], item["record"], man["catalogs"][item["collection"]])
        if not isinstance(entry, dict) or "orig_path" not in entry or "md5" not in entry:
            raise ValueError("invalid ingest entry")
        orig = Path(entry["orig_path"])
        if not orig.exists() or orig.stat().st_size <= 0:
            raise FileNotFoundError(str(orig))
        if _md5_file(orig) != entry["md5"]:
            raise ValueError("downloaded original md5 mismatch")
        item["entry"] = entry
        item["downloaded_sha256"] = _sha256_file(orig)
        item["state"] = "downloaded"
        item["error"] = None
        _save(man)

        status, msg = ingest.ingest_entry(conn, entry, SOURCE_ID, False)
        if status == "fail":
            raise RuntimeError(msg)
        md5 = entry["md5"]
        row = conn.execute(
            "SELECT id, library_path FROM assets WHERE md5 = ?", (md5,)).fetchone()
        if not row:
            raise RuntimeError("asset was not inserted")
        lib = Path(row["library_path"])
        connector._valid_audio(lib)
        rights = conn.execute("SELECT state, basis FROM rights WHERE asset_id = ?", (row["id"],)).fetchone()
        item["rights"] = dict(rights) if rights else {"state": "needs_review"}
        item.update({"state": "ok", "error": None, "asset_id": row["id"],
                     "library_path": str(lib), "md5": md5,
                     "library_sha256": _sha256_file(lib)})
        _save(man)
        return True
    except Exception as e:
        item["state"] = "error"
        item["error"] = f"{type(e).__name__}: {e}"
        _save(man)
        return False


def _write_derived(man: dict) -> None:
    d = _crate_dir(man["batch_id"])
    catalog = {"version": VERSION, "batch_id": man["batch_id"],
               "collections": man["options"]["collections"],
               "state": man["state"], "items": man["items"],
               "selection_policy": "different recordings first; not a musical quality ranking"}
    _atomic(d / "catalog.json", catalog)
    lines = ["#EXTM3U"]
    readme = [f"# Citizen DJ crate {man['batch_id']}", "",
              f"State: {man['state']}", f"Created: {man['created_at']}", ""]
    for item in man["items"]:
        rec, entry = item.get("record", {}), item.get("entry") or {}
        rights = item.get("rights") or {}
        readme += [f"## {item['id']} {rec.get('title','')}",
                   f"- source: {rec.get('source_url') or rec.get('media_url')}",
                   f"- offset: {rec.get('excerpt_start_seconds_label', '')}",
                   f"- status: {item['state']}",
                   f"- original: {(entry.get('orig_path') or '')}",
                   f"- library: {item.get('library_path') or ''}",
                   f"- rights: {rights.get('state','needs_review')} / {rights.get('basis','unknown_license')}",
                   f"- error: {item.get('error') or ''}", ""]
        if item["state"] == "ok":
            title = rec.get("title", "").replace("\n", " ").replace("\r", " ")
            lines.append(f"#EXTINF:{entry.get('duration_s', -1)},{title}")
            lines.append(os.path.relpath(item["library_path"], d))
    _atomic(d / "README.md", "\n".join(readme).encode("utf-8"))
    _atomic(d / "playlist.m3u8", ("\n".join(lines) + "\n").encode("utf-8"))


def run_batch(batch_id: str, collections: list[str] | None = None,
              limit: int | None = None, timeout_s: float | None = None,
              resume: bool = False, discover_only: bool = False) -> dict:
    if not SAFE_ID.fullmatch(batch_id):
        raise ValueError("invalid batch id")
    if resume and (collections is not None or limit is not None or timeout_s is not None or discover_only):
        raise ValueError("resume uses saved options; omit selection/timeout/discover flags")
    if not resume:
        limit = 8 if limit is None else limit
        timeout_s = 300 if timeout_s is None else timeout_s
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer 1..100")
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("timeout must be finite and positive")
        collections = list(COLLECTIONS) if collections is None else collections
        if not collections or len(set(collections)) != len(collections) or any(c not in COLLECTIONS for c in collections):
            raise ValueError("use distinct supported collections")
    ROOT.mkdir(parents=True, exist_ok=True)
    LIBRARY.mkdir(parents=True, exist_ok=True)
    with _lock(batch_id):
        existing = _load(batch_id)
        if existing and not resume:
            raise RuntimeError("batch exists; use --resume")
        if resume and not existing:
            raise FileNotFoundError("batch not found")
        man = existing or _fresh_manifest(batch_id, collections, limit, timeout_s, discover_only)
        if man.get("version") != VERSION or man.get("batch_id") != batch_id:
            raise ValueError("invalid batch manifest")
        timeout_s = man["options"]["timeout_s"]
        deadline = time.monotonic() + timeout_s
        man["error"] = None
        _save(man)
        if man["state"] in ("discovery_pending", "discovery_error"):
            try:
                _discover(man, deadline)
            except Exception:
                _write_derived(man)
                return man
        _freeze(man)
        if discover_only:
            _save(man, "ready")
            _write_derived(man)
            return man
        db = get_db()
        try:
            upsert_source(db, {"id": SOURCE_ID, "name": "Citizen DJ", "type": SOURCE_ID,
                              "config": {"collections": man["options"]["collections"]}, "enabled": 1})
            connector = _connector(timeout_s, deadline)
            for item in man["items"]:
                if item.get("state") != "ok" and not item.get("corrupt_completed") and time.monotonic() >= deadline:
                    man["error"] = "timeout; resume this fixed batch to continue"
                    break
                _process_item(item, man, connector, db)
            states = {i["state"] for i in man["items"]}
            state = "complete" if states <= {"ok"} and not man.get("error") else ("partial" if "ok" in states else "failed")
            _save(man, state)
            _write_derived(man)
            return man
        finally:
            db.close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Create/resume a Citizen DJ crate")
    p.add_argument("--batch-id", required=True)
    p.add_argument("--collections", nargs="+", choices=list(COLLECTIONS))
    p.add_argument("--limit", type=int)
    p.add_argument("--timeout", type=float)
    p.add_argument("--resume", action="store_true")
    p.add_argument("--discover-only", action="store_true")
    a = p.parse_args(argv)
    try:
        man = run_batch(a.batch_id, a.collections, a.limit, a.timeout,
                        a.resume, a.discover_only)
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    d = _crate_dir(a.batch_id)
    print(f"batch={man['batch_id']} state={man['state']} counts={man.get('counts')}")
    print(f"manifest={manifest_path(a.batch_id)}")
    print(f"catalog={d/'catalog.json'} readme={d/'README.md'} playlist={d/'playlist.m3u8'}")
    return 1 if man.get("has_errors") or man.get("state") not in ("ready", "complete") else 0


if __name__ == "__main__":
    sys.exit(main())
