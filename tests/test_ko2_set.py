"""Synthetic tests for the offline BeatLab KO II set exporter.

No real audio, databases, or network resources are used: every source
WAV is a generated fixture in a temporary directory.
"""

import hashlib
import json
import math
import os
import shutil
import sys
import tempfile
import unittest

import mido
import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pipeline import ko2_set as ks  # noqa: E402

PCM_TOL = 2.0 / 32768.0  # PCM16 quantization tolerance (+1 LSB headroom)


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def write_wav(path, data, sr, subtype="PCM_16"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    sf.write(path, data, sr, subtype=subtype)


def tone(seconds=0.2, sr=44100, amp=0.5, freq=220.0):
    n = int(round(seconds * sr))
    t = np.arange(n, dtype=np.float64) / sr
    return amp * np.sin(2.0 * np.pi * freq * t)


def fade_expected(x, sr=44100, seconds=0.005):
    y = np.array(x, dtype=np.float64)
    n = min(int(round(seconds * sr)), len(y) // 2)
    if n > 0:
        y[:n] *= np.linspace(0.0, 1.0, n)
        y[-n:] *= np.linspace(1.0, 0.0, n)
    return y


class Ko2Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ko2test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def p(self, *parts):
        return os.path.join(self.tmp, *parts)

    def make_pad(self, pid="kick", group="A", slot=0, data=None, sr=44100,
                 gain_db=0.0, label=None, freq=None, subtype="PCM_16"):
        path = self.p("src-" + pid + ".wav")
        if data is None:
            data = tone(0.2, sr, freq=180.0 if freq is None else freq)
        write_wav(path, data, sr, subtype=subtype)
        return {
            "id": pid,
            "group": group,
            "slot": slot,
            "label": label if label is not None else pid.title(),
            "path": path,
            "sha256": file_sha256(path),
            "gain_db": gain_db,
            "provenance": {
                "kind": "recorded_sample",
                "source_url": "https://example.org/" + pid,
                "note": "synthetic fixture",
            },
        }

    def job(self, pads, events=None, scenes=None, bpm=88, bars=4,
            set_id="demo-v1", title="Demo", parent_version="parent-id"):
        if scenes is None:
            scenes = [{"name": "Intro", "start_bar": 0, "bars": min(2, bars),
                       "instruction": "Play B first"}]
        return {
            "set_id": set_id,
            "title": title,
            "bpm": bpm,
            "bars": bars,
            "parent_version": parent_version,
            "pads": pads,
            "events": events if events is not None else [],
            "scenes": scenes,
        }

    def run_cli(self, job, out_name="set-out", job_name="job.json"):
        job_path = self.p(job_name)
        with open(job_path, "w") as f:
            json.dump(job, f)
        out_path = self.p(out_name)
        rc = ks.main(["ko2_set.py", job_path, out_path])
        return rc, out_path, job_path

    def read_manifest(self, out):
        with open(os.path.join(out, "set.json")) as f:
            return json.load(f)


def midi_events(mid_path):
    mid = mido.MidiFile(mid_path)
    notes = []  # (absolute_tick, kind, note, velocity)
    tempo = None
    for track in mid.tracks:
        now = 0
        for msg in track:
            now += msg.time
            if msg.type == "set_tempo" and tempo is None:
                tempo = msg.tempo
            elif msg.type == "note_on" and msg.velocity > 0:
                notes.append((now, "on", msg.note, msg.velocity, msg.channel))
            elif msg.type == "note_off" or (
                msg.type == "note_on" and msg.velocity == 0
            ):
                notes.append((now, "off", msg.note, msg.velocity, msg.channel))
    return mid, notes, tempo


class TestExportBasics(Ko2Case):
    def test_pcm16_mono_output_and_exact_manifest(self):
        pad = self.make_pad(freq=200.0)
        job = self.job(
            [pad],
            events=[{"pad_id": "kick", "beat": 0, "velocity": 0.8}],
            bars=2,
        )
        rc, out, _ = self.run_cli(job)
        self.assertEqual(rc, 0)

        manifest = self.read_manifest(out)
        self.assertEqual(
            list(manifest.keys()),
            [
                "schema", "set_id", "title", "bpm", "bars", "parent_version",
                "hardware_verified", "listening_review", "pads", "events",
                "scenes", "demo", "midi", "sample_bytes",
            ],
        )
        self.assertEqual(manifest["schema"], "beatlab.ko2-set.v1")
        self.assertIs(manifest["hardware_verified"], False)
        self.assertEqual(manifest["listening_review"], "pending")
        self.assertEqual(manifest["set_id"], "demo-v1")
        self.assertEqual(manifest["bpm"], 88)
        self.assertEqual(manifest["bars"], 2)

        (mp,) = manifest["pads"]
        self.assertEqual(
            list(mp.keys()),
            [
                "id", "group", "slot", "pad_label", "midi_note", "label",
                "file", "sha256", "source_sha256", "frames", "sample_rate",
                "channels", "duration_seconds", "gain_db", "provenance",
            ],
        )
        self.assertEqual(mp["file"], "Samples/A/00-kick.wav")
        self.assertEqual(mp["pad_label"], ".")
        self.assertEqual(mp["midi_note"], 36)
        self.assertEqual(mp["sample_rate"], 44100)
        self.assertEqual(mp["channels"], 1)
        self.assertEqual(mp["source_sha256"], pad["sha256"])
        self.assertEqual(mp["provenance"], pad["provenance"])
        self.assertAlmostEqual(mp["duration_seconds"], mp["frames"] / 44100)

        # No absolute source path may leak into the manifest.
        self.assertNotIn(self.tmp, json.dumps(manifest))

        sample_path = os.path.join(out, mp["file"])
        info = sf.info(sample_path)
        self.assertEqual(info.samplerate, 44100)
        self.assertEqual(info.channels, 1)
        self.assertEqual(info.subtype, "PCM_16")
        self.assertEqual(info.frames, mp["frames"])
        self.assertEqual(file_sha256(sample_path), mp["sha256"])

        # sample_bytes tracks PCM payload (16-bit mono frames), no WAV headers.
        total = sum(p["frames"] * 2 for p in manifest["pads"])
        self.assertEqual(manifest["sample_bytes"], total)
        self.assertLessEqual(total, 16 * 1024 * 1024)

        # Processed PCM content: gain + 5 ms fades, quantized to PCM16.
        decoded, sr = sf.read(sample_path, dtype="float64")
        expected = fade_expected(tone(0.2, 44100, freq=200.0))
        self.assertEqual(sr, 44100)
        self.assertTrue(np.allclose(decoded, expected, atol=PCM_TOL))
        self.assertLessEqual(np.max(np.abs(decoded)), 1.0)

        # Demo / midi entries point at portable relative files with hashes.
        self.assertEqual(manifest["demo"]["file"], "Demo/performance.wav")
        self.assertEqual(manifest["midi"]["file"], "Demo/performance.mid")
        self.assertEqual(
            file_sha256(os.path.join(out, manifest["demo"]["file"])),
            manifest["demo"]["sha256"],
        )
        self.assertEqual(
            file_sha256(os.path.join(out, manifest["midi"]["file"])),
            manifest["midi"]["sha256"],
        )
        expected_dur = 2 * 4 * 60.0 / 88 + 2.0
        self.assertAlmostEqual(
            manifest["demo"]["duration_seconds"], expected_dur, places=4
        )
        dinfo = sf.info(os.path.join(out, manifest["demo"]["file"]))
        self.assertEqual(dinfo.channels, 2)
        self.assertEqual(dinfo.samplerate, 44100)
        self.assertEqual(dinfo.subtype, "PCM_16")
        self.assertEqual(manifest["events"], job["events"])
        self.assertEqual(manifest["scenes"], job["scenes"])


class TestMidiMapping(Ko2Case):
    def test_exact_slot_labels_and_midi_notes_including_enter(self):
        labels = [".", "0", "ENTER", "1", "2", "3", "4", "5", "6", "7", "8", "9"]
        pads, events = [], []
        for slot in range(12):
            pid = "a%d" % slot
            pads.append(self.make_pad(pid=pid, group="A", slot=slot,
                                      freq=150.0 + slot * 12.0))
            events.append({"pad_id": pid, "beat": float(slot) * 0.25,
                           "velocity": [0.0, 0.25, 1.0][slot % 3]})
        for gi, group in enumerate("BCD"):
            pid = group.lower() + "zero"
            pads.append(self.make_pad(pid=pid, group=group, slot=0,
                                      freq=500.0 + gi * 50.0))
            events.append({"pad_id": pid, "beat": 3.0 + gi * 0.25,
                           "velocity": 0.7})

        rc, out, _ = self.run_cli(self.job(pads, events, bars=4))
        self.assertEqual(rc, 0)
        m = self.read_manifest(out)

        by_id = {p["id"]: p for p in m["pads"]}
        for slot in range(12):
            pad = by_id["a%d" % slot]
            self.assertEqual(pad["pad_label"], labels[slot])
            self.assertEqual(pad["midi_note"], 36 + slot)
            self.assertEqual(pad["file"],
                             "Samples/A/%02d-a%d.wav" % (slot, slot))
        self.assertEqual(by_id["bzero"]["midi_note"], 48)
        self.assertEqual(by_id["czero"]["midi_note"], 60)
        self.assertEqual(by_id["dzero"]["midi_note"], 72)

        mid, notes, tempo = midi_events(
            os.path.join(out, m["midi"]["file"]))
        self.assertEqual(mid.type, 1)
        self.assertEqual(mid.ticks_per_beat, 480)
        self.assertEqual(tempo, int(round(60_000_000.0 / 88)))

        ons = [(tick, note, vel) for tick, kind, note, vel, ch in notes
               if kind == "on"]
        offs = [(tick, note) for tick, kind, note, vel, ch in notes
                if kind == "off"]
        on_by_note = {note: (tick, vel) for tick, note, vel in ons}
        self.assertEqual(
            set(on_by_note),
            set(range(36, 48)) | {48, 60, 72},
        )
        # All messages on channel 0.
        self.assertTrue(all(ch == 0 for _, _, _, _, ch in notes))
        # Gate is exactly 0.1 beat = 48 ticks at PPQ480.
        off_by_note = {note: tick for tick, note in offs}
        for note, (tick, _vel) in on_by_note.items():
            self.assertEqual(off_by_note[note] - tick, 48)
        # Grid agreement: MIDI beat ticks line up with the audio frame grid.
        demo, _ = sf.read(os.path.join(out, m["demo"]["file"]))
        for ev in events:
            note = by_id[ev["pad_id"]]["midi_note"]
            tick = on_by_note[note][0]
            audio_frame = int(round(tick / 480.0 * 60.0 / 88 * 44100))
            self.assertEqual(
                audio_frame, int(round(ev["beat"] * 60.0 / 88 * 44100)))
        # Velocity mapping incl. min 1 and max 127.
        self.assertEqual(on_by_note[36][1], 1)       # v=0 -> min 1
        self.assertEqual(on_by_note[37][1], 32)      # round(0.25*127)
        self.assertEqual(on_by_note[38][1], 127)     # v=1
        self.assertEqual(demo.shape[1], 2)


class TestDemoRendering(Ko2Case):
    def test_demo_matches_exported_pad_gain_and_velocity(self):
        # amp 0.8, -6 dB pad gain, half-velocity trigger.
        pad = self.make_pad(data=tone(0.2, amp=0.8, freq=190.0), gain_db=-6.0)
        events = [{"pad_id": "kick", "beat": 0, "velocity": 0.5}]
        rc, out, _ = self.run_cli(self.job([pad], events, bars=1))
        self.assertEqual(rc, 0)
        m = self.read_manifest(out)
        exported, _ = sf.read(os.path.join(out, "Samples/A/00-kick.wav"),
                              dtype="float64")
        self.assertAlmostEqual(np.max(np.abs(exported)),
                               0.8 * 10.0 ** (-6.0 / 20.0), delta=PCM_TOL)

        demo, _ = sf.read(os.path.join(out, m["demo"]["file"]),
                          dtype="float64")
        n = len(exported)
        wanted = exported * 0.5
        self.assertTrue(np.allclose(demo[:n, 0], wanted, atol=PCM_TOL))
        self.assertTrue(np.allclose(demo[:n, 1], wanted, atol=PCM_TOL))
        # One-shot placed at beat 0 and fully played; silence after it.
        self.assertTrue(np.all(demo[n:, :] == 0.0))
        self.assertEqual(m["demo"]["gain_db"], 0.0)

    def test_demo_global_attenuation_and_timing(self):
        pad = self.make_pad(data=tone(0.2, amp=0.95, freq=210.0))
        events = [{"pad_id": "kick", "beat": 1.0, "velocity": 1.0}]
        rc, out, _ = self.run_cli(self.job([pad], events, bars=2))
        self.assertEqual(rc, 0)
        m = self.read_manifest(out)
        demo, _ = sf.read(os.path.join(out, m["demo"]["file"]),
                          dtype="float64")
        peak = float(np.max(np.abs(demo)))
        self.assertAlmostEqual(peak, 0.89125, delta=2 * PCM_TOL)
        start = int(round(1.0 * 60.0 / 88 * 44100))
        exported, _ = sf.read(os.path.join(out, "Samples/A/00-kick.wav"),
                              dtype="float64")
        # Attenuation is computed from the re-decoded EXPORTED sample.
        exported_peak = float(np.max(np.abs(exported)))
        factor = 0.89125 / exported_peak
        self.assertAlmostEqual(m["demo"]["gain_db"],
                               20.0 * math.log10(factor), places=6)
        wanted = exported * factor
        end = start + len(exported)
        self.assertTrue(np.allclose(demo[start:end, 0], wanted, atol=PCM_TOL))
        self.assertTrue(np.all(demo[:start, :] == 0.0))

        mid, notes, _ = midi_events(os.path.join(out, m["midi"]["file"]))
        on = [(t, n) for t, k, n, v, c in notes if k == "on"]
        self.assertEqual(on, [(480, 36)])
        off = [(t, n) for t, k, n, v, c in notes if k == "off"]
        self.assertEqual(off, [(528, 36)])


class TestResampling(Ko2Case):
    def test_stereo_22050_and_mono_48000_sources(self):
        n22 = 22050
        t = np.arange(n22) / 22050.0
        w = 0.5 * np.sin(2 * np.pi * 300.0 * t)
        stereo = np.column_stack((w, 0.6 * w))
        p1 = self.make_pad(pid="s22", data=stereo, sr=22050, slot=0)

        w48 = tone(0.5, 48000, amp=0.4, freq=440.0)
        p2 = self.make_pad(pid="s48", data=w48, sr=48000, group="A", slot=1)

        events = [
            {"pad_id": "s22", "beat": 0.0, "velocity": 1.0},
            {"pad_id": "s48", "beat": 2.0, "velocity": 1.0},
        ]
        rc, out, _ = self.run_cli(self.job([p1, p2], events, bars=2))
        self.assertEqual(rc, 0)
        m = self.read_manifest(out)
        by_id = {p["id"]: p for p in m["pads"]}

        y1, sr1 = sf.read(os.path.join(out, by_id["s22"]["file"]),
                          dtype="float64")
        self.assertEqual(sr1, 44100)
        self.assertEqual(by_id["s22"]["frames"], len(y1))
        exp1 = resample_poly(stereo, 2, 1, axis=0).mean(axis=1)
        self.assertEqual(len(y1), len(exp1))
        self.assertTrue(np.allclose(y1[400:-400], exp1[400:-400], atol=1e-4))

        g = math.gcd(44100, 48000)
        exp2 = resample_poly(w48, 44100 // g, 48000 // g)
        y2, sr2 = sf.read(os.path.join(out, by_id["s48"]["file"]),
                          dtype="float64")
        self.assertEqual(sr2, 44100)
        self.assertEqual(len(y2), len(exp2))
        self.assertTrue(np.allclose(y2[400:-400], exp2[400:-400], atol=1e-4))


class TestRejections(Ko2Case):
    def _assert_failed_clean(self, job, out_name):
        rc, out, _ = self.run_cli(job, out_name=out_name)
        self.assertEqual(rc, 1)
        self.assertFalse(os.path.exists(out))
        parent = self.tmp
        self.assertFalse(
            any(name.startswith("." + out_name + ".tmp-")
                for name in os.listdir(parent))
        )

    def test_sha_mismatch_rejected(self):
        pad = self.make_pad()
        pad["sha256"] = "0" * 63 + "1"
        self._assert_failed_clean(self.job([pad]), "out-sha")

    def test_duplicate_slot_and_id_and_bad_group(self):
        p1 = self.make_pad(pid="a", slot=0)
        p2 = self.make_pad(pid="b", slot=0)
        self._assert_failed_clean(self.job([p1, p2]), "out-dup-slot")

        q1 = self.make_pad(pid="same", slot=0)
        q2 = self.make_pad(pid="same", slot=1)
        self._assert_failed_clean(self.job([q1, q2]), "out-dup-id")

        r1 = self.make_pad(pid="g", group="E", slot=0)
        self._assert_failed_clean(self.job([r1]), "out-group")

        s1 = self.make_pad(pid="s", slot=12)
        self._assert_failed_clean(self.job([s1]), "out-slot")

    def test_malformed_events_and_scenes(self):
        pad = self.make_pad()
        base_pads = [pad]
        bad_events = [
            {"pad_id": "ghost", "beat": 0, "velocity": 1.0},
            {"pad_id": "kick", "beat": 99.0, "velocity": 1.0},
            {"pad_id": "kick", "beat": -0.1, "velocity": 1.0},
            {"pad_id": "kick", "beat": 0, "velocity": 1.5},
            {"pad_id": "kick", "beat": 0, "velocity": float("nan")},
        ]
        for i, ev in enumerate(bad_events):
            self._assert_failed_clean(
                self.job(base_pads, [ev]), "out-event-%d" % i)
        bad_scene = [{"name": "X", "start_bar": 99, "bars": 2,
                      "instruction": "nope"}]
        self._assert_failed_clean(
            self.job(base_pads, [], scenes=bad_scene), "out-scene")

    def test_silence_peak_duration_and_bad_gain_rejected(self):
        self._assert_failed_clean(
            self.job([self.make_pad(pid="z", data=np.zeros(44100))]),
            "out-silence")
        loud = self.make_pad(pid="l", data=tone(0.2, amp=1.0), gain_db=3.0)
        self._assert_failed_clean(self.job([loud]), "out-peak")
        long_src = tone(17.0, sr=8000, amp=0.5, freq=200.0)
        self._assert_failed_clean(
            self.job([self.make_pad(pid="long", data=long_src, sr=8000)]),
            "out-long")
        # Float WAV so >1 values survive encoding (PCM16 would clip).
        hot = self.make_pad(pid="hot", data=tone(0.2, amp=1.2),
                            subtype="FLOAT")
        self._assert_failed_clean(self.job([hot]), "out-hot")
        bad_gain = self.make_pad(pid="g", gain_db=13.0)
        self._assert_failed_clean(self.job([bad_gain]), "out-gain")

    def test_bad_later_pad_leaves_no_output(self):
        p1 = self.make_pad(pid="one", slot=0, freq=120.0)
        p2 = self.make_pad(pid="two", slot=1, freq=140.0)
        p3 = self.make_pad(pid="three", slot=2, freq=160.0)
        p3["sha256"] = file_sha256(self.p("job.json")) if os.path.exists(
            self.p("job.json")) else "f" * 64
        job = self.job(
            [p1, p2, p3],
            events=[{"pad_id": "one", "beat": 0, "velocity": 1.0}],
        )
        job_path = self.p("job-late.json")
        with open(job_path, "w") as f:
            json.dump(job, f)
        p3["sha256"] = file_sha256(job_path)  # definitely wrong hash
        with open(job_path, "w") as f:
            json.dump(job, f)
        out = self.p("out-late")
        rc = ks.main(["ko2_set.py", job_path, out])
        self.assertEqual(rc, 1)
        self.assertFalse(os.path.exists(out))
        self.assertFalse(
            any(n.startswith(".out-late.tmp-") for n in os.listdir(self.tmp))
        )


class TestAtomicity(Ko2Case):
    def test_existing_output_dir_unchanged(self):
        pad = self.make_pad()
        job = self.job(
            [pad], events=[{"pad_id": "kick", "beat": 0, "velocity": 1.0}])
        out = self.p("out-exists")
        os.makedirs(out)
        marker = os.path.join(out, "marker.txt")
        with open(marker, "w") as f:
            f.write("do-not-touch")
        job_path = self.p("job.json")
        with open(job_path, "w") as f:
            json.dump(job, f)
        rc = ks.main(["ko2_set.py", job_path, out])
        self.assertEqual(rc, 1)
        self.assertEqual(os.listdir(out), ["marker.txt"])
        with open(marker) as f:
            self.assertEqual(f.read(), "do-not-touch")

    def test_inputs_unchanged(self):
        pad = self.make_pad(freq=230.0, gain_db=-2.0)
        job = self.job(
            [pad], events=[{"pad_id": "kick", "beat": 0, "velocity": 0.9}])
        rc, _, job_path = self.run_cli(job, out_name="out-keep")
        self.assertEqual(rc, 0)
        self.assertEqual(file_sha256(pad["path"]), pad["sha256"])
        with open(job_path) as f:
            on_disk = json.load(f)
        self.assertEqual(on_disk["pads"][0]["sha256"], pad["sha256"])


if __name__ == "__main__":
    unittest.main()
