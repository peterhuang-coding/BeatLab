"""Afterglow v3: new recorded phrases take the lead; preserve v1/v2.

Run .venv/bin/python -m examples.afterglow_vocal_relay. Requires the archived
v2 render, Citizen DJ excerpts and Windowlight vocal samples on the music drive.
No shared DSP/exporter changes. Musical approval remains a listening decision.
"""
from copy import deepcopy
import gzip
import json
import math
from pathlib import Path
import shutil
import subprocess
import xml.etree.ElementTree as ET

from mido import MidiFile, MetaMessage
import numpy as np
import soundfile as sf

from examples.afterglow_daily import sha, save
from examples.afterglow_collage import write_midi, BPM, BARS, SR, BEAT_S
from examples.windowlight_drum_practice import build_rack, xml, value
from pipeline.sample_flip import make_slice
from pipeline.song import render_score
from pipeline.ableton_export import validate_song, export_song

ROOT = Path(__file__).resolve().parents[1]
RUN, PARENT = 'afterglow-2026-09-16-v3', 'afterglow-2026-09-16-v2'
PARENT_SHA = '942c607d79457a0bf7f824c3f74388c8ff96d3c639eb034ec271ccace5166321'
RECORDS = {
    'esther': ('aa4439f4333d77a8', 'Esther Walker',
               'cee6e0910320a205402a9895e1ea89907f11d8c19cca75df8f5ef0e1ab9273c5',
               'b42c7a00dcc0c5b7adf7cf2b945332705ca74beb6c59e3b00aafb7044b89e8a2'),
    'goodman': ('dfd615072f6fec57', 'Marion Harris',
                'f9ea2c000f8d472672ec07b0c81d87de9a4b54d68fe13674541ce36c89021263',
                '6aabe4d125690a239e758baa0b1b961b0a556558fbea3ab3a31722f1baaa6b7b'),
}
# Twelve different, non-overlapping windows across two new recordings. Chroma
# suggests these harmonic colors; it cannot verify lyrics or aesthetic quality.
REGIONS = [
    ('esther_gm', 'esther', .06966, 1.85760, 4, 5.03, False),
    ('esther_bb_question', 'esther', 1.85760, 3.69197, 4, -1.97, False),
    ('esther_cm', 'esther', 3.69197, 5.87465, 4, -1.97, False),
    ('esther_eb', 'esther', 5.87465, 7.82512, 4, -1.97, False),
    ('esther_bb', 'esther', 7.82512, 9.63628, 4, .03, False),
    ('esther_low_eb', 'esther', 9.63628, 12.45, 6, -6.97, False),
    ('goodman_cm', 'goodman', 12.00472, 13.97841, 4, -2.03, False),
    ('goodman_gm', 'goodman', 13.97841, 15.92889, 4, 4.97, False),
    ('goodman_eb', 'goodman', 17.92580, 19.96916, 4, -2.03, False),
    ('goodman_bb', 'goodman', 22.15184, 24.17197, 4, 4.97, False),
    ('goodman_eb_end', 'goodman', 26.26177, 28.32834, 4, -2.03, False),
    ('goodman_bb_call', 'goodman', 7.80190, 9.93814, 4, 2.97, False),
    # These are transformations, explicitly NOT counted as new source phrases.
    ('esther_reverse_cm', 'esther', 3.69197, 5.87465, 2, -1.97, True),
    ('goodman_tail_eb', 'goodman', 27.79429, 28.32834, 1, -2.03, False),
]


def build():
    parent, song = ROOT/'beats'/PARENT, ROOT/'beats'/RUN
    prepared, delivery = ROOT/'library/instruments'/RUN, ROOT/'exports'/RUN
    for path in (song, prepared, delivery):
        if path.exists():
            raise FileExistsError(f'Preserve this version: {path}')
    assert sha(parent/'full_mix.wav') == PARENT_SHA
    check = validate_song(parent)
    score = deepcopy(json.loads((parent/'score.json').read_text()))
    previous = deepcopy(score['sample_flip'])
    old_ids = {s['id'] for s in previous['slices'] if s['group']=='vocal_record'}
    keep_old = {'a', 'b', 'resolve'}
    tracks = {t['id']: t for t in score['tracks'] if t['id'] not in old_ids-keep_old}
    prepared.mkdir(parents=True)
    for t in tracks.values():
        src = Path(score['sample_root'])/t['sample']
        assert sha(src) == next(x['source_sha256'] for x in check['tracks'] if x['id']==t['id'])
        shutil.copy2(src, prepared/src.name)
        if t['id'] in old_ids:
            t['events'] = []
    slices = [s for s in previous['slices'] if s['id'] in tracks]
    for s in slices:
        s['file'] = str(prepared/Path(s['file']).name)
        if s['group']=='vocal_record':
            s['group'] = 'original_hook'
    placements = [p for p in previous['placements'] if p['group']!='vocal_record']
    catalog = json.loads((ROOT/'library/sources/citizen_dj/catalog.json').read_text())
    refs, cleaned, processing = {}, {}, []
    for role,(ident,performer,source_sha,download_sha) in RECORDS.items():
        ref = deepcopy(next(r for r in catalog if r['id']==ident))
        assert sha(ref['library_path'])==source_sha and sha(ref['original_download'])==download_sha
        ref.update(performer=performer, source_type='female solo with orchestra; not separated vocals',
                   local_source_sha256=source_sha, download_sha256=download_sha,
                   metadata_verified_date='2026-09-16',
                   offset_precision='seconds within archived excerpt, not full-record offsets')
        refs[role] = ref; cleaned[role] = prepared/(role+'-cleaned.wav')
        filters = 'highpass=f=105,afftdn=nr=5:nf=-34:tn=1,acompressor=threshold=0.12:ratio=2:attack=15:release=160'
        subprocess.run(['ffmpeg','-v','error','-nostdin','-i',ref['library_path'],
                        '-af',filters,'-c:a','pcm_f32le',str(cleaned[role])],check=True)
        assert sf.info(cleaned[role]).frames==sf.info(ref['library_path']).frames
        processing.append(dict(source_id=ident,filters=filters,file=str(cleaned[role]),sha256=sha(cleaned[role])))

    def add_slice(tid, role, src, original, expected, a, b, beats, pitch, reverse, source_id, gain=-10.2):
        sound, rate = make_slice(src,start_s=a,end_s=b,duration_s=beats*BEAT_S,
                                 semitones=pitch,reverse=reverse)
        dest=prepared/(tid+'.wav'); sf.write(dest,sound,rate,subtype='FLOAT')
        # Compensate song.py's peak normalization so recorded phrases have comparable
        # active RMS, with a conservative gain cap. Arrangement velocities still vary.
        ratio=float(np.sqrt(np.mean(sound**2)))/max(float(np.max(abs(sound))),1e-8)
        gain += float(np.clip(20*math.log10(.23/max(ratio,1e-8)), -3, 3))
        tracks[tid]=dict(id=tid,name=tid.replace('_',' ').title(),sample=dest.name,root_midi=60,
                         gain_db=gain,highpass_hz=230,lowpass_hz=4800,
                         pan=-.09 if role=='esther' else .09 if role=='goodman' else .03,
                         room=.07 if role!='wordless' else .20,events=[])
        slices.append(dict(id=tid,source_id=source_id,source_file=str(original),source_sha256=expected,
                           start_seconds=a,end_seconds=b,target_beats=beats,semitones=pitch,reverse=reverse,
                           group=role,file=str(dest),sha256=sha(dest),processed_source=str(src),
                           processed_sha256=sha(src),new_independent_window=tid in {r[0] for r in REGIONS[:12]}))
        print(f'[phrase] {tid}: {a:.3f}–{b:.3f}s, {pitch:+.2f} st',flush=True)

    for tid,role,a,b,beats,pitch,reverse in REGIONS:
        ref=refs[role]
        add_slice(tid,role,cleaned[role],ref['library_path'],ref['local_source_sha256'],
                  a,b,beats,pitch,reverse,ref['id'])
    kit=json.loads((ROOT/'library/instruments/windowlight-v2/source_catalog.json').read_text())
    wordless_refs=[]
    for tid,original_id,note,root,beats in [('oh_g','vocal_oh',55,56,1.1),('oh_d','vocal_oh',62,56,.8),
                                         ('ah_bb','vocal_ah',58,59,1.1),('ah_c','vocal_ah',60,59,.8)]:
        ref=deepcopy(next(r for r in kit if r['id']==original_id))
        path=ROOT/'library/instruments/windowlight-v2'/(original_id+'.aif')
        assert sha(path)==ref['sha256']
        info=sf.info(path)
        add_slice(tid,'wordless',path,path,ref['sha256'],0,info.duration,beats,note-root,False,original_id,-14.0)
        if original_id not in {r['id'] for r in wordless_refs}:
            ref.update(collected_file=str(path),source_type='Core Library wordless vocal one-shot; not an additional historical song')
            wordless_refs.append(ref)
    for i,s in enumerate(slices):
        s['midi_note']=48+i
    lookup={s['id']:s for s in slices}
    for p in placements:
        p['note']=lookup[p['slice_id']]['midi_note']

    def cut(tid,at,duration=3.85,velocity=.73):
        assert duration<=lookup[tid]['target_beats'] and at+duration<=BARS*4
        tracks[tid]['events'].append(dict(beat=at,note=60,duration_beats=duration,velocity=velocity))
        placements.append(dict(slice_id=tid,beat=at,duration_beats=duration,velocity=velocity,
                               note=lookup[tid]['midi_note'],group=lookup[tid]['group']))

    # New voices are exposed immediately, before a familiar four-beat callback.
    cut('esther_eb',0,3.8,.59);cut('a',4,2,.55);cut('b',6,1.8,.58)
    cut('goodman_gm',8,3.8,.63);cut('esther_bb_question',12,3.75,.62)
    # The two verses have different lead orders and source windows, not a duplicated loop.
    leads_a=['goodman_eb','esther_gm','goodman_cm','esther_bb',
             'goodman_eb_end','goodman_gm','esther_cm','goodman_bb_call']
    leads_b=['esther_eb','goodman_gm','esther_cm','goodman_bb',
             'goodman_eb_end','esther_gm','goodman_cm','esther_bb_question']
    answers_a=['esther_eb','goodman_gm','esther_cm','goodman_bb',
               'esther_low_eb',None,'esther_reverse_cm','esther_bb_question']
    answers_b=['goodman_eb',None,'goodman_cm','esther_bb',
               'esther_eb','goodman_gm','esther_reverse_cm','goodman_bb_call']
    for start,leads,answers in [(16,leads_a,answers_a),(128,leads_b,answers_b)]:
        for i,(lead,answer) in enumerate(zip(leads,answers)):
            at=start+8*i
            cut(lead,at+.04,3.8,.74 if i<4 else .79)
            if answer:
                duration=1.85 if 'reverse' in answer else 2.7 if i%2==0 else 2.2
                cut(answer,at+4.25,duration,.62 if start==16 else .68)
            else:
                cut('oh_g',at+4.2,1.05,.66);cut('oh_d',at+5.55,.75,.64)
                cut('ah_bb',at+6.65,1.0,.62)
    # Keep the original two chops at just three landmarks in the whole arrangement.
    for start in (80,192):
        cut('a',start,2,.76);cut('b',start+2,1.8,.75)
        cut('esther_eb' if start==80 else 'goodman_eb_end',start+4.05,3.7,.80)
        for i,lead,answer in [(1,'esther_gm','goodman_gm'),(2,'goodman_cm','esther_cm'),
                               (3,'esther_bb','goodman_bb')]:
            at=start+8*i
            if start==192:
                lead,answer=answer,lead
            cut(lead,at,3.75,.80);cut(answer,at+4.05,2.75,.75)
            cut(['ah_bb','ah_c','oh_d'][i-1],at+7.0,.75,.51)
    # Wordless melodic bridge leaves a distinct timbre between the two record-led verses.
    for at,ids in [(112,['oh_g','ah_bb','oh_d']),(116,['ah_c','oh_g','ah_c']),
                   (120,['oh_g','oh_d','ah_bb']),(124,['ah_bb','oh_d','ah_c'])]:
        for off,tid in zip((.2,1.65,2.8),ids):
            cut(tid,at+off,.75,.69 if off==.2 else .57)
    cut('esther_reverse_cm',118.9,1.0,.36)
    for at,tid in [(224,'goodman_eb_end'),(228,'esther_gm'),(232,'goodman_cm'),(236,'esther_bb')]:
        cut(tid,at,3.75,.81)
    cut('esther_low_eb',240,5.75,.58)
    cut('goodman_tail_eb',246.0,.7,.49);cut('goodman_tail_eb',246.85,.5,.39)
    cut('resolve',248,3.7,.52);cut('goodman_eb',252,3.5,.49)
    for t in tracks.values():
        assert t['events'],t['id'];t['events'].sort(key=lambda e:(e['beat'],e['note']))
    placements.sort(key=lambda p:(p['beat'],p['note']))
    revision=dict(parent_run=PARENT,parent_mix_sha256=PARENT_SHA,
                  feedback='可以，人声或者采样可以再多来点，感觉这个里面大妈一直是这两句',
                  changes=['12 new non-overlapping source windows across two additional vocal recordings',
                           'replace original vocal placements with alternating leads and responses',
                           'wordless bridge and hook punctuation; original motif limited to landmarks'])
    provenance=deepcopy(score['source_provenance'])
    source_records=previous['source_records']+list(refs.values())
    provenance.update(musical_sources=source_records,wordless_vocals=wordless_refs,
                      v3_preprocessing=processing,
                      source_selection_limit='Harmonic analysis and catalog vocalist credits; no automated lyric/VAD or human listening validation')
    score.update(title='余温 · Afterglow — Vocal Relay',run_id=RUN,sample_root=str(prepared),
                 tracks=list(tracks.values()),revision=revision,source_provenance=provenance,
                 composition_mode='three vocal-recording relay with instrumental collage and wordless responses',
                 sample_flip=dict(slices=slices,placements=placements,source_records=source_records))
    m=render_score(score,song)
    m.update(run_id=RUN,mix_sha256=sha(song/'full_mix.wav'),revision=revision,
             source_provenance=provenance,sample_flip=score['sample_flip'],
             composition_mode=score['composition_mode'],musical_feedback='awaiting user')
    for t in m['tracks']:
        path=song/t['midi'];mid=MidiFile(path)
        for lane in mid.tracks:
            elapsed=sum(e.time for e in lane if e.type!='end_of_track')
            lane[:]=[e for e in lane if e.type!='end_of_track']
            lane.append(MetaMessage('end_of_track',time=BARS*4*mid.ticks_per_beat-elapsed))
        mid.save(path);t['midi_sha256']=sha(path)
    save(song/'run_manifest.json',m);save(song/'slice-map.json',slices)
    save(song/'source-provenance.json',provenance);write_midi(placements,song/'phrase-chops.mid')
    finish_delivery(song,delivery)


def finish_delivery(song,delivery):
    if delivery.exists():
        raise FileExistsError(delivery)
    check=validate_song(song)
    score=json.loads((song/'score.json').read_text());m=json.loads((song/'run_manifest.json').read_text())
    slices=score['sample_flip']['slices'];lookup={s['id']:s for s in slices}
    n=sf.info(song/'full_mix.wav').frames
    groups={k:np.zeros((n,2),dtype='float32') for k in
            ('original_hook','esther','goodman','wordless','keys','guitar','rag','drums','bass')}
    for t in m['tracks']:
        role=lookup[t['id']]['group'] if t['id'] in lookup else 'bass' if t['id']=='bass' else 'drums'
        groups[role]+=sf.read(song/t['file'],dtype='float32',always_2d=True)[0]
    (song/'groups').mkdir(exist_ok=True)
    for role,sound in groups.items():
        assert np.isfinite(sound).all() and 0<np.max(abs(sound))<1
        sf.write(song/'groups'/(role+'.wav'),sound,SR,subtype='PCM_24')
    vocals=sum(groups[k] for k in ('original_hook','esther','goodman','wordless'))
    # A 31-second first-verse preview, and the same passage with record/wordless groups soloed.
    a,b=round(16*BEAT_S*SR),round(64*BEAT_S*SR)
    mix=sf.read(song/'full_mix.wav',always_2d=True)[0]
    sf.write(song/'vocal-relay-preview.wav',mix[a:b],SR,subtype='PCM_24')
    sf.write(song/'vocal-relay-solo.wav',vocals[a:b],SR,subtype='PCM_24')
    pieces=[];parts=[];cursor=0
    for tid in ('esther_gm','esther_cm','goodman_eb','goodman_bb'):
        item=lookup[tid]
        sound,rate=sf.read(item['source_file'],start=round(item['start_seconds']*SR),
                           stop=round(item['end_seconds']*SR),always_2d=True)
        assert rate==SR
        if sound.shape[1]==1:
            sound=np.repeat(sound,2,axis=1)
        gain=min(.8/np.max(abs(sound)),.12/np.sqrt(np.mean(sound**2)))
        pieces.extend([sound*gain,np.zeros((SR//2,2))])
        parts.append(dict(id=tid,start=cursor,duration=len(sound)/SR));cursor+=len(sound)/SR+.5
    pieces.append(mix[a:a+round(16*BEAT_S*SR)])
    sf.write(song/'new-sources-to-relay.wav',np.concatenate(pieces),SR,subtype='PCM_24')
    save(song/'new-sources-to-relay.json',dict(sources=parts,beat_starts_seconds=cursor,
         matching='source RMS .12 with .8 peak guard; beat unchanged, not LUFS matched'))
    export_song(song,delivery/'AbletonProject',set_name='AfterglowVocalRelay')
    rack=delivery/'ChopRack';(rack/'Samples').mkdir(parents=True);pads=[]
    for s in slices:
        relative=f'Samples/{s["midi_note"]:02d}-{s["id"]}.wav';shutil.copy2(s['file'],rack/relative)
        # All historical leads share one choke group; wordless/instruments are separate.
        choke=1 if s['group'] in ('original_hook','esther','goodman') else {'wordless':2,'keys':3,'guitar':4,'rag':5}[s['group']]
        pads.append(dict(midi=s['midi_note'],name=s['id'].upper(),choke=choke,file=relative))
    build_rack(pads,rack,rack);temp=rack/'Windowlight 16 Pads.adg';root=xml(temp)
    name=f'Afterglow {len(slices)} Vocal Relay Pads';value(root,'.//DrumGroupDevice/UserName',name)
    value(root,'.//DrumGroupDevice/PadScrollPosition',12)
    (rack/(name+'.adg')).write_bytes(gzip.compress(ET.tostring(root,encoding='utf-8',xml_declaration=True),mtime=0));temp.unlink()
    for name in ('slice-map.json','phrase-chops.mid','source-provenance.json'):
        shutil.copy2(song/name,rack/name)
    (rack/'使用说明.md').write_text(f'94 BPM，64 小节；MIDI 48–{47+len(slices)} 对应 {len(slices)} 鼓垫。ADG 与 phrase-chops.mid 拖入同一 MIDI 轨。\n\n历史录音同组互斥，哼唱/键盘/吉他/器乐短句独立组。音频工程包含最终混音处理，鼓垫用于继续创作，未承诺回放完全相同；Live GUI 尚待验。素材来自带伴奏录音，不是分离的人声。私人创作工程，不是商品采样包。\n')
    save(song/'verification.json',check)
    print(json.dumps(dict(run=RUN,tracks=len(m['tracks']),slices=len(slices),
                         triggers=len(score['sample_flip']['placements']),mix_sha256=m['mix_sha256']),ensure_ascii=False),flush=True)


if __name__=='__main__':
    build()
