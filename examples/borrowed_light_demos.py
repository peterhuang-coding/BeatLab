"""Three original phrase-flip demos. Run with --root pointing to the media library.

Coding Plan proposed narrative/motif drafts; the curated score below fixes timing,
harmony and orchestration. Existing tracks are never overwritten. No model audio API.
"""
from copy import deepcopy
import argparse,gzip,hashlib,json,math,shutil
from pathlib import Path
import xml.etree.ElementTree as ET
import numpy as np
import soundfile as sf
from mido import MidiFile, MetaMessage
from pipeline.song import render_score,_midi
from pipeline.sample_flip import make_slice
from pipeline.ableton_export import export_song,validate_song
from examples.windowlight_drum_practice import build_rack,xml,value

CONFIGS = [
 dict(slug='pocket-sun',title='Pocket Sun / 口袋里的太阳',bpm=98,bars=32,
      chords=[[53,57,60,64],[53,57,60,62],[57,60,64,67],[53,58,62,67]],roots=[38,34,41,36],
      sections=[('First light',0,4),('Pocket groove',4,8),('Vocal lift',12,8),('Suspended',20,4),('Sun return',24,8)]),
 dict(slug='applause-machine',title='Applause Machine / 掌声机器',bpm=144,bars=48,
      chords=[[53,57,62,64],[55,58,62,67],[53,57,62,65],[55,58,61,64]],roots=[38,39,34,33],
      sections=[('Wiring',0,8),('Machine',8,12),('Pressure',20,12),('Half-time turn',32,8),('Human return',40,8)]),
 dict(slug='no-curtain-call',title='No Curtain Call / 不用谢幕',bpm=82,bars=24,
      chords=[[53,57,60,64],[53,57,60,62],[57,60,64,67],[53,58,62,67]],roots=[38,34,41,36],
      sections=[('Empty hall',0,4),('Small voice',4,8),('Close conversation',12,8),('Morning',20,4)])]

def sha(p):
 with Path(p).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def save(p,obj):Path(p).write_text(json.dumps(obj,ensure_ascii=False,indent=2)+'\n')
def event(beat,note,length,velocity=.7):
 return dict(beat=round(beat,5),note=note,duration_beats=round(length,5),velocity=velocity)

def build(root,c):
 run='borrowed-light-'+c['slug']+'-20260919-v1'
 out=root/'beats'/run; prepared=root/'library/instruments'/run; delivery=root/'exports'/run
 for p in (out,prepared,delivery):
  if p.exists():raise FileExistsError(f'Preserve version: {p}')
 old=json.loads((root/'beats/windowlight-v2/score.json').read_text())
 catalog=json.loads((root/'library/instruments/windowlight-v2/source_catalog.json').read_text())
 templates={t['id']:deepcopy(t) for t in old['tracks']}
 prepared.mkdir(parents=True)
 provenance=[]
 for tid,t in templates.items():
  p=Path(old['sample_root'])/t['sample'];record=next(r for r in catalog if r['id']==tid)
  if sha(p)!=record['sha256']:raise ValueError(f'Source changed: {tid}')
  shutil.copy2(p,prepared/p.name);t['events']=[]
  provenance.append({**record,'file':str(prepared/p.name),'parent_collected_file':str(p)})
 bpm,bars=c['bpm'],c['bars'];beat_s=60/bpm
 # Compose an eight-bar musical source, before cutting it into a new performance.
 source_tracks=[deepcopy(templates['rhodes']),deepcopy(templates['piano'])]
 for t in source_tracks:t.update(gain_db=-17 if t['id']=='rhodes' else -25,room=.10,highpass_hz=130)
 for chord,notes in enumerate(c['chords']):
  for at,dur,vel in [(0,2.1,.7),(2.75,1.0,.48),(4.4,2.5,.58),(7,.75,.44)]:
   for i,note in enumerate(notes):source_tracks[0]['events'].append(event(chord*8+at+i*.012,note,dur,vel))
  melody=[notes[-1]+12,notes[-2]+12,notes[-1]+12,notes[1]+12]
  for at,note,dur in zip([.35,1.65,4.7,6.15],melody,[.8,1.1,.65,1.1]):
   source_tracks[1]['events'].append(event(chord*8+at,note,dur,.42))
 source_score=dict(title=c['title']+' original phrase',bpm=bpm,bars=8,tail_seconds=0,fade_seconds=.08,
                   sample_root=str(prepared),tracks=source_tracks)
 source_dir=prepared/'original-score';render_score(source_score,source_dir)
 source=source_dir/'full_mix.wav';source_peak=np.max(abs(sf.read(source)[0]))
 slices=[];tracks={};placements=[]
 for chord in range(4):
  for part,start,dur in [('full',0,8),('head',0,4),('tail',4,4),('turn',6,2)]:
   tid=f'phrase_{chord}_{part}'
   audio,sr=make_slice(source,start_s=(chord*8+start)*beat_s,end_s=(chord*8+start+dur)*beat_s,
                       duration_s=dur*beat_s,semitones=12 if part=='turn' else 0,reverse=part=='turn')
   p=prepared/(tid+'.wav');sf.write(p,audio,sr,subtype='FLOAT')
   peak=max(float(np.max(abs(audio))),1e-8)
   tracks[tid]=dict(id=tid,name=f'Phrase {chord+1} {part}',sample=p.name,root_midi=60,
       gain_db=(-18 if c['slug']!='applause-machine' else -20)+20*math.log10(peak/source_peak),
       highpass_hz=190,lowpass_hz=6200,room=.06,events=[])
   slices.append(dict(id=tid,midi_note=48+len(slices),source_file=str(source),source_sha256=sha(source),
       start_seconds=(chord*8+start)*beat_s,end_seconds=(chord*8+start+dur)*beat_s,
       target_beats=dur,semitones=12 if part=='turn' else 0,reverse=part=='turn',file=str(p),sha256=sha(p)))
 for tid in ['piano','bass','kick','snare','hat','shaker','rim','open_hat','vocal_oh','vocal_ah','choir']:
  tracks[tid]=deepcopy(templates[tid]);tracks[tid]['events']=[]
 gains=dict(piano=-21,bass=-16,kick=-13,snare=-20,hat=-31,shaker=-34,rim=-28,open_hat=-33,vocal_oh=-18,vocal_ah=-20,choir=-29)
 if c['slug']=='no-curtain-call':gains.update(kick=-19,snare=-28,hat=-35,bass=-20,piano=-17,vocal_oh=-22)
 if c['slug']=='applause-machine':gains.update(kick=-12,snare=-17,rim=-23,bass=-14,choir=-23,vocal_oh=-20)
 for tid,gain in gains.items():tracks[tid]['gain_db']=gain
 tracks['bass'].update(highpass_hz=30,lowpass_hz=680)
 tracks['vocal_oh'].update(room=.12,highpass_hz=230,lowpass_hz=6500)
 tracks['vocal_ah'].update(room=.15,highpass_hz=250,pan=.18)
 lookup={s['id']:s for s in slices}
 def hit(tid,beat,note,length,vel=.7):
  if 0<=beat<bars*4:tracks[tid]['events'].append(event(beat,note,min(length,bars*4-beat),vel))
 def cut(tid,at,length,vel=.7):
  hit(tid,at,60,length,vel);placements.append(event(at,lookup[tid]['midi_note'],length,vel))
 for bar in range(0,bars,2):
  at=bar*4;ch=(bar//2)%4;slug=c['slug'];quiet=slug=='no-curtain-call'
  halftime=slug=='applause-machine' and 32<=bar<40
  if bar<4 or (quiet and bar>=20) or halftime:
   cut(f'phrase_{ch}_full',at,7.8,.48 if quiet else .65)
  elif (bar//2)%3==0:
   cut(f'phrase_{ch}_head',at,2.5,.72);cut(f'phrase_{ch}_head',at+2.75,1,.50)
   cut(f'phrase_{ch}_tail',at+4.1,2.85,.68);cut(f'phrase_{ch}_turn',at+7,1,.36)
  else:
   cut(f'phrase_{ch}_head',at,3.45,.70);cut(f'phrase_{ch}_tail',at+4.35,3.4,.63)
  root_note=c['roots'][ch]
  if bar>=4:
   pattern=[(.015,root_note,1.6,.7),(2.65,root_note,.7,.48),(4.02,root_note,1.7,.64),(6.65,root_note+7,.72,.42)]
   if quiet or halftime:pattern=[(.02,root_note,2.5,.56),(4.2,root_note,2.2,.48)]
   for off,note,length,vel in pattern:hit('bass',at+off,note,length,vel)
  # Foreground roles take turns, instead of layering every motif throughout.
  chord_notes=c['chords'][ch]
  melody=([64,65,69,67] if ch in [0,2] else [62,65,69,65] if ch==1 else [62,65,67,65])
  if slug=='applause-machine':melody=[chord_notes[-1]+12,chord_notes[1]+12,chord_notes[2]+12,chord_notes[-1]+12]
  voice_leads=bar>=4 and (bar//2)%2==0
  for idx,(off,length) in enumerate([(0.2,.6),(1.65,.9),(3.25,.55),(4.75,1.15)]):
   if quiet:off=idx*1.55+.15;length=1.1
   tid='vocal_oh' if voice_leads else 'piano'
   hit(tid,at+off,melody[idx],length,.66 if voice_leads else .5)
  if bar>=4:
   hit('vocal_ah' if voice_leads else 'vocal_oh',at+6.25,chord_notes[-1]+12,1.1,.48)
  if (slug=='pocket-sun' and 12<=bar<20) or (slug=='applause-machine' and 20<=bar<32):
   for n in chord_notes[1:3]:hit('choir',at+5.1,n+12,1.4,.38)
 for bar in range(bars):
  at=bar*4;slug=c['slug'];quiet=slug=='no-curtain-call'
  if bar<4 or bar==bars-1:continue
  if slug=='applause-machine' and bar<8:
   hit('rim',at+1,37,.2,.5);hit('rim',at+3.35,37,.2,.38);continue
  half=slug=='applause-machine' and 32<=bar<40
  sparse=(slug=='pocket-sun' and 20<=bar<24) or quiet or half
  kicks=[0,2.6] if sparse else ([.015,1.65,2.5,3.75] if bar%2==0 else [.015,1.85,3.1])
  for i,off in enumerate(kicks):hit('kick',at+off,36,.32,.77 if i==0 else .59)
  for off in ([2.025] if half else [1.035,3.035]):
   hit('rim' if sparse and not half else 'snare',at+off,37 if sparse and not half else 38,.26,.7)
  for j,off in enumerate([.05,.57,1.04,1.59,2.04,2.59,3.04,3.59]):
   if sparse and j%2==0:continue
   hit('hat',at+off,42,.12,.35 if j%2==0 else .48)
  if not sparse and bar%4==3:
   hit('open_hat',at+3.52,46,.35,.37)
   hit('snare',at+3.77,38,.15,.29)
  if slug=='pocket-sun' and not sparse:
   for j in range(4):hit('shaker',at+j+.78,70,.12,.26+(j%2)*.08)
 # Prune unused parts: every exported stem and MIDI track has actual notes.
 definitions=[t for t in tracks.values() if t['events']]
 score=dict(title=c['title'],bpm=bpm,bars=bars,tail_seconds=2,fade_seconds=2,
            sample_root=str(prepared),tracks=definitions,
            sections=[dict(name=name,start_bar=start,bars=count) for name,start,count in c['sections']],
            license=dict(status='private creative demo; source rights retained',
                         composition='original note score and original-phrase resampling',
                         vocals='Ableton Core Library wordless syllables; not new recorded lyrics'))
 render_score(score,out)
 shutil.copy2(source,out/'original-phrase.wav');save(out/'source-provenance.json',provenance)
 used={t['id'] for t in definitions};slices=[s for s in slices if s['id'] in used]
 save(out/'slice-map.json',slices);_midi(placements,bpm,out/'phrase-chops.mid')
 mid=MidiFile(out/'phrase-chops.mid')
 for lane in mid.tracks:
  lane[:]=[m for m in lane if m.type!='end_of_track']
  lane.append(MetaMessage('end_of_track',time=bars*4*mid.ticks_per_beat-sum(m.time for m in lane)))
 mid.save(out/'phrase-chops.mid')
 solo=np.zeros_like(sf.read(out/'full_mix.wav',dtype='float32',always_2d=True)[0])
 for t in definitions:
  if t['id'].startswith('phrase_'):solo+=sf.read(out/'stems'/f'{t["id"]}.wav',dtype='float32',always_2d=True)[0]
 sf.write(out/'sample-flip-solo.wav',solo,44100,subtype='FLOAT')
 export_song(out,delivery/'AbletonProject',set_name=c['slug'])
 rack=delivery/'ChopRack';(rack/'Samples').mkdir(parents=True)
 pads=[]
 for s in slices:
  rel='Samples/'+s['id']+'.wav';shutil.copy2(s['file'],rack/rel)
  pads.append(dict(midi=s['midi_note'],name=s['id'],choke=1,file=rel))
 build_rack(pads,rack,rack)
 temp=rack/'Windowlight 16 Pads.adg';tree=xml(temp);value(tree,'.//DrumGroupDevice/UserName',c['slug']+' phrase pads')
 (rack/'Phrase Pads.adg').write_bytes(gzip.compress(ET.tostring(tree,encoding='utf-8',xml_declaration=True),mtime=0));temp.unlink()
 shutil.copy2(out/'phrase-chops.mid',rack/'phrase-chops.mid');save(rack/'pads.json',pads)
 evidence=validate_song(out);evidence.update(run_id=run,composition='original phrase sampling',
    phrase_slices=len(slices),user_listening='pending',coding_plan='doubao-seed-evolving draft + curated corrections')
 save(out/'validation.json',evidence)
 return dict(run_id=run,title=c['title'],bpm=bpm,bars=bars,duration=evidence['duration_seconds'],
             mix_sha256=evidence['mix_sha256'],stems=len(definitions),slices=len(slices),stem_sum=evidence['stem_sum'])

def main():
 ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--root',type=Path,required=True);ap.add_argument('--track',choices=[c['slug'] for c in CONFIGS]);args=ap.parse_args()
 results=[build(args.root.resolve(),c) for c in CONFIGS if not args.track or c['slug']==args.track]
 print(json.dumps(results,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
