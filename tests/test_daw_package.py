"""Actual portable package, corrupt media, missing metadata and float sum cases."""
from pathlib import Path
from types import SimpleNamespace
import hashlib,json,shutil,sys,tempfile,unittest
import numpy as np
import soundfile as sf
from mido import MidiFile,MidiTrack
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'pipeline'))
import arrangement,render,daw_export

class PackageTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.root=Path(self.tmp.name);self.cand=self.root/'candidate';self.cand.mkdir()
  source=self.root/'source.wav';t=np.arange(render.SR)/render.SR
  sf.write(source,.95*np.sin(2*np.pi*330*t),render.SR,subtype='FLOAT')
  p=dict(file=str(source),start_sec=0,end_sec=1,gain=1,bar=0)
  spec=SimpleNamespace(bpm=120,total_bars=1,beat_id='roundtrip',drum_pattern={},bass_pattern={},chop_placements=[p],vocal_placements=[p],sections=[])
  self.arr=arrangement.build_arrangement(spec,dict(recipe_id='roundtrip:stem',kind='stem'),{})
  render.render_candidate(self.arr,self.cand)
  (self.cand/'midi').mkdir()
  for name in ('drums','bass','chops'):
   mid=MidiFile();mid.tracks.append(MidiTrack());mid.save(self.cand/'midi'/f'{name}.mid')
  for name in ('recipe','provenance','spec'): (self.cand/f'{name}.json').write_text('{}')
  _,self.package=daw_export.build_project_package(self.cand,self.arr,recipe_path=self.cand/'recipe.json',provenance_path=self.cand/'provenance.json',spec_path=self.cand/'spec.json')
 def test_relocated_valid_package(self):
  target=self.root/'moved';shutil.copytree(self.package,target)
  self.assertEqual(daw_export.verify_project_package(target),[])
 def test_corrupt_audio_even_with_updated_hash(self):
  p=next((self.package/'Samples/Processed').glob('*.wav'));p.write_bytes(b'not a wav file')
  manifest=json.loads((self.package/'manifest.json').read_text())
  for f in manifest['files']:
   if f['path']==p.relative_to(self.package).as_posix():
    f.update(bytes=p.stat().st_size,sha256=hashlib.sha256(p.read_bytes()).hexdigest())
  (self.package/'manifest.json').write_text(json.dumps(manifest))
  self.assertTrue(daw_export.verify_project_package(self.package))
 def test_missing_manifest_and_metadata(self):
  for name in ('manifest.json','recipe.json','spec.json','provenance.json'):
   p=self.package/name;b=p.read_bytes();p.unlink()
   self.assertTrue(daw_export.verify_project_package(self.package),name);p.write_bytes(b)
 def test_reject_size_hash_mismatch(self):
  p=self.package/'recipe.json';p.write_text('{"changed":true}')
  self.assertTrue(daw_export.verify_project_package(self.package))
 def test_omitted_required_entry(self):
  p=self.package/'manifest.json';d=json.loads(p.read_text());d['files']=[r for r in d['files'] if r['path']!='recipe.json'];p.write_text(json.dumps(d))
  self.assertTrue(daw_export.verify_project_package(self.package))
 def test_symlink_parent_and_traversal(self):
  p=self.package/'manifest.json';d=json.loads(p.read_text());d['files'].append(dict(path='../source.wav',bytes=1,sha256='bad'));p.write_text(json.dumps(d))
  self.assertTrue(daw_export.verify_project_package(self.package))
 def test_float_stems_reconstruct_over_unity_premaster(self):
  pre,_=sf.read(self.cand/'premaster_mix.wav')
  stems=[sf.read(p)[0] for p in (self.cand/'stems').glob('*.wav')]
  self.assertGreater(np.max(abs(pre)),1.0)
  self.assertLess(np.max(abs(sum(stems)-pre)),2e-7)

 def test_resume_does_not_accept_corrupt_package(self):
  from unittest.mock import patch
  import common
  run=self.root/'beats'/'resume';run.mkdir(parents=True)
  for kind in ('loop','chop','stem'):
   shutil.copytree(self.cand,run/kind)
   (run/kind/'arrangement.json').write_text(arrangement.dumps(self.arr))
  (run/'run_manifest.json').write_text('{"status":"generated"}')
  (run/'chop/project/recipe.json').write_text('{"broken":"changed"}')
  with patch.object(common,'ROOT',self.root),patch.object(render.recipes,'job_status',return_value='generated'):
   self.assertFalse(render._already_generated('resume'))
