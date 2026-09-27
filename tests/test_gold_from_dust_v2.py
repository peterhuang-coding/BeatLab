"""Focused local acceptance for the recovered Coding Plan composition draft."""
from collections import Counter
from copy import deepcopy
import math
import unittest
from test_real_record_flip import make_prepared
from examples.gold_from_dust_v2 import build_score, PreparedError


def fixture():
    p = make_prepared()
    for i, row in enumerate(p['slices']):
        row['bass_midi'] = (32, 39, 36)[i % 3]
    for t in p['instruments']:
        t.update(gain_db=-90, lowpass_hz=500)
    p['extra_instruments'] = [
        {'id': x, 'sample': f'{x}.wav', 'root_midi': 60 if x != 'clap' else 39,
         'gain_db': -18} for x in ('piano', 'vibes', 'clap')]
    p['instrument_sources'] = [{'id': t['id'], 'sha256': 'a' * 64}
                               for t in p['instruments'] + p['extra_instruments']]
    return p


class PrismCutTests(unittest.TestCase):
    def test_actual_catalog_schema_and_no_mutation(self):
        p = fixture(); original = deepcopy(p)
        a = build_score(p)
        self.assertEqual(p, original)
        self.assertEqual(a, build_score(p))
        self.assertEqual(a['instrument_sources'], p['instrument_sources'])
        self.assertEqual(a['sample_flip']['slices'], p['slices'])
        self.assertEqual(a['sample_flip']['source_records'], p['source_records'])

    def test_events_and_placements_match(self):
        s = build_score(fixture())
        ids = {t['id'] for t in s['sample_flip']['slices']}
        key = lambda tid,e: (tid,e['beat'],e['duration_beats'],e['velocity'],e.get('note',60))
        actual = Counter(key(t['id'],e) for t in s['tracks'] if t['id'] in ids for e in t['events'])
        expected = Counter(key(p['slice_id'],p) for p in s['sample_flip']['placements'])
        self.assertEqual(actual,expected)
        self.assertEqual(len({p['slice_id'] for p in s['sample_flip']['placements']}),10)
        for t in s['tracks']:
            self.assertTrue(t['events'])
            for e in t['events']:
                self.assertTrue(all(math.isfinite(e[k]) for k in ('beat','duration_beats','velocity')))
                self.assertTrue(0 <= e['beat'] < e['beat']+e['duration_beats'] <= 80)
                self.assertTrue(0 < e['velocity'] <= 1)
        for events in (s['sample_flip']['placements'],next(t['events'] for t in s['tracks'] if t['id']=='bass')):
            events = sorted(events,key=lambda e:e['beat'])
            for a,b in zip(events,events[1:]): self.assertLessEqual(a['beat']+a['duration_beats'],b['beat'])

    def test_bright_backing_overrides_parent_settings(self):
        tracks = {t['id']:t for t in build_score(fixture())['tracks']}
        for tid in ('kick','snare','hat','bass'):
            self.assertGreater(tracks[tid]['gain_db'],-90)
            self.assertGreater(tracks[tid]['lowpass_hz'],500)
        self.assertGreaterEqual(tracks['piano']['lowpass_hz'],10000)
        self.assertGreaterEqual(tracks['hat']['lowpass_hz'],12000)

    def test_break_and_drop_gap_are_real(self):
        s = build_score(fixture())
        for t in s['tracks']:
            for e in t['events']:
                self.assertFalse(e['beat'] < 76 and e['beat']+e['duration_beats'] > 74)
                if t.get('drum'): self.assertFalse(32 <= e['beat'] < 40)
        piano = next(t for t in s['tracks'] if t['id']=='piano')
        self.assertTrue(any(32 <= e['beat'] < 40 for e in piano['events']))
        self.assertEqual(sum(x['bars'] for x in s['sections']),20)

    def test_bass_follows_changed_slice_root(self):
        p = fixture()
        for row in p['slices']: row['bass_midi']=39
        s=build_score(p);bass=next(t for t in s['tracks'] if t['id']=='bass')
        for beat in (8,16,24,40,48,64,76):
            self.assertEqual(next(e['note'] for e in bass['events'] if e['beat']==beat),39)

    def test_invalid_extras_rejected(self):
        for mode in ('missing','duplicate','root','gain'):
            with self.subTest(mode=mode):
                p=fixture()
                if mode=='missing': p['extra_instruments'].pop()
                elif mode=='duplicate': p['extra_instruments'][1]['id']='piano'
                elif mode=='root': p['extra_instruments'][0]['root_midi']=200
                else: p['extra_instruments'][0]['gain_db']=float('nan')
                with self.assertRaises(PreparedError): build_score(p)


if __name__ == '__main__':
    unittest.main()
