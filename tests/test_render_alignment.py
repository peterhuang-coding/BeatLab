"""Real-audio round trips for the unified arrangement renderer."""
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
import numpy as np
import soundfile as sf
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'pipeline'))
import arrangement
import render

class VocalTimingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root/'voice.wav'
        t = np.arange(12*render.SR)/render.SR
        sf.write(self.source, .25*np.sin(2*np.pi*330*t), render.SR, subtype='FLOAT')

    def make(self, seconds, stretch=None):
        p = dict(file=str(self.source),start_sec=0,end_sec=seconds,gain=.5,bar=0)
        if stretch: p['stretch_to'] = stretch
        spec = SimpleNamespace(bpm=60,total_bars=3,beat_id='fixture',
            drum_pattern={},bass_pattern={},chop_placements=[],vocal_placements=[p],sections=[])
        return arrangement.build_arrangement(spec,dict(recipe_id='fixture:stem',kind='stem'),{})

    def check_route(self, arr, duration):
        event = arrangement.track(arr,'track-vocals')['events'][0]
        self.assertAlmostEqual(event['gain'], .75)
        self.assertAlmostEqual(event['duration_beats'], duration)
        layers = render._render_layers(arr,self.root,12*render.SR)
        asset = arrangement.assets_by_id(arr)[event['media_asset_id']]
        media,sr=sf.read(self.root/asset['source_path'])
        self.assertEqual(len(media),round(duration*sr))
        expected=media*event['gain']/np.sqrt(2)
        self.assertLess(np.max(abs(layers['vocal'][0][:len(media)]-expected)),1e-7)
        self.assertEqual(np.count_nonzero(layers['vocal'][0][len(media):]),0)

    def test_short_vocal_matches_declared_gain(self): self.check_route(self.make(2),2)
    def test_unstretched_vocal_accents_keep_the_eight_second_limit(self): self.check_route(self.make(10),8)
    def test_stretched_vocal_is_not_capped_at_eight_seconds(self): self.check_route(self.make(2,10),10)

    def test_float_dry_audio_preserves_over_unity(self):
        x=np.array([0.,1.284515,-1.25],dtype=np.float32)
        path=self.root/'premaster.wav'
        render._write_stereo(path,x,x,dry=True)
        y,sr=sf.read(path)
        self.assertEqual(sf.info(path).subtype,'FLOAT')
        self.assertLess(np.max(abs(y[:,0]-x)),1e-7)
