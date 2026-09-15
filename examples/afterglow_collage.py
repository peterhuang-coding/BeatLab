"""Afterglow v2: four musical sources, longer verses, and an alternate vocal-light mix.

Run with .venv/bin/python -m examples.afterglow_collage. Reuses the preserved v1
score and existing render/export code. All media remains on the local music drive.
"""
from copy import deepcopy
import gzip
import json
from pathlib import Path
import shutil
import subprocess
import xml.etree.ElementTree as ET

import numpy as np
import soundfile as sf
from mido import MidiFile, MidiTrack, Message, MetaMessage, bpm2tempo

from examples.afterglow_daily import sha, save
from examples.windowlight_drum_practice import build_rack, xml, value
from pipeline.sample_flip import make_slice
from pipeline.song import render_score
from pipeline.ableton_export import export_song, validate_song

ROOT = Path(__file__).resolve().parents[1]
RUN, PARENT = 'afterglow-2026-09-16-v2', 'afterglow-2026-09-16-v1'
PARENT_SHA = '9e030c07f632b2bcb50971adc83f2aa5d9634f9c3de71cae55068483b66ca3a2'
BPM, BARS, SR = 94, 64, 44100
BEAT_S = 60 / BPM
SECTIONS = [('Opening', 0, 4), ('Verse A', 4, 16), ('Hook A', 20, 8),
            ('Bridge', 28, 4), ('Verse B', 32, 16), ('Hook B', 48, 8),
            ('Last refrain', 56, 4), ('Closing', 60, 4)]
NEW_SLICES = [
    # G minor upper notes also color an Eb-bass chord as Eb major 7.
    ('keys_gm', 'rhodes-dust', 8*60/115, 12*60/115, 4, -.02, False, 'keys'),
    ('keys_cm', 'rhodes-dust', 8*60/115, 12*60/115, 4, -7.02, False, 'keys'),
    ('keys_bb', 'rhodes-dust', 8*60/115, 12*60/115, 4, -5.02, False, 'keys'),
    ('guitar_gm', 'guitar-muffled', 0, 2.5, 4, -6.11, False, 'guitar'),
    ('guitar_cm', 'guitar-muffled', 0, 2.5, 4, -1.11, False, 'guitar'),
    ('rag_eb', 'rag', 28.049705, 29.373243, 2, -.18, False, 'rag'),
    ('rag_bb', 'rag', 23.986213, 25.332971, 2, -.18, False, 'rag'),
    ('rag_reverse', 'rag', 28.049705, 29.373243, 2, -.18, True, 'rag'),
]


def write_midi(events, path):
    m = MidiFile(type=0, ticks_per_beat=960); lane = MidiTrack(); m.tracks.append(lane)
    lane.extend([MetaMessage('set_tempo', tempo=bpm2tempo(BPM)),
                 MetaMessage('time_signature', numerator=4, denominator=4)])
    queue = []
    for e in events:
        start, end = round(e['beat']*960), round((e['beat']+e['duration_beats'])*960)
        assert 0 <= start < end <= BARS*4*960
        queue.extend([(start, 1, e['note'], max(1, round(e['velocity']*127))),
                      (end, 0, e['note'], 0)])
    previous = 0
    for tick, on, note, velocity in sorted(queue):
        lane.append(Message('note_on' if on else 'note_off', note=note, velocity=velocity, time=tick-previous))
        previous = tick
    lane.append(MetaMessage('end_of_track', time=BARS*4*960-previous)); m.save(path)


def mapped_beats(beat):
    if 16 <= beat < 48:
        return [beat, beat+32]
    if 96 <= beat < 128:
        return [beat+32, beat+64]
    return [beat + (0 if beat < 48 else 32 if beat < 128 else 64)]


def verse_at(beat):
    return next((start for start in (16, 128) if start <= beat < start+64), None)


def build():
    parent, song = ROOT/'beats'/PARENT, ROOT/'beats'/RUN
    prepared, delivery = ROOT/'library/instruments'/RUN, ROOT/'exports'/RUN
    for path in (song, prepared, delivery):
        if path.exists():
            raise FileExistsError(f'Preserve the version: {path}')
    if sha(parent/'full_mix.wav') != PARENT_SHA:
        raise ValueError('Parent music changed')
    check = validate_song(parent)
    score = deepcopy(json.loads((parent/'score.json').read_text()))
    old_slices = deepcopy(score['sample_flip']['slices'])
    vocal_ids = {s['id'] for s in old_slices}
    tracks = {t['id']: t for t in score['tracks']}
    prepared.mkdir(parents=True)
    for t in tracks.values():
        src = Path(score['sample_root'])/t['sample']
        expected = next(x['source_sha256'] for x in check['tracks'] if x['id'] == t['id'])
        if sha(src) != expected:
            raise ValueError(f'Parent instrument changed: {src}')
        dest = prepared/src.name; shutil.copy2(src, dest); t['sample'] = dest.name
        events = []
        if t['id'] not in vocal_ids:
            for e in t['events']:
                events.extend([{**e, 'beat': b} for b in mapped_beats(e['beat'])])
        else:
            t['gain_db'] -= 1.0
        t['events'] = events
    for s in old_slices:
        s.update(file=str(prepared/Path(s['file']).name), group='vocal_record')
    refs = {r['id']: r for r in json.loads((ROOT/'library/loops/phrase-bank-v1/catalog.json').read_text())}
    refs = {k: refs[k] for k in ('rhodes-dust', 'guitar-muffled')}
    for r in refs.values():
        if sha(r['file']) != r['sha256']:
            raise ValueError('Factory musical phrase changed')
    rag = next(r for r in json.loads((ROOT/'library/sources/citizen_dj/catalog.json').read_text())
               if r['id'] == 'a4d39d404c04ebe7')
    assert sha(rag['original_download']) == '6575008c38782dba5bb1a6c588f214b53fc09c437a29c8fc6ca970d739186941'
    assert sha(rag['library_path']) == 'a12ffd4a0956741ad6b29447831d82ac8e32cc379267c3ee4f6dd9fc3e4b0232'
    rag.update(file=rag['library_path'], download_sha256=rag['sha256'],
               sha256=sha(rag['library_path']), performer='All Star Trio', recording_date='1920-10-05',
               source_type='historical instrumental trio recording; not an isolated instrument stem',
               metadata_source='https://www.loc.gov/item/jukebox-38191/')
    refs['rag'] = rag
    cleaned = prepared/'rag-cleaned.wav'
    filters = 'highpass=f=150,afftdn=nr=5:nf=-34:tn=1'
    subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-i', rag['file'], '-af', filters,
                    '-c:a', 'pcm_f32le', str(cleaned)], check=True)
    assert sf.info(cleaned).frames == sf.info(rag['file']).frames
    slices = old_slices[:]
    for index, (tid, source_id, a, b, beats, pitch, reverse, group) in enumerate(NEW_SLICES):
        ref = refs[source_id]; src = cleaned if source_id == 'rag' else Path(ref['file'])
        sound, rate = make_slice(src, start_s=a, end_s=b, duration_s=beats*BEAT_S,
                                 semitones=pitch, reverse=reverse)
        dest = prepared/(tid+'.wav'); sf.write(dest, sound, rate, subtype='FLOAT')
        # Levels belong to the arrangement; song.py normalizes the prepared voice once.
        tracks[tid] = dict(id=tid, name=tid.replace('_', ' ').title(), sample=dest.name,
                           root_midi=60, gain_db={'keys': -14, 'guitar': -15.8, 'rag': -14}[group],
                           highpass_hz={'keys': 210, 'guitar': 260, 'rag': 430}[group],
                           lowpass_hz={'keys': 4300, 'guitar': 4500, 'rag': 4200}[group],
                           pan={'keys': -.14, 'guitar': .20, 'rag': .06}[group],
                           room=.12 if group != 'rag' else .05, events=[])
        slices.append(dict(id=tid, midi_note=64+index, source_id=source_id,
                           source_file=ref['file'], source_sha256=ref['sha256'],
                           start_seconds=a, end_seconds=b, target_beats=beats,
                           semitones=pitch, reverse=reverse, group=group,
                           file=str(dest), sha256=sha(dest),
                           processed_source=str(src), processed_sha256=sha(src)))
        print(f'[new source] {tid}: {source_id}, {a:.3f}–{b:.3f}s', flush=True)
    lookup = {s['id']: s for s in slices}; placements = []

    def cut(tid, at, duration, velocity):
        tracks[tid]['events'].append(dict(beat=at, note=60, duration_beats=duration, velocity=velocity))
        placements.append(dict(slice_id=tid, beat=at, duration_beats=duration, velocity=velocity,
                               note=lookup[tid]['midi_note'], group=lookup[tid]['group']))

    for p in score['sample_flip']['placements']:
        for at in mapped_beats(p['beat']):
            start = verse_at(at)
            duration = p['duration_beats']
            if start is not None:
                rel = at-start; chord, offset = int(rel//8)%4, rel%8
                second = rel >= 32
                keep = (offset < (4 if second else 8) if chord == 0 else
                        offset < 4 if chord == 1 else
                        (offset < 2 or offset >= 6) and not second if chord == 2 else
                        offset >= (6 if second else 4))
                if not keep:
                    continue
                if chord == 0 and second or chord == 1:
                    duration = min(duration, 4-offset)
                elif chord == 2 and offset < 2:
                    duration = min(duration, 2-offset)
            cut(p['slice_id'], at, duration, p['velocity'])
    # Upper chord colors and a performed guitar response share the verse with the record.
    for start in (16, 128):
        for phrase in range(8):
            at = start+phrase*8; chord = phrase%4
            keys = ['keys_gm', 'keys_gm', 'keys_cm', 'keys_bb'][chord]
            cut(keys, at+.04, 3.6, .46 if phrase < 4 else .60)
            if chord == 0:
                if phrase >= 4:
                    cut('rag_eb' if start == 16 else 'rag_reverse', at+6, 1.8, .58)
                else:
                    cut(keys, at+4.12, 3.45, .28)
            elif chord == 1:
                cut('guitar_gm', at+4.04, 3.7, .72)
            elif chord == 2:
                cut('guitar_cm', at+2.3 if phrase < 4 else at+4.04, 3.45, .64)
                if phrase >= 4:
                    cut(keys, at+4.12, 3.5, .35)
            else:
                cut('rag_bb', at+.10, 1.9, .55)
                if phrase >= 4:
                    cut('rag_bb', at+3.0, .85, .42)
    # Hook layers are quiet harmony + brief instrumental answers, never another whole song.
    for start in (80, 192):
        for chord in range(4):
            at = start+chord*8
            keys = ['keys_gm', 'keys_gm', 'keys_cm', 'keys_bb'][chord]
            cut(keys, at+.03, 3.6, .35 if start == 80 else .43)
            if chord in (1, 2):
                cut('guitar_gm' if chord == 1 else 'guitar_cm', at+6.0, .46, .53)
            elif chord == 3:
                cut('rag_bb', at+6.02, .43, .50)
    cut('keys_gm', .05, 3.7, .38)
    cut('rag_bb', 14.8, .65, .30)
    cut('keys_gm', 112.05, 4, .65); cut('keys_cm', 116.10, 3.7, .46)
    # Keep bridge bass/record harmony compatible: the second low record phrase is omitted
    # here so the new Cm phrase owns that half of the bridge.
    for p in placements:
        if p['slice_id'] == 'low' and p['beat'] == 112:
            p['duration_beats'] = 4
            next(e for e in tracks['low']['events'] if e['beat'] == 112)['duration_beats'] = 4
    cut('guitar_gm', 120.04, 3.65, .53)
    cut('rag_bb', 124.04, 1.85, .58)
    cut('rag_reverse', 127.5, .5, .34)
    for chord in range(4):
        cut(['keys_gm', 'keys_gm', 'keys_cm', 'keys_bb'][chord], 224+chord*4+.03, 3.7, .40)
    cut('rag_eb', 242.04, 1.8, .45); cut('keys_gm', 248.04, 3.6, .36)
    for t in tracks.values():
        assert t['events'], t['id']
        t['events'].sort(key=lambda e: (e['beat'], e['note']))
    # Every source is represented by actual audible events, not just by a manifest entry.
    source_records = score['sample_flip']['source_records'] + list(refs.values())
    revision = dict(parent_run=PARENT, parent_mix_sha256=PARENT_SHA,
                    feedback='可以再复杂一点，多几个音乐一起采样放进去',
                    changes=['four musical source performances', '16-bar verses',
                             'instrumental responses replace selected vocal passages', 'vocal-light alternate mix'])
    score.update(title='余温 · Afterglow — Four-source Collage', run_id=RUN, bars=BARS,
                 sample_root=str(prepared), tracks=list(tracks.values()), revision=revision,
                 composition_mode='curated four-source phrase collage',
                 sections=[dict(name=n, start_bar=s, bars=b) for n,s,b in SECTIONS],
                 sample_flip=dict(slices=slices, placements=placements, source_records=source_records))
    provenance = deepcopy(score['source_provenance'])
    provenance.update(musical_sources=source_records, new_record_preprocessing=dict(file=str(cleaned),
                      filters=filters, sha256=sha(cleaned)), rights_review='see local sale-readiness document; not cleared for listing')
    score['source_provenance'] = provenance
    m = render_score(score, song)
    m.update(run_id=RUN, mix_sha256=sha(song/'full_mix.wav'), revision=revision,
             source_provenance=provenance, sample_flip=score['sample_flip'],
             composition_mode=score['composition_mode'], musical_feedback='awaiting user')
    for t in m['tracks']:
        path = song/t['midi']; mid = MidiFile(path)
        for lane in mid.tracks:
            elapsed = sum(e.time for e in lane if e.type != 'end_of_track')
            lane[:] = [e for e in lane if e.type != 'end_of_track']
            lane.append(MetaMessage('end_of_track', time=BARS*4*mid.ticks_per_beat-elapsed))
        mid.save(path); t['midi_sha256'] = sha(path)
    save(song/'run_manifest.json', m); save(song/'slice-map.json', slices)
    save(song/'source-provenance.json', provenance); write_midi(placements, song/'phrase-chops.mid')
    # Source groups make audition and a future artist's vocal mix practical.
    (song/'groups').mkdir(); n = round(m['duration_seconds']*SR)
    groups = {k: np.zeros((n,2), dtype='float32') for k in ('vocal_record', 'keys', 'guitar', 'rag', 'drums', 'bass')}
    for t in m['tracks']:
        group = lookup[t['id']]['group'] if t['id'] in lookup else 'bass' if t['id']=='bass' else 'drums'
        groups[group] += sf.read(song/t['file'], always_2d=True, dtype='float32')[0]
    for name, sound in groups.items():
        assert np.isfinite(sound).all() and 0 < np.max(abs(sound)) < 1
        sf.write(song/'groups'/(name+'.wav'), sound, SR, subtype='PCM_24')
    envelope = np.ones(n, dtype='float32'); amount = 10**(-9/20); fade = round(.05*SR)
    for start in (16,128):
        a,b = round(start*BEAT_S*SR),round((start+64)*BEAT_S*SR)
        envelope[a:b] = amount
        envelope[a:a+fade] = np.linspace(1,amount,fade)
        envelope[b-fade:b] = np.linspace(amount,1,fade)
    artist = sum(sound for group,sound in groups.items() if group!='vocal_record') + groups['vocal_record']*envelope[:,None]
    assert np.isfinite(artist).all() and np.max(abs(artist)) < 1
    sf.write(song/'verse-vocal-light.wav', artist, SR, subtype='PCM_24')
    save(song/'alternate-mix.json', dict(file='verse-vocal-light.wav', parent_mix_sha256=m['mix_sha256'],
         change='vocal-record group -9 dB only in both 16-bar verses, 50 ms gain ramps',
         scope='The sample contains voice AND orchestra; not an isolated-vocal removal',
         sha256=sha(song/'verse-vocal-light.wav')))
    finish_delivery(song, delivery)


def finish_delivery(song, delivery):
    """Collect a validated render; can resume a failed comparison/export without rerendering."""
    if delivery.exists():
        raise FileExistsError(delivery)
    validate_song(song)
    score = json.loads((song/'score.json').read_text())
    m = json.loads((song/'run_manifest.json').read_text())
    slices, placements = score['sample_flip']['slices'], score['sample_flip']['placements']
    source_records = score['sample_flip']['source_records']
    refs = {r['id']: r for r in source_records}
    rag = refs['a4d39d404c04ebe7']
    # Expose the four source recordings followed by the resulting chorus.
    (song/'source-audition').mkdir(exist_ok=True); pieces=[]; parts=[]; cursor=0
    sources = [(source_records[0]['local_source_sha256'], source_records[0]['library_path'], 4.202812,8.126984,'Marion Harris'),
               (rag['sha256'],rag['file'],23.986213,25.332971,'All Star Trio'),
               (refs['rhodes-dust']['sha256'],refs['rhodes-dust']['file'],8*60/115,12*60/115,'Rhodes Dust'),
               (refs['guitar-muffled']['sha256'],refs['guitar-muffled']['file'],0,2.5,'Electric Guitar Muffled')]
    for i,(expected,path,a,b,label) in enumerate(sources):
        assert sha(path)==expected
        info=sf.info(path);sound,rate=sf.read(path,start=round(a*info.samplerate),stop=round(b*info.samplerate),always_2d=True)
        assert rate==SR
        sf.write(song/'source-audition'/f'{i+1:02d}-source.wav',sound,rate,subtype='PCM_24')
        # Keep native mono sources above; match channels only in the listening montage.
        if sound.shape[1] == 1:
            sound = np.repeat(sound, 2, axis=1)
        gain=min(.8/np.max(abs(sound)),.12/np.sqrt(np.mean(sound**2)))
        pieces.extend([sound*gain,np.zeros((SR,2))]);parts.append(dict(label=label,start=cursor,duration=len(sound)/SR));cursor+=len(sound)/SR+1
    a,b=round(80*BEAT_S*SR),round(96*BEAT_S*SR)
    clip,_=sf.read(song/'full_mix.wav',start=a,stop=b,always_2d=True)
    pieces.append(clip*min(.8/np.max(abs(clip)),.12/np.sqrt(np.mean(clip**2))))
    parts.append(dict(label='V2 full hook',start=cursor,duration=len(clip)/SR))
    sf.write(song/'sources-to-beat.wav',np.concatenate(pieces),SR,subtype='PCM_24')
    save(song/'sources-to-beat-map.json',dict(parts=parts,matching='RMS with peak guard; not LUFS matched'))
    export_song(song,delivery/'AbletonProject',set_name='AfterglowCollage')
    rack=delivery/'ChopRack';(rack/'Samples').mkdir(parents=True);pads=[]
    for s in slices:
        relative=f'Samples/{s["midi_note"]:02d}-{s["id"]}.wav';shutil.copy2(s['file'],rack/relative)
        pads.append(dict(midi=s['midi_note'],name=s['id'].upper(),
                    choke={'vocal_record':1,'keys':2,'guitar':3,'rag':4}[s['group']],file=relative))
    build_rack(pads,rack,rack);temp=rack/'Windowlight 16 Pads.adg';root=xml(temp)
    value(root,'.//DrumGroupDevice/UserName','Afterglow 24 Collage Pads')
    value(root,'.//DrumGroupDevice/PadScrollPosition',12)
    (rack/'Afterglow 24 Collage Pads.adg').write_bytes(gzip.compress(ET.tostring(root,encoding='utf-8',xml_declaration=True),mtime=0));temp.unlink()
    for name in ('slice-map.json','phrase-chops.mid','source-provenance.json'):
        shutil.copy2(song/name,rack/name)
    (rack/'使用说明.md').write_text('# Afterglow v2 · 24 Pads\n\n94 BPM，64小节。把ADG和MIDI拖到同一MIDI轨；48–71对应24切片，四来源组各自互斥。保留完整文件夹。\n\n此为私人继续创作工程，不是待出售的采样包。成品的滤波、空间与增益在音频工程中；练习预设不承诺相同混音。Live实际加载/发声/回渲染仍待验。\n')
    vocal_beats=sum(p['duration_beats'] for p in placements if p['group']=='vocal_record')
    print(json.dumps(dict(run=RUN,duration=m['duration_seconds'],sources=4,slices=len(slices),
          triggers=len(placements),tracks=len(m['tracks']),vocal_record_event_coverage=vocal_beats/(BARS*4),
          mix_sha256=m['mix_sha256']),ensure_ascii=False),flush=True)


if __name__ == '__main__':
    build()
