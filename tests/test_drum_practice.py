import importlib.util
from pathlib import Path
import tempfile
import unittest
from mido import MidiFile

MODULE = Path(__file__).resolve().parents[1] / 'examples/windowlight_drum_practice.py'


class DrumPracticeTest(unittest.TestCase):
    def module(self):
        self.assertTrue(MODULE.is_file(), 'MIDI drum practice builder is missing')
        spec = importlib.util.spec_from_file_location('practice', MODULE)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_sixteen_pads_have_unique_notes_and_hat_choke(self):
        mod = self.module()
        pads = mod.pads()
        self.assertEqual([p['midi'] for p in pads], list(range(36, 52)))
        by_source = {p['source_id']: p for p in pads}
        self.assertEqual(by_source['hat']['choke'], by_source['open_hat']['choke'])
        self.assertNotEqual(by_source['kick']['choke'], by_source['hat']['choke'])

    def test_patterns_roundtrip_with_full_bar_length_and_velocity(self):
        mod = self.module()
        patterns = mod.patterns()
        self.assertEqual(len(patterns), 4)
        with tempfile.TemporaryDirectory() as d:
            for pattern in patterns:
                dest = Path(d) / (pattern['id'] + '.mid')
                mod.write_midi(pattern['events'], 32, dest)
                mid = MidiFile(dest)
                self.assertEqual(sum(m.time for m in mid.tracks[0]), 32 * mid.ticks_per_beat)
                ons = [m for m in mid.tracks[0] if m.type == 'note_on']
                self.assertTrue(ons)
                self.assertTrue(all(36 <= m.note <= 51 for m in ons))
                self.assertGreater(len({m.velocity for m in ons}), 1)
                self.assertEqual(len(ons), len([m for m in mid.tracks[0] if m.type == 'note_off']))

    def test_unmapped_note_rejected_before_file_write(self):
        mod = self.module()
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d) / 'bad.mid'
            with self.assertRaises(ValueError):
                mod.write_midi([dict(beat=0, note=70, velocity=.5, duration_beats=1)], 32, dest)
            self.assertFalse(dest.exists())


if __name__ == '__main__':
    unittest.main()
