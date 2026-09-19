import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from mido import MidiFile, bpm2tempo

PIPELINE_DIR = Path(__file__).resolve().parents[1] / "pipeline"
sys.path.insert(0, str(PIPELINE_DIR))

import common as project_common
import compose


class ComposeMidiExportTests(unittest.TestCase):
    def make_spec(self, *, bpm=120.0, total_bars=1, drum_pattern=None, bass_pattern=None, chop_placements=None):
        return SimpleNamespace(
            bpm=bpm,
            total_bars=total_bars,
            drum_pattern=drum_pattern if drum_pattern is not None else {},
            bass_pattern=bass_pattern if bass_pattern is not None else {},
            chop_placements=chop_placements if chop_placements is not None else [],
        )

    def read_track(self, path: Path):
        midi = MidiFile(str(path))
        self.assertEqual(midi.ticks_per_beat, compose.TICKS_PER_BEAT)
        self.assertEqual(len(midi.tracks), 1)
        absolute = []
        tick = 0
        for message in midi.tracks[0]:
            tick += message.time
            absolute.append((tick, message))
        return absolute

    def test_legacy_default_chop_gate_is_192_ticks_and_trailing_end_is_96(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            spec = self.make_spec(
                total_bars=1,
                chop_placements=[
                    {"bar": 0, "step": 0, "midi_note": 60, "gain": 1.0}
                ],
            )

            paths = compose.export_midi(spec, out_dir)
            events = self.read_track(paths["chops"])

            note_on = next((tick, msg) for tick, msg in events if msg.type == "note_on")
            note_off = next((tick, msg) for tick, msg in events if msg.type == "note_off")
            end = next((tick, msg) for tick, msg in events if msg.type == "end_of_track")

            self.assertEqual(note_on[0], 0)
            self.assertEqual(note_off[0] - note_on[0], 192)
            self.assertEqual(end[0] - note_off[0], 96)

    def test_v1_one_beat_chop_gate_is_96_ticks(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            # At 96 BPM, 0.625 seconds is exactly one beat.
            spec = self.make_spec(
                bpm=96.0,
                total_bars=1,
                chop_placements=[
                    {
                        "bar": 0,
                        "step": 0,
                        "midi_note": 60,
                        "gain": 1.0,
                        "timing_basis": "phrase_schedule_v1",
                        "stretch_to": 0.625,
                    }
                ],
            )

            paths = compose.export_midi(spec, out_dir)
            events = self.read_track(paths["chops"])

            note_on = next(tick for tick, msg in events if msg.type == "note_on")
            note_off = next(tick for tick, msg in events if msg.type == "note_off")
            self.assertEqual(note_off - note_on, 96)

    def test_v1_fractional_bpm_uses_exact_float_tempo(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            bpm = 92.7
            spec = self.make_spec(
                bpm=bpm,
                total_bars=1,
                chop_placements=[
                    {
                        "bar": 0,
                        "step": 0,
                        "midi_note": 60,
                        "gain": 1.0,
                        "timing_basis": "phrase_schedule_v1",
                        "stretch_to": 60.0 / bpm,
                    }
                ],
            )

            paths = compose.export_midi(spec, out_dir)
            events = self.read_track(paths["chops"])
            tempo = next(msg for tick, msg in events if msg.type == "set_tempo")
            self.assertEqual(tempo.tempo, bpm2tempo(float(bpm)))
            self.assertNotEqual(tempo.tempo, bpm2tempo(round(bpm)))

    def test_v1_all_tracks_preserve_full_timeline_including_empty_tracks(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            spec = self.make_spec(
                bpm=120.0,
                total_bars=2,
                drum_pattern={},
                bass_pattern={},
                chop_placements=[dict(bar=0,step=0,midi_note=60,gain=1.0,
                    timing_basis="phrase_schedule_v1",stretch_to=.5)],
            )

            paths = compose.export_midi(spec, out_dir)
            expected_end = 2 * compose.STEPS_PER_BAR * compose.TICKS_PER_STEP
            self.assertEqual(expected_end, 768)

            for key in ("drums", "bass", "chops"):
                events = self.read_track(paths[key])
                end_tick, end_message = events[-1]
                self.assertEqual(end_message.type, "end_of_track")
                self.assertGreaterEqual(end_tick, expected_end)
                self.assertEqual(end_tick, expected_end)

    def test_v1_preserves_final_rests_after_last_noteoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            spec = self.make_spec(
                bpm=120.0,
                total_bars=2,
                chop_placements=[
                    {
                        "bar": 0,
                        "step": 0,
                        "midi_note": 60,
                        "gain": 1.0,
                        "timing_basis": "phrase_schedule_v1",
                        # One beat at 120 BPM = 96 ticks.
                        "stretch_to": 0.5,
                    }
                ],
            )

            paths = compose.export_midi(spec, out_dir)
            for key in ("drums", "bass", "chops"):
                events = self.read_track(paths[key])
                end_tick, end_message = events[-1]
                self.assertEqual(end_message.type, "end_of_track")
                self.assertEqual(end_tick, 768)

    def test_same_pitch_adjacent_notes_sort_noteoff_before_noteon(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            notes = [
                (0, 96, 60, 100, 1),
                (96, 96, 60, 100, 1),
            ]
            path = out_dir / "adjacent.mid"

            compose._write_midi(path, notes, 120.0, "Adjacent")
            events = self.read_track(path)
            at_96 = [(tick, msg.type) for tick, msg in events if tick == 96]

            self.assertEqual(at_96, [(96, "note_off"), (96, "note_on")])

    def test_invalid_v1_gates_are_rejected_before_writing_outputs(self):
        for bad_stretch_to in (float("nan"), float("inf"), float("-inf"), -1.0, 0.0):
            with self.subTest(bad_stretch_to=bad_stretch_to), tempfile.TemporaryDirectory() as tmp:
                out_dir = Path(tmp)
                spec = self.make_spec(
                    bpm=120.0,
                    total_bars=1,
                    chop_placements=[
                        {
                            "bar": 0,
                            "step": 0,
                            "midi_note": 60,
                            "gain": 1.0,
                            "timing_basis": "phrase_schedule_v1",
                            "stretch_to": bad_stretch_to,
                        }
                    ],
                )

                with self.assertRaises(ValueError):
                    compose.export_midi(spec, out_dir)

                self.assertEqual(list(out_dir.iterdir()), [])

    def test_invalid_v1_bpm_rejected(self):
        for bad_bpm in (float("nan"), float("inf"), float("-inf"), -1.0, 0.0):
            with self.subTest(bad_bpm=bad_bpm), tempfile.TemporaryDirectory() as tmp:
                out_dir = Path(tmp)
                spec = self.make_spec(
                    bpm=bad_bpm,
                    total_bars=1,
                    chop_placements=[
                        {
                            "bar": 0,
                            "step": 0,
                            "midi_note": 60,
                            "gain": 1.0,
                            "timing_basis": "phrase_schedule_v1",
                            "stretch_to": 1.0,
                        }
                    ],
                )

                with self.assertRaises(ValueError):
                    compose.export_midi(spec, out_dir)

                self.assertEqual(list(out_dir.iterdir()), [])


    def test_negative_drum_offset_at_bar_zero_clamps_to_start_zero(self):
        spec = SimpleNamespace(
            bpm=120.0,
            total_bars=1,
            drum_pattern={0: {"kick": {0: {"offset_ms": -100, "velocity": 100}}}},
            bass_pattern={},
            chop_placements=[],
        )
        with tempfile.TemporaryDirectory() as tmp:
            paths = compose.export_midi(spec, Path(tmp))
            midi = MidiFile(paths["drums"])
            on, off = (m for m in midi.tracks[0] if m.type.startswith("note_"))
            self.assertEqual(on.time, 0)
            self.assertEqual(off.time, 12)
            self.assertEqual(on.note, compose.DRUM_NOTES["kick"])

if __name__ == "__main__":
    unittest.main()
