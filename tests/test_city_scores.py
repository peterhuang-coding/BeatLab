import json,pathlib,unittest,copy,hashlib
from examples import ten_beat_scores as m
fixtures=json.loads(pathlib.Path(__file__).with_name("fixtures").joinpath("city-prepared.json").read_text())

class CityScores(unittest.TestCase):
    def test_all_real_shapes_build_and_protect_contract(self):
        signatures=set()
        for p in fixtures[:6]:
            with self.subTest(number=p['number']):
                original=copy.deepcopy(p);s=m.build_score(p);self.assertEqual(p,original);self.assertEqual(s,m.build_score(p))
                self.assertEqual(s['bars'],p['bars']);self.assertEqual(s['bpm'],p['bpm'])
                self.assertEqual(s['sample_flip']['source_records'],p['source_records'])
                self.assertIsNone(s['metadata']['keep']);limit=p['bars']*4
                ps=sorted(s['sample_flip']['placements'],key=lambda x:x['beat']);self.assertGreaterEqual(len({x['slice_id'] for x in ps}),8)
                for a,b in zip(ps,ps[1:]):self.assertLessEqual(a['beat']+a['duration_beats'],b['beat']+1e-6)
                for t in s['tracks']:
                    for e in t['events']:
                        self.assertGreaterEqual(e['beat'],0);self.assertGreater(e['duration_beats'],0);self.assertLessEqual(e['beat']+e['duration_beats'],limit+1e-6)
                        if t['id'] in ['piano','vibes']:
                            for cut in ps:self.assertFalse(e['beat']<cut['beat']+cut['duration_beats'] and cut['beat']<e['beat']+e['duration_beats'])
                bass=next(t['events'] for t in s['tracks'] if t['id']=='bass')
                for a,b in zip(bass,bass[1:]):self.assertLessEqual(a['beat']+a['duration_beats'],b['beat']+1e-6)
                signature=[(t['id'],[(e['beat'],e['duration_beats']) for e in t['events']]) for t in s['tracks'] if t['id'] in ('kick','snare','rim','clap')]
                signatures.add(hashlib.sha256(json.dumps(signature).encode()).hexdigest())
        self.assertEqual(len(signatures),6)
    def test_half_time_snare_is_on_beat_three(self):
        snare=m.FEELS['halftime']['snare'];self.assertEqual(snare[0][0],2)

if __name__=='__main__': unittest.main()
