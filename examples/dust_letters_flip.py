"""A phrase-based soul flip: source music -> mapped chops -> a new arrangement."""
from copy import deepcopy
import gzip
import hashlib
import json
import math
from pathlib import Path
import shutil
import xml.etree.ElementTree as ET

import numpy as np
import soundfile as sf
from mido import MidiFile, MidiTrack, Message, MetaMessage, bpm2tempo

try:
    from pipeline.sample_flip import make_slice
    from pipeline.song import render_score
except ModuleNotFoundError as exc:
    if exc.name not in ('pipeline.sample_flip','pipeline.song'):
        raise
    from sample_flip import make_slice
    from song import render_score

REPO=Path(__file__).resolve().parents[1]
CORE=Path('/Applications/Ableton Live 12 Suite.app/Contents/App-Resources/Core Library/Samples/Loops/Tonal')
BPM=92
PARENT_SHA='f339d0743e5f8f0f5d76920fdc0339babd546e9cbc1b0146e096a1562a1e80b7'


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path,data):
    Path(path).write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')


def collect_phrases():
    root=REPO/'library/loops/phrase-bank-v1'
    root.mkdir(parents=True,exist_ok=True)
    records=[]
    choices=[('rhodes-dust','Keys/Rhodes Dust BbMaj 115 bpm.wav',115,'Bb major'),
             ('piano-warped','Keys/Piano Warped G Minor 98 bpm.wav',98,'G minor'),
             ('piano-stabs','Keys/Grand Piano Dirty Stabs E Minor 90 bpm.wav',90,'E minor'),
             ('guitar-muffled','Guitar/Electric Guitar Muffled C# Minor 96 bpm.wav',96,'C# minor')]
    for slug,name,bpm,key in choices:
        source=CORE/name;folder=root/slug;folder.mkdir(exist_ok=True)
        dest=folder/'source.wav'
        if dest.exists() and digest(dest)!=digest(source):
            raise ValueError(f'Existing library source changed: {dest}')
        if not dest.exists():
            shutil.copy2(source,dest)
        record=dict(id=slug,file=str(dest),original_source=str(source),sha256=digest(dest),
                    duration_seconds=sf.info(dest).duration,bpm=bpm,key=key,
                    musical_metadata_basis='factory filename; grid duration checked separately',
                    source_type='factory musical performance loop',commercial_status='needs_review',
                    license_url='https://help.ableton.com/hc/en-us/articles/209768885-Commercial-Use-rights-for-Live-content')
        write_json(folder/'source_manifest.json',record);records.append(record)
    # This is explicitly a resample of our own earlier vocal arrangement, not a new singer recording.
    old=REPO/'beats/windowlight-v2'
    if digest(old/'full_mix.wav')!=PARENT_SHA:
        raise ValueError('Approved Windowlight v2 changed')
    old_manifest=json.loads((old/'run_manifest.json').read_text())
    refs={t['id']:t for t in old_manifest['tracks']}
    start=round(64*60/88*44100);end=round(96*60/88*44100)
    stems=[]
    for tid in ['vocal_oh','vocal_ah']:
        path=old/refs[tid]['file']
        if digest(path)!=refs[tid]['sha256']:
            raise ValueError('Parent vocal stem changed')
        stems.append(sf.read(path,start=start,stop=end,always_2d=True)[0])
    folder=root/'windowlight-vocal-phrase';folder.mkdir(exist_ok=True);dest=folder/'source.wav'
    audio=sum(stems)
    if dest.exists():
        existing,rate=sf.read(dest,always_2d=True)
        if rate!=44100 or existing.shape!=audio.shape or np.max(abs(existing-audio))>2e-7:
            raise ValueError('Existing vocal phrase differs from source stems')
    else:
        sf.write(dest,audio,44100,subtype='PCM_24')
    record=dict(id='windowlight-vocal-phrase',file=str(dest),sha256=digest(dest),bpm=88,
                duration_seconds=len(audio)/44100,key='C major pitch collection',
                source_type='resampled original Windowlight v2 wordless vocal arrangement',
                parent_mix_sha256=PARENT_SHA,parent_start_seconds=64*60/88,parent_end_seconds=96*60/88,
                constituent_stems=[refs[t] for t in ['vocal_oh','vocal_ah']],commercial_status='needs_review')
    write_json(folder/'source_manifest.json',record);records.append(record)
    write_json(root/'catalog.json',records)
    rows='\n'.join(f'| {r["id"]} | {r["duration_seconds"]:.2f} s | {r["bpm"]} | {r["key"]} | [原片段]({r["file"]}) |' for r in records)
    (root/'乐句库.md').write_text('# 音乐乐句采样库\n\n完整音乐片段用于切句、变调、重排和重复；每条均保留 source_manifest.json。\n\n| 素材 | 长度 | BPM | 调性标签 | 试听 |\n|---|---|---|---|---|\n'+rows+'\n\n本库为本地创作素材索引，商业状态仍待核验；尚未接入网络自动挖歌或后台筛选。Rhodes Dust 是本轮主素材，其余片段作为候选保留。\n')
    return root,records


def build():
    out=REPO/'beats/dust-letters-v1'
    if out.exists():
        raise FileExistsError('Keep previous renders; choose a new run directory')
    library,records=collect_phrases()
    refs={r['id']:r for r in records}
    prepared=REPO/'library/instruments/dust-letters-v1'
    if prepared.exists():
        raise FileExistsError(prepared)
    prepared.mkdir(parents=True)
    definitions=[]
    # Beat ranges are in the ORIGINAL performance. Adjacent 2-beat chunks retain musical gestures.
    for i,tid in enumerate('abcdefgh'):
        definitions.append(dict(id=tid,source_id='rhodes-dust',source_beats=[i*2,(i+1)*2],
                                target_beats=2,semitones=2,reverse=False,root=[36,36,41,41,33,33,41,41][i]))
    definitions += [dict(id='reverse',source_id='rhodes-dust',source_beats=[12,14],target_beats=2,semitones=2,reverse=True,root=41),
                    dict(id='octave',source_id='rhodes-dust',source_beats=[0,2],target_beats=2,semitones=14,reverse=False,root=36),
                    dict(id='low',source_id='rhodes-dust',source_beats=[0,8],target_beats=8,semitones=-10,reverse=False,root=36),
                    dict(id='unfold',source_id='rhodes-dust',source_beats=[0,16],target_beats=16,semitones=2,reverse=False,root=36),
                    dict(id='vox_a',source_id='windowlight-vocal-phrase',source_beats=[0,3.5],target_beats=3.5,semitones=0,reverse=False,root=36),
                    dict(id='vox_b',source_id='windowlight-vocal-phrase',source_beats=[8,11.5],target_beats=3.5,semitones=0,reverse=False,root=41)]
    tracks={};slice_records=[]
    for i,definition in enumerate(definitions):
        src=refs[definition['source_id']];start,end=[b*60/src['bpm'] for b in definition['source_beats']]
        audio,sr=make_slice(Path(src['file']),start_s=start,end_s=end,
                            duration_s=definition['target_beats']*60/BPM,
                            semitones=definition['semitones'],reverse=definition['reverse'])
        file=prepared/(definition['id']+'.wav');sf.write(file,audio,sr,subtype='FLOAT')
        peak=float(np.max(abs(audio)))
        # song.py normalizes instrument files internally; compensate to preserve relative phrase dynamics.
        source_peak=float(np.max(abs(sf.read(src['file'],always_2d=True)[0])))
        gain=(-15 if definition['id'].startswith('vox') else -10.5)+20*math.log10(max(peak,1e-9)/max(source_peak,1e-9))
        tracks[definition['id']]=dict(id=definition['id'],name=definition['id'].upper()+' phrase chop',
                                      sample=file.name,root_midi=60,gain_db=gain,
                                      highpass_hz=175 if not definition['id'].startswith('vox') else 180,
                                      lowpass_hz=4700 if definition['id']!='low' else 2600,
                                      room=.06,events=[])
        record=dict(**definition,midi_note=48+i,source_sha256=src['sha256'],start_seconds=start,end_seconds=end,
                    source_file=src['file'],file=str(file),sha256=digest(file))
        slice_records.append(record)
        print('[slice]',definition['id'],definition['source_beats'],'pitch',definition['semitones'],flush=True)
    old_score=json.loads((REPO/'beats/windowlight-v2/score.json').read_text())
    for tid in ['kick','snare','hat','shaker','rim','open_hat','bass']:
        original=next(t for t in old_score['tracks'] if t['id']==tid)
        track=deepcopy(original);track['events']=[]
        path=Path(old_score['sample_root'])/original['sample'];dest=prepared/(tid+path.suffix)
        shutil.copy2(path,dest);track['sample']=dest.name
        tracks[tid]=track
    tracks['bass'].update(gain_db=-15,lowpass_hz=750)
    sections=[('Opening',0,4),('Theme',4,8),('Chopped hook',12,8),('Low bridge',20,4),('Hook variation',24,8),('Closing',32,4)]
    chop_lookup={d['id']:d for d in definitions};placements=[]

    def hit(tid,beat,duration,velocity=.7,note=60):
        tracks[tid]['events'].append(dict(beat=round(beat,4),note=note,duration_beats=duration,velocity=velocity))

    def cut(tid,beat,duration,velocity=.8,with_bass=True):
        hit(tid,beat,duration,velocity)
        placements.append(dict(slice_id=tid,beat=beat,duration_beats=duration,velocity=velocity))
        if with_bass and duration>=.7 and tid in 'abcdefgh':
            hit('bass',beat+.012,min(duration*.68,1.15),.65,chop_lookup[tid]['root'])

    # Expose the intact pitched phrase first, then let it become a new rhythmic theme.
    cut('unfold',0,16,.68,False)
    motif=[('a',0,1.5),('a',1.5,.5),('c',2,2),('e',4,1.5),('g',5.5,.5),('b',6,2)]
    answer=[('a',0,2),('h',2,1.5),('h',3.5,.5),('d',4,2),('f',6,2)]
    hook=[('e',0,.75),('e',.75,.25),('e',1,1),('b',2,1),('b',3,.5),('h',3.5,.5),('a',4,1.5),('c',5.5,.5),('g',6,2)]
    for section,start,bars in sections:
        if section in ['Opening','Low bridge','Closing']:
            continue
        is_hook='hook' in section.lower()
        for phrase in range(bars//2):
            at=(start+phrase*2)*4
            chosen=hook if is_hook and phrase%2==0 else answer if phrase%2 else motif
            for tid,offset,duration in chosen:
                # Evolving ending: last response becomes reverse swelling into next downbeat.
                if phrase==bars//2-1 and offset==6:
                    tid='reverse'
                cut(tid,at+offset,duration,.83 if is_hook else .74)
            if is_hook and phrase in [1,3]:
                cut('vox_a' if phrase==1 else 'vox_b',at+.08,3.5,.58,False)
            if section=='Hook variation' and phrase in [0,2]:
                # Octave answers replace a layer during a brief gap instead of adding an entire bed.
                cut('octave',at+7.5,.48,.25,False)
    cut('low',80,8,.58,False);cut('low',88,8,.56,False)
    cut('vox_a',84.0,3.5,.44,False);cut('reverse',94,2,.44,False)
    for tid,at,duration,vel in [('a',128,2,.70),('c',130,2,.64),('e',132,2,.64),('h',134,2,.60),
                               ('a',136,2,.58),('b',138,2,.52),('a',140,3.7,.42)]:
        cut(tid,at,duration,vel,at<136)
    rng=np.random.default_rng(923)
    for bar in range(36):
        if bar<2 or bar>=34:
            continue
        at=bar*4;bridge=20<=bar<24;hooked=12<=bar<20 or 24<=bar<32
        kicks=[(0,.86),(2.5,.66)] if not bridge else [(0,.65)]
        if hooked and bar%2:
            kicks.append((1.75,.47));kicks.append((3.5,.50))
        for offset,vel in kicks:
            hit('kick',at+offset,.48,vel,36)
        for offset in ([2.025] if bridge else [1.025,3.025]):
            hit('snare',at+offset,.46,.71 if not bridge else .49,38)
        for i,offset in enumerate([0,.57,1,1.57,2,2.57,3,3.57] if not bridge else [.57,2.57]):
            hit('hat',at+offset+float(rng.uniform(-.006,.006)),.17,.48 if i%2==0 else .30,42)
        if hooked:
            for offset in [.56,1.56,2.56,3.56]:
                hit('shaker',at+offset,.21,.39,70)
        if bar%4==3 and not bridge:
            hit('rim',at+2.77,.2,.35,37)
            for offset,vel in [(3.26,.21),(3.76,.29)]:
                hit('snare',at+offset,.18,vel,38)
        if hooked and bar%4==1:
            hit('open_hat',at+2.54,.33,.34,46)
    for t in tracks.values():
        t['events'].sort(key=lambda e:(e['beat'],e['note']))
    score=dict(title='尘封来信 · Dust Letters — Phrase Flip',bpm=BPM,bars=36,tail_seconds=2,fade_seconds=3,
               sample_root=str(prepared),tracks=list(tracks.values()),
               sections=[dict(name=n,start_bar=s,bars=b) for n,s,b in sections],
               license=old_score['license'],composition_mode='curated musical phrase flip',
               sample_flip=dict(primary_source='rhodes-dust',source_bpm=115,transpose_semitones=2,
                                source_records=records,slices=slice_records,placements=placements))
    write_json(REPO/'examples/dust-letters.json',score)
    manifest=render_score(score,out)
    manifest.update(composition_mode='curated musical phrase flip',sample_flip=score['sample_flip'])
    write_json(out/'run_manifest.json',manifest)
    shutil.copy2(refs['rhodes-dust']['file'],out/'original-phrase.wav')
    sr=44100;solo=sum(sf.read(out/'stems'/(t+'.wav'),always_2d=True)[0] for t in chop_lookup)
    if not np.isfinite(solo).all() or np.max(abs(solo))>=1:
        raise ValueError('Sample solo clips or contains invalid audio')
    sf.write(out/'sample-flip-solo.wav',solo,sr,subtype='PCM_24')
    # A single MIDI lane triggers the chopped phrases; pads retain the baked pitch/time treatment.
    mid=MidiFile(type=0,ticks_per_beat=960);track=MidiTrack();mid.tracks.append(track)
    track.extend([MetaMessage('set_tempo',tempo=bpm2tempo(BPM)),MetaMessage('time_signature',numerator=4,denominator=4)])
    note_map={s['id']:s['midi_note'] for s in slice_records};queue=[]
    for p in placements:
        start=round(p['beat']*960);end=round((p['beat']+p['duration_beats'])*960)
        queue.extend([(start,1,note_map[p['slice_id']],round(p['velocity']*127)),(end,0,note_map[p['slice_id']],0)])
    last=0
    for tick,on,note,velocity in sorted(queue):
        track.append(Message('note_on' if on else 'note_off',note=note,velocity=velocity,time=tick-last));last=tick
    track.append(MetaMessage('end_of_track',time=144*960-last));mid.save(out/'phrase-chops.mid')
    write_json(out/'slice-map.json',slice_records)
    # Source -> transformation -> arrangement is audible without requiring Live to open.
    original=sf.read(out/'original-phrase.wav',always_2d=True)[0]
    flip=solo[round(16*60/BPM*sr):round(32*60/BPM*sr)]
    mix=sf.read(out/'full_mix.wav',always_2d=True)[0][round(48*60/BPM*sr):round(64*60/BPM*sr)]
    def match(x):
        rms=np.sqrt(np.mean(x*x));return x*min(.85/max(np.max(abs(x)),1e-9),.12/max(rms,1e-9))
    comparison=np.concatenate([match(original),np.zeros((sr,2)),match(flip),np.zeros((sr,2)),match(mix)])
    sf.write(out/'before-after.wav',comparison,sr,subtype='PCM_24')
    write_json(out/'before-after-map.json',dict(parts=['original factory phrase','reordered sample solo','full beat'],
               durations_seconds=[len(original)/sr,1,len(flip)/sr,1,len(mix)/sr],
               matching='RMS matched with peak guard; not LUFS matched'))
    print(json.dumps(dict(out=str(out),duration=manifest['duration_seconds'],source=str(refs['rhodes-dust']['file']),
                         slices=len(slice_records),placements=len(placements)),ensure_ascii=False))


def export_delivery():
    """Collect the listening arrangement and a separately editable phrase sampler."""
    from pipeline.ableton_export import export_song
    from examples.windowlight_drum_practice import build_rack, xml, value

    song=REPO/'beats/dust-letters-v1'
    out=REPO/'exports/dust-letters-v1'
    rack_folder=out/'ChopRack'
    if rack_folder.exists():
        raise FileExistsError(rack_folder)
    export_song(song,out/'AbletonProject',set_name='DustLetters')
    (rack_folder/'Samples').mkdir(parents=True)
    slices=json.loads((song/'slice-map.json').read_text())
    pads=[]
    for s in slices:
        relative=f'Samples/{s["midi_note"]:02d}-{s["id"]}.wav'
        shutil.copy2(s['file'],rack_folder/relative)
        pads.append(dict(midi=s['midi_note'],name=s['id'].upper(),choke=0,file=relative))
    build_rack(pads,rack_folder,rack_folder)
    temporary=rack_folder/'Windowlight 16 Pads.adg'
    root=xml(temporary)
    value(root,'.//DrumGroupDevice/UserName','Dust Letters Phrase Chops')
    value(root,'.//DrumGroupDevice/PadScrollPosition',12)
    (rack_folder/'Dust Letters Phrase Chops.adg').write_bytes(
        gzip.compress(ET.tostring(root,encoding='utf-8',xml_declaration=True),mtime=0))
    temporary.unlink()
    for name in ['phrase-chops.mid','slice-map.json','original-phrase.wav']:
        shutil.copy2(song/name,rack_folder/name)
    (rack_folder/'使用说明.md').write_text(
        '# Dust Letters 乐句切片\n\n'
        '把 Dust Letters Phrase Chops.adg 拖入一条 MIDI 轨，再把 phrase-chops.mid 拖入同一轨，设为 92 BPM。'
        'MIDI 音高 48–61 对应 14 个实际切片，变调和伸缩已写入 WAV；可改顺序、长度、力度及采样器参数。'
        '保留整个 ChopRack 文件夹以维持媒体引用。\n\n'
        '这是供继续翻采的乐器预设。试听成品的滤波、混响、Bass、鼓与总线增益不在本预设里；'
        '完整编排见旁边 AbletonProject/DustLetters.als（21 条音频轨）。'
        '工程结构和文件引用可校验；Live 实际打开、发声和回渲染仍待验证。\n')


if __name__=='__main__':
    build()
    export_delivery()
