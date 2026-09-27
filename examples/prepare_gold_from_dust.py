"""Curated source windows for Gold From Dust; no generated music or speech.

Run locally with --media-root. Audio stays outside Git; official source catalogs
are required and their SHA values are checked before processing anything.
"""
from __future__ import annotations
import argparse
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess
import numpy as np
import soundfile as sf
from pipeline.sample_flip import make_slice

RUN = 'gold-from-dust-20260927-v1'
BPM = 92
# The 2026-09-13 catalog's sha256 identifies the downloaded 48kHz WAV,
# while library_path points to its 44.1kHz ingest. Keep both identities.
INGESTED_SHA = {
 '9c6c32d17de4128c': 'c98a33cc7696e93f534cd56d1e4d9d4c9ad04f6d3129cd12343db1be15e45714',
 'aa4439f4333d77a8': 'cee6e0910320a205402a9895e1ea89907f11d8c19cca75df8f5ef0e1ab9273c5',
}
# Windows relative to the collected WAV, NOT sample-accurate global recording times.
# Bass is a musical interpretation; automatic chroma only informed the choice.
WINDOWS = [
 ('lead_a','9c6c32d17de4128c',4.202812,5.224490,2,3.0,False,32,'lead'),
 ('lead_b','9c6c32d17de4128c',5.224490,6.176508,2,3.0,False,32,'lead'),
 ('lead_c','9c6c32d17de4128c',6.176508,7.151746,2,3.0,False,32,'lead'),
 ('lead_d','9c6c32d17de4128c',7.151746,8.126984,2,3.0,False,32,'lead'),
 ('answer_a','aa4439f4333d77a8',.069660,1.857600,2,-2.0,False,32,'answer'),
 ('answer_b','aa4439f4333d77a8',1.857600,3.691970,2,-2.0,False,39,'answer'),
 ('answer_c','aa4439f4333d77a8',3.691970,5.874650,2,-2.0,False,39,'answer'),
 ('answer_d','aa4439f4333d77a8',5.874650,7.825120,2,-2.0,False,39,'answer'),
 ('long_memory','9c6c32d17de4128c',16.346848,18.436644,4,3.0,False,36,'turnaround'),
 ('reverse_brass','9a7e495a5224d187',8.290000,10.681000,4,-2.0,True,32,'turnaround'),
]


def sha(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f,'sha256').hexdigest()


def prepare(root: Path) -> Path:
    root=root.resolve()
    out=root/'library/instruments'/RUN
    if out.exists():
        raise FileExistsError(f'Keep existing prepared material: {out}')
    first=json.loads((root/'library/sources/citizen_dj/catalog.json').read_text())
    # Initial catalog is a list; later crate catalog wraps items.
    if isinstance(first,dict):
        first=first.get('items',first.get('sources',[]))
    catalog={r['id']:r for r in first}
    later=json.loads((root/'library/crates/old-records-20260919/catalog.json').read_text())
    catalog.update({r['asset_id']:{'id':r['asset_id'],'sha256':r['library_sha256'],**r['record']} for r in later['items'] if r['state']=='ok'})
    sources={}
    for sid in dict.fromkeys(w[1] for w in WINDOWS):
        source=root/'library/loops'/sid/'source.wav'
        meta=json.loads(source.with_name('meta.json').read_text())
        if meta['rights']['state']!='allowed' or meta['rights']['basis']!='loc_citizen_dj_collection_statement':
            raise ValueError(f'Unreviewed source: {sid}')
        if sid in INGESTED_SHA and sha(catalog[sid]['original_download'])!=catalog[sid]['sha256']:
            raise ValueError(f'Download changed: {sid}')
        expected=INGESTED_SHA.get(sid,catalog[sid]['sha256'])
        if sha(source)!=expected:
            raise ValueError(f'Source changed: {sid}')
        sources[sid]={'id':sid,'source_file':str(source),'sha256':expected,
                     'source_sha256':expected,**meta['provenance'],'rights':meta['rights'],
                     'current_purpose':'local private creative draft from collection permission',
                     'vocals':'recording with accompaniment; no source separation claimed',
                     'lyrics':'not transcribed; distinct windows do not certify different lyrics'}
    old=json.loads((root/'beats/windowlight-v2/score.json').read_text())
    kit=json.loads((root/'library/instruments/windowlight-v2/source_catalog.json').read_text())
    instruments=[]; kit_records=[]
    for tid in ['kick','snare','hat','bass','shaker','rim','open_hat']:
        track=deepcopy(next(t for t in old['tracks'] if t['id']==tid))
        source=Path(old['sample_root'])/track['sample']
        record=next(r for r in kit if r['id']==tid)
        if sha(source)!=record['sha256']:
            raise ValueError(f'Instrument changed: {tid}')
        instruments.append((track,source));kit_records.append(record)
    out.mkdir(parents=True)
    cleaned={}
    filters='highpass=f=85,afftdn=nr=4:nf=-34:tn=1'
    for sid,record in sources.items():
        target=out/(sid+'-clean.wav')
        subprocess.run(['ffmpeg','-v','error','-nostdin','-i',record['source_file'],
                        '-af',filters,'-c:a','pcm_f32le',str(target)],check=True,timeout=60)
        before=sf.info(record['source_file']);after=sf.info(target)
        if (before.frames,before.samplerate)!=(after.frames,after.samplerate):
            raise ValueError('Cleaning changed timing')
        cleaned[sid]=target
        record['preprocessing']={'filters':filters,'file':str(target),'sha256':sha(target)}
    slices=[]
    for index,(sid,source_id,a,b,beats,pitch,reverse,bass,role) in enumerate(WINDOWS):
        data,sr=make_slice(cleaned[source_id],start_s=a,end_s=b,
                          duration_s=beats*60/BPM,semitones=pitch,reverse=reverse)
        # A single equalization step per slice; no invented/generated replacement.
        peak=float(np.max(np.abs(data)));rms=float(np.sqrt(np.mean(data.astype('float64')**2)))
        if not np.isfinite(data).all() or peak<1e-5:
            raise ValueError(f'Invalid actual cut: {sid}')
        target=out/(sid+'.wav');sf.write(target,data,sr,subtype='FLOAT')
        # Renderer normalizes peaks. Compensate so vowels have consistent RMS,
        # bounded +/-3dB to retain some performance dynamics.
        gain=-14.0+float(np.clip(20*math.log10(.22/(rms/peak)),-3,3))
        slices.append(dict(id=sid,source_id=source_id,source_file=sources[source_id]['source_file'],
            source_sha256=sources[source_id]['sha256'],start_seconds=a,end_seconds=b,
            target_beats=beats,semitones=pitch,reverse=reverse,role=role,bass_midi=bass,
            root_midi=60,midi_note=36+index,file=str(target),sha256=sha(target),gain_db=round(gain,4),
            processed_source=str(cleaned[source_id]),processed_source_sha256=sha(cleaned[source_id]),
            stretch_ratio=(beats*60/BPM)/(b-a),peak_before_sampler=peak,rms_before_sampler=rms,
            content_identity='untranscribed source interval; variants preserve source identity'))
        print(sid,round(len(data)/sr,3),round(gain,2),flush=True)
    tracks=[]
    gains={'kick':-11,'snare':-17,'hat':-29,'bass':-17,'shaker':-32,'rim':-25,'open_hat':-31}
    for track,source in instruments:
        shutil.copy2(source,out/source.name)
        track['events']=[];track['sample']=source.name;track['gain_db']=gains[track['id']]
        if track['id']=='bass':track['lowpass_hz']=650
        tracks.append(track)
    prepared=dict(version=1,run_id=RUN,bpm=BPM,sample_root=str(out),slices=slices,
                  instruments=tracks,source_records=list(sources.values()),instrument_sources=kit_records,
                  melodic_material='actual recorded phrases; no TTS/oscillator composition',
                  working_harmony='Ab major / Cm contrast; heuristic chroma plus curated interpretation, audition pending')
    (out/'prepared.json').write_text(json.dumps(prepared,ensure_ascii=False,indent=2)+'\n')
    (out/'source_catalog.json').write_text(json.dumps(kit_records,ensure_ascii=False,indent=2)+'\n')
    return out/'prepared.json'


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--media-root',type=Path,required=True)
    print(prepare(parser.parse_args().media_root))
