#!/usr/bin/env python3
"""Real ingest-to-Review smoke using generated audio, without model separation.

Outputs stay in a fresh .cache/smoke-* directory for inspection. This checks
plumbing and audio exports, not musical quality or Demucs inference.
"""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile

import numpy as np
import soundfile as sf

PROJECT = Path(__file__).resolve().parents[1]
(PROJECT / ".cache").mkdir(exist_ok=True)
ROOT = Path(tempfile.mkdtemp(prefix="smoke-", dir=PROJECT / ".cache"))
ENV = dict(os.environ, BEATLAB_ROOT=str(ROOT), BEATLAB_MIRROR_ROOT=str(ROOT / "exports"))
RUN = "smoke-generated-synth"
print(f"SMOKE_ROOT={ROOT}", flush=True)

# A deterministic musical phrase; entirely generated, not a user's recording.
sr = 44100
t = np.arange(sr // 2) / sr
phrase = []
for midi in [60, 64, 67, 64, 62, 65, 69, 65] * 4:
    freq = 440 * 2 ** ((midi - 69) / 12)
    tone = (np.sin(2 * np.pi * freq * t) + 0.25 * np.sin(4 * np.pi * freq * t))
    envelope = np.minimum(t / 0.015, 1) * np.exp(-4 * t)
    phrase.append(0.3 * tone * envelope)
audio = np.concatenate(phrase).astype(np.float32)
source = ROOT / "synthetic-phrase.wav"
sf.write(source, np.column_stack([audio, audio * 0.95]), sr)


def run(*args):
    subprocess.run([sys.executable, str(PROJECT / "pipeline/pipeline.py"), *args],
                   env=ENV, check=True)


run("ingest", "--path", str(source))
with sqlite3.connect(ROOT / "db.sqlite") as conn:
    assert conn.execute("select count(*) from assets").fetchone()[0] == 1
    conn.execute("update assets set bpm=120, bpm_conf=1, key_note='C', key_conf=1")
    conn.execute("update rights set state='allowed', basis='generated_test_fixture'")
run("moments", "--all")
run("score", "--all")
run("compose", RUN, "--bpm", "92")
run("render", RUN)
run("report", RUN)

base = ROOT / "beats" / RUN
durations = {}
for kind in ("loop", "chop", "stem"):
    folder = base / kind
    mix, rate = sf.read(folder / "full_mix.wav")
    assert rate == sr and mix.ndim == 2 and mix.shape[1] == 2
    assert np.isfinite(mix).all() and np.max(np.abs(mix)) > 0.01
    duration = len(mix) / rate
    assert 60 <= duration <= 91, duration
    durations[kind] = round(duration, 3)
    for stem in ("chops", "drums", "bass", "vocal"):
        assert abs(sf.info(folder / "stems" / f"{stem}.wav").duration - duration) < 0.001
    for name in ("recipe.json", "provenance.json"):
        assert isinstance(json.loads((folder / name).read_text()), dict)
    assert list((folder / "midi").glob("*.mid"))
    assert list((folder / "chops").glob("*.wav"))

before = {p: p.stat().st_mtime_ns for p in base.rglob("*.wav")}
run("render", RUN)
assert before == {p: p.stat().st_mtime_ns for p in base.rglob("*.wav")}
with sqlite3.connect(ROOT / "db.sqlite") as conn:
    assert conn.execute("pragma integrity_check").fetchone()[0] == "ok"
    assert conn.execute("select count(*) from moments").fetchone()[0] > 0
    assert conn.execute("select state from jobs where id=?", (RUN,)).fetchone()[0] == "generated"
assert list((ROOT / "exports").glob(f"*/{RUN}.html"))
print(json.dumps({"status": "PASS", "root": str(ROOT), "duration_s": durations,
                  "separation": "not tested; synthetic input without Demucs",
                  "als_created": bool(list(base.glob('*.als')))}, ensure_ascii=False), flush=True)
