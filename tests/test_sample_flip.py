import importlib
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np
import soundfile as sf

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'pipeline'))


class SampleFlipTests(unittest.TestCase):
    def module(self):
        try:
            mod=importlib.import_module('sample_flip')
        except ModuleNotFoundError:
            mod=None
        self.assertIsNotNone(mod, 'phrase slicing renderer is missing')
        return mod

    def test_source_region_not_first_region_and_pitch_preserves_gate(self):
        mod=self.module()
        with tempfile.TemporaryDirectory() as d:
            sr=22050;t=np.arange(sr)/sr
            x=np.concatenate([.4*np.sin(2*np.pi*220*t),.4*np.sin(2*np.pi*440*t)])
            p=Path(d)/'source.wav';sf.write(p,x,sr,subtype='FLOAT')
            y,rate=mod.make_slice(p,start_s=1,end_s=2,semitones=12,duration_s=.75)
            self.assertEqual(len(y),round(.75*sr));self.assertEqual(rate,sr)
            z=y[2000:-2000,0]
            freq=np.fft.rfftfreq(len(z),1/sr)[np.argmax(abs(np.fft.rfft(z)))]
            self.assertAlmostEqual(freq,880,delta=8)
            self.assertLess(abs(y[0,0]),1e-5)
            self.assertLess(abs(y[-1,0]),1e-5)

    def test_reverse_changes_attack_position(self):
        mod=self.module()
        with tempfile.TemporaryDirectory() as d:
            sr=22050;x=np.zeros(sr);x[2000:5000]=.4*np.sin(2*np.pi*440*np.arange(3000)/sr)
            p=Path(d)/'source.wav';sf.write(p,x,sr,subtype='FLOAT')
            forward,_=mod.make_slice(p,start_s=0,end_s=1,semitones=0,duration_s=1)
            reverse,_=mod.make_slice(p,start_s=0,end_s=1,semitones=0,duration_s=1,reverse=True)
            self.assertGreater(np.sum(forward[:sr//2]**2),np.sum(forward[sr//2:]**2)*50)
            self.assertGreater(np.sum(reverse[sr//2:]**2),np.sum(reverse[:sr//2]**2)*50)

    def test_out_of_range_source_is_rejected(self):
        mod=self.module()
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'source.wav';sf.write(p,np.ones(1000)*.1,1000)
            for start,end,duration in [(-1,.5,1),(0,2,1),(.5,.2,1),(0,.5,0)]:
                with self.assertRaises(ValueError):
                    mod.make_slice(p,start_s=start,end_s=end,duration_s=duration)


if __name__=='__main__':
    unittest.main()
