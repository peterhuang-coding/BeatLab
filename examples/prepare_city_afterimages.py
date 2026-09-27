"""Local-only preparation of a bounded ten-draft real-record collection.

Source analysis is an explicit input, not evidence of complete lyric identity.
No source download, network access, publication, or old-song mutation occurs.
"""
from pathlib import Path
from copy import deepcopy
import argparse
import hashlib
import json
import math
import subprocess
import numpy as np
import soundfile as sf
import librosa
from pipeline.sample_flip import make_slice
from examples.gold_from_dust_v2 import KIT_SETTINGS, EXTRA_SETTINGS

COLLECTION = 'city-afterimages-20260927'
PAIRS = [
    ('9c6c32d17de4128c', 'dfd615072f6fec57', 3),
    ('b99037ea894abe28', '0e68a23c225b7946', 2),
    ('37baf98ef2b9d8a4', '775722a4dfc85b66', -1),
    ('a53f5e44f8e3a005', '9a7e495a5224d187', 1),
    ('2ab51a3bee3af4f1', '628ccd77bbcd10be', 2),
    ('dfd615072f6fec57', 'bded27c143a65d4c', -2),
    ('3849c9beedd6e62a', 'e5d77696593a43f4', 1),
    ('ca20d479c4f78d92', 'a4d39d404c04ebe7', 2),
    ('222e558101758b3a', '7c7579c8d6260e56', -1),
    ('d3bde0061ccb9a71', 'f5b03032a3898708', 1),
]


def sha(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def select_windows(row, bpm, variant):
    """Keep adjacent beat intervals and modest stretch; no lyric claims."""
    beats = np.asarray(row['beats'], dtype=float)
    if len(beats) < 10 or not np.isfinite(beats).all() or np.any(np.diff(beats) <= 0):
        raise ValueError('Insufficient ordered beat observations')
    desired = 120 / bpm
    period = float(np.median(np.diff(beats)))
    step = min((1, 2, 3, 4), key=lambda x: abs(math.log(desired / (period*x))))
    max_start = len(beats)-4*step-1
    if max_start < 0:
        raise ValueError('Too few source beats for four different cuts')
    start = min(max_start, 2 + variant % 4)
    cuts = [(float(beats[start+i*step]), float(beats[start+(i+1)*step])) for i in range(4)]
    turn_step = min(len(beats)-1, step*2)
    j = max(0, len(beats)-turn_step-2)
    turn = (float(beats[j]), float(beats[j+turn_step]))
    for a,b in cuts+[turn]:
        target = 240/bpm if (a,b)==turn else desired
        if not 0.55 <= target/(b-a) <= 1.65 or not 0 <= a < b <= row['duration']:
            raise ValueError('Extreme or invalid stretch rejected')
    return cuts, turn


def prepare(root, specs, analysis):
    root=Path(root).resolve()
    out=root/'library/instruments'/COLLECTION
    if out.exists():
        raise FileExistsError('Keep existing prepared collection: '+str(out))
    rows={r['id']:r for r in analysis}
    kit=json.loads((root/'library/instruments/gold-from-dust-20260927-v2/prepared.json').read_text())
    kit_root=Path(kit['sample_root'])
    kit_tracks=kit['instruments']+kit['extra_instruments']
    kit_hashes={t['id']:sha(kit_root/t['sample']) for t in kit_tracks}
    source_ids=sorted({sid for pair in PAIRS for sid in pair[:2]})
    for sid in source_ids:
        source=root/'library/loops'/sid/'source.wav'
        meta=json.loads(source.with_name('meta.json').read_text())
        if meta.get('rights',{}).get('basis')!='loc_citizen_dj_collection_statement' or meta['rights'].get('state')!='allowed':
            raise ValueError('Unknown source purpose: '+sid)
        if sha(source)!=rows[sid]['source_sha256']:
            raise ValueError('Source changed: '+sid)
        # Ingest normalizes the downloaded WAV; catalog MD5 is for orig_path.
        original=Path(meta['orig_path'])
        if hashlib.md5(original.read_bytes()).hexdigest()!=meta['md5']:
            raise ValueError('Original download changed: '+sid)
    out.mkdir(parents=True)
    cleaned={}
    filters='highpass=f=85,afftdn=nr=4:nf=-34:tn=1'
    for sid in source_ids:
        source=root/'library/loops'/sid/'source.wav'; target=out/(sid+'-clean.wav')
        subprocess.run(['ffmpeg','-v','error','-nostdin','-i',str(source),'-af',filters,'-c:a','pcm_f32le',str(target)],check=True,timeout=60)
        before,after=sf.info(source),sf.info(target)
        if (before.frames,before.samplerate)!=(after.frames,after.samplerate):
            raise ValueError('Preprocessing changed timing')
        cleaned[sid]=target
    outputs=[]
    for index,(spec,pair) in enumerate(zip(specs,PAIRS,strict=True)):
        lead,answer,lead_pitch=pair; target_dir=out/spec['number'];target_dir.mkdir()
        # Chromagram alignment is a provisional musical interpretation, not a key detector verdict.
        lc=np.roll(np.asarray(rows[lead]['chroma']),lead_pitch)
        answer_pitch=max(range(-5,6),key=lambda p: float(np.dot(lc,np.roll(np.asarray(rows[answer]['chroma']),p))))
        slices=[];records=[]
        for which,sid,pitch in [('lead',lead,lead_pitch),('answer',answer,answer_pitch)]:
            row=rows[sid]; source=root/'library/loops'/sid/'source.wav'
            records.append(dict(id=sid,source_file=str(source),source_sha256=sha(source),sha256=sha(source),
                **row['provenance'],rights=row['rights'],lyrics='untranscribed; distinct windows are not verified distinct lyrics',
                vocals='historical recording with accompaniment; not source-separated',
                rights_review={'date':'2026-09-27','basis':'official collection statement permits reuse including commercial purposes',
                              'release_status':'draft; final work and destination-specific clearance not adjudicated'},
                preprocessing={'file':str(cleaned[sid]),'sha256':sha(cleaned[sid]),'filters':filters}))
            windows,turn=select_windows(row,spec['bpm'],index)
            for k,(a,b) in enumerate(windows+[turn]):
                name=f'{which}_{"abcd"[k]}' if k<4 else ('turn_a' if which=='lead' else 'turn_b')
                target_beats=2 if k<4 else 4
                reverse=bool(k==4 and which=='answer' and spec['number'] in ('04','09'))
                data,sr=make_slice(cleaned[sid],start_s=a,end_s=b,duration_s=target_beats*60/spec['bpm'],semitones=pitch,reverse=reverse)
                peak=float(np.max(np.abs(data)));rms=float(np.sqrt(np.mean(data.astype('float64')**2)))
                if not np.isfinite(data).all() or peak<1e-5 or len(data)!=round(target_beats*60/spec['bpm']*sr):
                    raise ValueError('Invalid processed cut')
                path=target_dir/(name+'.wav');sf.write(path,data,sr,subtype='FLOAT')
                mono=librosa.resample(data.mean(axis=1),orig_sr=sr,target_sr=22050)
                chroma=librosa.feature.chroma_stft(y=mono,sr=22050,n_fft=4096).mean(axis=1)
                pc=int(np.argmax(chroma)); bass=36+pc if pc<6 else 24+pc
                gain=-15+float(np.clip(20*np.log10(.22/(rms/peak)),-3,3))
                slices.append(dict(id=name,file=str(path),sha256=sha(path),source_id=sid,source_file=str(source),source_sha256=sha(source),
                    start_seconds=a,end_seconds=b,target_beats=target_beats,semitones=pitch,reverse=reverse,
                    root_midi=60,bass_midi=bass,gain_db=round(gain,4),role=which if k<4 else 'turnaround',
                    midi_note=36+len(slices),stretch_ratio=(target_beats*60/spec['bpm'])/(b-a),
                    bass_basis='strongest processed-cut chroma; provisional, user audition pending',
                    content_identity=f'{sid}:{a:.6f}-{b:.6f}',boundary_basis='local detected beat boundaries, not verified complete lyrical phrase'))
        instruments=deepcopy(kit_tracks)
        for track in instruments:
            track['sample']=str(kit_root/track['sample']);track['events']=[]
            if track['id'] in KIT_SETTINGS:
                gain,lowpass,highpass,room=KIT_SETTINGS[track['id']]
                track.update(gain_db=gain,lowpass_hz=lowpass,highpass_hz=highpass,room=room)
            elif track['id'] in EXTRA_SETTINGS:
                track.update(EXTRA_SETTINGS[track['id']])
        prepared=dict(version=1,**spec,run_id=f'city-{spec["number"]}-{spec["slug"]}-20260927-v1',sample_root=str(target_dir),slices=slices,
                      instruments=instruments,source_records=records,instrument_sources=kit['instrument_sources'],kit_sha256=kit_hashes,
                      melodic_material='real recording cuts, no generated vocal substitute',keep=None)
        path=target_dir/'prepared.json';path.write_text(json.dumps(prepared,ensure_ascii=False,indent=2)+'\n');outputs.append(str(path))
        print(spec['number'],spec['title'],rows[lead]['title'],'/',rows[answer]['title'],'pitches',lead_pitch,answer_pitch,flush=True)
    (out/'prepared-index.json').write_text(json.dumps(outputs,indent=2)+'\n')
    return outputs


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--media-root',required=True,type=Path)
    p.add_argument('--specs',required=True,type=Path);p.add_argument('--analysis',required=True,type=Path);a=p.parse_args()
    prepare(a.media_root,json.loads(a.specs.read_text()),json.loads(a.analysis.read_text()))
