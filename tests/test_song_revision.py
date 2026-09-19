from pathlib import Path
import importlib,json,sys,tempfile,unittest
import numpy as np
import soundfile as sf
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'pipeline'))
from song import render_score
from ableton_export import validate_song

class RevisionTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.root=Path(self.tmp.name);self.parent=self.root/'parent'
  t=np.arange(11025)/22050;source=self.root/'tone.wav';sf.write(source,.3*np.sin(2*np.pi*440*t),22050,subtype='FLOAT')
  self.score=dict(title='test',bpm=120,bars=1,tail_seconds=.2,sample_root=str(self.root),tracks=[dict(id=tid,sample='tone.wav',root_midi=69,gain_db=-12,events=[dict(beat=i,note=69,duration_beats=.8,velocity=.7)]) for i,tid in enumerate(('voice','keys'))])
  render_score(self.score,self.parent,sr=22050)
 def revise(self,change):
  try: module=importlib.import_module('revision')
  except ImportError: self.fail('revision module must implement real feedback rendering')
  return module.revise_song(self.parent,change)
 def test_real_change_retains_other_layer_and_is_idempotent(self):
  before=(self.parent/'full_mix.wav').read_bytes()
  result=self.revise({'voice':-6});child=Path(result['song'])
  self.assertNotEqual((child/'full_mix.wav').read_bytes(),before)
  self.assertEqual((self.parent/'full_mix.wav').read_bytes(),before)
  a=sf.read(self.parent/'stems/keys.wav')[0];b=sf.read(child/'stems/keys.wav')[0]
  self.assertLess(np.max(abs(a-b)),2e-7)
  a=sf.read(self.parent/'stems/voice.wav')[0];b=sf.read(child/'stems/voice.wav')[0]
  self.assertAlmostEqual(np.sqrt(np.sum(b*b)/np.sum(a*a)),10**(-6/20),places=5)
  self.assertEqual(self.revise({'voice':-6})['song'],str(child))
  self.assertEqual(json.loads((child/'revision.json').read_text())['parent'],str(self.parent.resolve()))
  validate_song(child)
 def test_unknown_noop_nonfinite_rejected(self):
  for changes in ({'missing':-6},{'voice':0},{'voice':float('nan')},{'voice':True}):
   with self.subTest(changes=changes):
    with self.assertRaises(ValueError): self.revise(changes)
 def test_missing_original_source_does_not_publish_child(self):
  (self.root/'tone.wav').unlink()
  with self.assertRaises((ValueError,FileNotFoundError)): self.revise({'voice':-6})
  self.assertFalse(list(self.root.glob('parent-r*')))
 def test_score_track_omission_cannot_silently_remove_layer(self):
  path=self.parent/'score.json';data=json.loads(path.read_text());data['tracks'].pop();path.write_text(json.dumps(data))
  with self.assertRaises(ValueError):self.revise({'voice':-6})
