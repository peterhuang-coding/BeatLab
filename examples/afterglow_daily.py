"""2026-09-16: a traceable vocal-record flip, with an intact motif and gated replies.

Run: .venv/bin/python -m examples.afterglow_daily
Requires the collected Citizen DJ excerpt and Windowlight v2 kit. Media stays local.
This creates a new immutable version; it never rewrites an existing render.
"""
from copy import deepcopy
import gzip
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess
import xml.etree.ElementTree as ET

from mido import MidiFile, MidiTrack, Message, MetaMessage, bpm2tempo
import numpy as np
import soundfile as sf

from pipeline.sample_flip import make_slice
from pipeline.song import render_score
from pipeline.ableton_export import export_song
from examples.windowlight_drum_practice import build_rack, xml, value

REPO = Path(__file__).resolve().parents[1]
RUN = 'afterglow-2026-09-16-v1'
BPM, BARS, SR = 94, 48, 44100
SOURCE_ID = '9c6c32d17de4128c'
SOURCE_SHA = 'c98a33cc7696e93f534cd56d1e4d9d4c9ad04f6d3129cd12343db1be15e45714'
DOWNLOAD_SHA = '65c87d6b556672460a6beba36fdb90318e13950ed338060d6a3b51e81d92c4dd'
TRANSPOSE = -2.11  # -2 semitones, plus estimated +11-cent recording offset correction.
# Local excerpt seconds, not claimed to be sample-accurate offsets in the full record.
# Beat candidates from librosa; selected harmonic windows support F/Am/Dm/C before pitching.
REGIONS = [
    ('a', 4.202812, 5.224490, 2, 39), ('b', 5.224490, 6.176508, 2, 39),
    ('c', 6.176508, 7.151746, 2, 39), ('d', 7.151746, 8.126984, 2, 39),
    ('e', 16.346848, 17.438186, 2, 43), ('f', 17.438186, 18.436644, 2, 43),
    ('g', 22.476916, 23.475374, 2, 36), ('h', 23.475374, 24.520272, 2, 36),
    ('i', 25.518730, 26.586848, 2, 34), ('j', 26.586848, 27.654966, 2, 34),
    ('reverse', 7.151746, 8.126984, 2, 39),
    ('octave', 4.202812, 5.224490, 2, 39),
    ('unfold', 4.202812, 8.126984, 8, 39),
    ('low', 4.202812, 8.126984, 8, 39),
    ('answer', 16.346848, 18.436644, 4, 43),
    ('resolve', 25.518730, 27.654966, 4, 34),
]


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def save(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')


def midi(events, path):
    result = MidiFile(type=0, ticks_per_beat=960)
    track = MidiTrack(); result.tracks.append(track)
    track.extend([MetaMessage('track_name', name='Afterglow phrase chops'),
                  MetaMessage('set_tempo', tempo=bpm2tempo(BPM)),
                  MetaMessage('time_signature', numerator=4, denominator=4)])
    queue = []
    for e in events:
        start, end = [round(x * 960) for x in (e['beat'], e['beat'] + e['duration_beats'])]
        assert 0 <= start < end <= BARS * 4 * 960
        queue.extend([(start, 1, e['note'], max(1, round(e['velocity'] * 127))),
                      (end, 0, e['note'], 0)])
    previous = 0
    for tick, on, note, velocity in sorted(queue):
        track.append(Message('note_on' if on else 'note_off', note=note,
                             velocity=velocity, time=tick - previous))
        previous = tick
    track.append(MetaMessage('end_of_track', time=BARS * 4 * 960 - previous))
    result.save(path)


def build():
    song = REPO / 'beats' / RUN
    prepared = REPO / 'library/instruments' / RUN
    delivery = REPO / 'exports' / RUN
    for path in (song, prepared, delivery):
        if path.exists():
            raise FileExistsError(f'Preserve this version; use a new run ID: {path}')
    source = REPO / 'library/loops' / SOURCE_ID / 'source.wav'
    if sha(source) != SOURCE_SHA:
        raise ValueError('The source recording changed')
    catalog = json.loads((REPO / 'library/sources/citizen_dj/catalog.json').read_text())
    record = next(r for r in catalog if r['id'] == SOURCE_ID)
    if sha(record['original_download']) != DOWNLOAD_SHA:
        raise ValueError('The original downloaded recording changed')
    record.update(performer='Marion Harris', recording_date='1918-10-18',
                  credit='Library of Congress, National Jukebox; Citizen DJ Project',
                  metadata_source='https://www.loc.gov/item/jukebox-313413/',
                  metadata_verified_date='2026-09-16',
                  local_source_sha256=SOURCE_SHA,
                  download_sha256=DOWNLOAD_SHA,
                  sample_rates=dict(download=48000, library=44100),
                  offset_precision='Slice offsets are within the downloaded excerpt; full-record offset unverified')
    prepared.mkdir(parents=True)
    cleaned = prepared / 'record-cleaned.wav'
    # A restrained noise reduction, preserving the original backup and exact frame count.
    filters = 'highpass=f=80,afftdn=nr=5:nf=-34:tn=1,acompressor=threshold=0.12:ratio=2:attack=15:release=160'
    subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-i', str(source), '-af', filters,
                    '-c:a', 'pcm_f32le', str(cleaned)], check=True)
    raw_info, clean_info = sf.info(source), sf.info(cleaned)
    assert (raw_info.frames, raw_info.samplerate) == (clean_info.frames, clean_info.samplerate)
    clean_peak = float(np.max(abs(sf.read(cleaned, dtype='float32')[0])))
    tracks, slices = {}, []
    for index, (tid, start, end, beats, root) in enumerate(REGIONS):
        pitch = TRANSPOSE + (12 if tid == 'octave' else -12 if tid == 'low' else 0)
        sound, rate = make_slice(cleaned, start_s=start, end_s=end, duration_s=beats * 60 / BPM,
                                 semitones=pitch, reverse=tid == 'reverse')
        path = prepared / f'{tid}.wav'
        sf.write(path, sound, rate, subtype='FLOAT')
        relative_peak = max(float(np.max(abs(sound))), 1e-8) / clean_peak
        tracks[tid] = dict(id=tid, name=f'{tid.upper()} · record phrase', sample=path.name,
                           root_midi=60, gain_db=-8.2 + 20 * math.log10(relative_peak),
                           highpass_hz=180 if tid != 'low' else 220,
                           lowpass_hz=4600 if tid != 'low' else 2300,
                           room=.055, events=[])
        slices.append(dict(id=tid, midi_note=48 + index, source_id=SOURCE_ID,
                           source_file=str(source), source_sha256=SOURCE_SHA,
                           start_seconds=start, end_seconds=end, target_beats=beats,
                           semitones=pitch, reverse=tid == 'reverse', bass_midi=root,
                           file=str(path), sha256=sha(path),
                           processed_source=str(cleaned), processed_sha256=sha(cleaned)))
        print(f'[slice] {tid}: {start:.3f}–{end:.3f}s -> {beats} beats, {pitch:+.2f} st', flush=True)
    old_score = json.loads((REPO / 'beats/windowlight-v2/score.json').read_text())
    kit_catalog = json.loads((REPO / 'library/instruments/windowlight-v2/source_catalog.json').read_text())
    kit_sources = []
    for tid in ('kick', 'snare', 'hat', 'shaker', 'rim', 'open_hat', 'bass'):
        track = deepcopy(next(t for t in old_score['tracks'] if t['id'] == tid))
        ref = next(r for r in kit_catalog if r['id'] == tid)
        original = Path(old_score['sample_root']) / track['sample']
        if sha(original) != ref['sha256']:
            raise ValueError(f'Collected kit source changed: {tid}')
        destination = prepared / (tid + original.suffix)
        shutil.copy2(original, destination)
        track.update(sample=destination.name, events=[])
        tracks[tid] = track
        kit_sources.append({**ref, 'file': str(destination), 'parent_collected_file': str(original)})
    for tid, gain in dict(kick=-12.5, snare=-17.4, hat=-30, shaker=-33,
                          rim=-25, open_hat=-32, bass=-17.5).items():
        tracks[tid]['gain_db'] = gain
    tracks['bass'].update(lowpass_hz=680, highpass_hz=32)
    sections = [('Needle / opening', 0, 4), ('Theme A', 4, 8), ('Chopped hook A', 12, 8),
                ('Low room / bridge', 20, 4), ('Theme B / replies', 24, 8),
                ('Chopped hook B', 32, 8), ('Last refrain', 40, 4), ('Afterglow / closing', 44, 4)]
    lookup = {s['id']: s for s in slices}
    placements = []

    def hit(tid, beat, duration, velocity=.7, note=60):
        tracks[tid]['events'].append(dict(beat=round(beat, 5), note=note,
                                          duration_beats=round(duration, 5), velocity=round(velocity, 5)))

    def cut(tid, beat, duration, velocity=.8):
        hit(tid, beat, duration, velocity)
        placements.append(dict(slice_id=tid, beat=beat, duration_beats=duration,
                               velocity=velocity, note=lookup[tid]['midi_note']))

    # The opening exposes the whole phrase before the new rhythmic treatment.
    cut('unfold', 0, 8, .55); cut('answer', 8, 4, .60); cut('resolve', 12, 3.5, .58)
    for section, start, count in sections:
        if section not in ('Theme A', 'Chopped hook A', 'Theme B / replies', 'Chopped hook B'):
            continue
        hooked = 'hook' in section
        later = section in ('Theme B / replies', 'Chopped hook B')
        for chord, (left, right, root) in enumerate([('a', 'b', 39), ('e', 'f', 43),
                                                    ('g', 'h', 36), ('i', 'j', 34)]):
            at = start * 4 + chord * 8
            if not hooked and chord == 0:
                pattern = [('a', 0, 2), ('b', 2, 2), ('c', 4, 2), ('d', 6, 1.85)]
            elif hooked:
                pattern = [(left, 0, 1.5), (left, 1.5, .5), (right, 2, 2),
                           (left, 4, .75), (left, 4.75, .25), (right, 5, 1),
                           (right if later else left, 6.5, 1.45)]
            else:
                pattern = [(left, 0, 2), (right, 2, 1.8), (left, 4.5, 1.5), (right, 6, 1.85)]
            if later and chord == 0:
                pattern[-1] = ('octave' if hooked else 'reverse', pattern[-1][1], pattern[-1][2])
            for tid, offset, duration in pattern:
                velocity = (.81 if hooked else .72) * (.67 if tid == 'octave' else 1)
                cut(tid, at + offset, duration, velocity)
            # Simple supportive roots, plus occasional fifth/approach. Harmony is inferred,
            # not an automatic chord-truth label, and is exposed for user correction.
            for offset, pitch, duration, velocity in [(0.018, root, 1.35, .66),
                    (2.52, root, .85, .52), (4.018, root, 1.45, .62),
                    (6.53, root + 7 if hooked else root, .9, .47)]:
                hit('bass', at + offset, duration, velocity, pitch)
    cut('low', 80, 8, .62); cut('answer', 88, 3.6, .52)
    cut('resolve', 92, 3.5, .51); cut('reverse', 95.5, .5, .31)
    for chord, (left, right, root) in enumerate([('a', 'c', 39), ('e', 'f', 43), ('g', 'h', 36), ('i', 'j', 34)]):
        at = 160 + chord * 4
        cut(left, at, 1.5, .80); cut(left, at + 1.5, .5, .60)
        cut(right, at + 2, 1.9, .77)
        hit('bass', at + .018, 1.4, .65, root)
        hit('bass', at + 2.55, .8, .49, root)
    cut('unfold', 176, 8, .64); cut('resolve', 184, 3.8, .54)
    cut('unfold', 188, 4, .47)
    for beat, pitch in [(8, 43), (12, 34), (88, 43), (92, 34), (176, 39), (180, 39), (184, 34), (188, 39)]:
        hit('bass', beat + .018, 1.8, .48, pitch)
    rng = np.random.default_rng(20260916)
    for bar in range(BARS):
        at = bar * 4
        if bar < 2 or bar >= 47:
            continue
        bridge = 20 <= bar < 24
        intro_outro = bar < 4 or bar >= 44
        hooked = 12 <= bar < 20 or 32 <= bar < 44
        # A short breath before the next section, not a fill on every fourth bar.
        stop = 3 if bar in (11, 31, 43) else 4
        kicks = [(0, .85), (2.55 if bar % 2 else 2.0, .64)]
        if bridge:
            kicks = [(0, .63)] if bar % 2 == 0 else []
        elif hooked and bar % 2:
            kicks += [(1.75, .42), (3.5, .49)]
        for offset, velocity in kicks:
            if offset < stop:
                hit('kick', at + offset, .48, velocity * (.84 if intro_outro else 1), 36)
        for offset in ([2.04] if bridge else [1.03, 3.03]):
            if offset < stop:
                hit('rim' if bridge or intro_outro else 'snare', at + offset, .42,
                    .61 if bridge else .72, 37 if bridge or intro_outro else 38)
        eighths = [.58, 2.58] if bridge else [0, .57, 1, 1.57, 2, 2.57, 3, 3.57]
        for i, offset in enumerate(eighths):
            if offset < stop and not (hooked and bar % 4 == 1 and offset == 2.57):
                hit('hat', at + offset + float(rng.uniform(0, .008)), .14,
                    (.47 if i % 2 == 0 else .28) * (.8 if intro_outro else 1), 42)
        if hooked:
            for offset in [.58, 1.58, 2.58, 3.58]:
                if offset < stop:
                    hit('shaker', at + offset, .18, .38, 70)
            if bar % 4 == 1:
                hit('open_hat', at + 2.57, .33, .38, 46)
        if bar in (7, 17, 27, 37, 41):
            hit('rim', at + 2.8, .18, .31, 37)
            hit('snare', at + 3.77, .16, .23, 38)
    for track in tracks.values():
        assert track['events'], track['id']
        track['events'].sort(key=lambda e: (e['beat'], e['note']))
    ordered = sorted(placements, key=lambda e: e['beat'])
    assert all(a['beat'] + a['duration_beats'] <= b['beat'] + 1e-6
               for a, b in zip(ordered, ordered[1:])), 'Full record phrases must not pile up'
    provenance = dict(record=record, preprocessing=dict(filters=filters, file=str(cleaned),
                      sha256=sha(cleaned), frames=clean_info.frames), instruments=kit_sources)
    score = dict(title='余温 · Afterglow', run_id=RUN, bpm=BPM, bars=BARS,
                 tail_seconds=2, fade_seconds=4, sample_root=str(prepared), tracks=list(tracks.values()),
                 sections=[dict(name=n, start_bar=s, bars=b) for n, s, b in sections],
                 license=dict(status='private creative draft; commercial release not verified',
                              record_rights_source=record['rights_source'],
                              instrument_rights_source=old_score['license']['source_url']),
                 composition_mode='curated historical vocal phrase flip',
                 harmonic_plan=dict(working_key='Eb major', roots=['Eb2', 'G2', 'C2', 'Bb1'],
                                    basis='CQT harmonic windows; musical interpretation, user audition pending'),
                 sample_flip=dict(slices=slices, placements=placements, source_records=[record]),
                 source_provenance=provenance)
    manifest = render_score(score, song)
    manifest.update(run_id=RUN, mix_sha256=sha(song / 'full_mix.wav'),
                    composition_mode=score['composition_mode'], source_provenance=provenance,
                    sample_flip=score['sample_flip'], musical_feedback='awaiting user audition')
    # Keep full bar lengths when importing the MIDI lanes into Live.
    for track in manifest['tracks']:
        p = song / track['midi']; m = MidiFile(p)
        for lane in m.tracks:
            tick = sum(e.time for e in lane if e.type != 'end_of_track')
            lane[:] = [e for e in lane if e.type != 'end_of_track']
            lane.append(MetaMessage('end_of_track', time=BARS * 4 * m.ticks_per_beat - tick))
        m.save(p); track['midi_sha256'] = sha(p)
    save(song / 'run_manifest.json', manifest)
    save(song / 'slice-map.json', slices)
    save(song / 'source-provenance.json', provenance)
    midi(placements, song / 'phrase-chops.mid')
    shutil.copy2(source, song / 'original-source.wav')
    original, _ = sf.read(source, start=round(REGIONS[0][1] * SR), stop=round(REGIONS[3][2] * SR), always_2d=True)
    sf.write(song / 'original-phrase.wav', original, SR, subtype='PCM_24')
    solo = np.zeros((round(manifest['duration_seconds'] * SR), 2), dtype='float32')
    for s in slices:
        solo += sf.read(song / 'stems' / (s['id'] + '.wav'), dtype='float32', always_2d=True)[0]
    assert np.max(abs(solo)) < 1 and np.isfinite(solo).all()
    sf.write(song / 'sample-flip-solo.wav', solo, SR, subtype='PCM_24')
    a, b = [round(x * 60 / BPM * SR) for x in (48, 56)]
    full, _ = sf.read(song / 'full_mix.wav', start=a, stop=b, always_2d=True)
    clips = [('Original phrase', original), ('Hook phrase solo', solo[a:b]), ('Hook full beat', full)]
    comparison, parts, cursor = [], [], 0
    for label, sound in clips:
        gain = min(.82 / np.max(abs(sound)), .12 / np.sqrt(np.mean(sound * sound)))
        comparison.append(sound * gain)
        parts.append(dict(label=label, start_seconds=cursor / SR, duration_seconds=len(sound) / SR))
        cursor += len(sound)
        if label != clips[-1][0]:
            comparison.append(np.zeros((SR, 2))); cursor += SR
    sf.write(song / 'before-after.wav', np.concatenate(comparison), SR, subtype='PCM_24')
    save(song / 'before-after-map.json', dict(parts=parts, matching='RMS with peak guard; not LUFS matched'))
    export_song(song, delivery / 'AbletonProject', set_name='Afterglow')
    rack = delivery / 'ChopRack'; (rack / 'Samples').mkdir(parents=True)
    pads = []
    for s in slices:
        relative = f'Samples/{s["midi_note"]:02d}-{s["id"]}.wav'
        shutil.copy2(s['file'], rack / relative)
        pads.append(dict(midi=s['midi_note'], name=s['id'].upper(), choke=1, file=relative))
    build_rack(pads, rack, rack)
    temporary = rack / 'Windowlight 16 Pads.adg'; root = xml(temporary)
    value(root, './/DrumGroupDevice/UserName', 'Afterglow Record Chops')
    value(root, './/DrumGroupDevice/PadScrollPosition', 12)
    (rack / 'Afterglow Record Chops.adg').write_bytes(gzip.compress(
        ET.tostring(root, encoding='utf-8', xml_declaration=True), mtime=0))
    temporary.unlink()
    for name in ('phrase-chops.mid', 'slice-map.json', 'source-provenance.json', 'original-phrase.wav'):
        shutil.copy2(song / name, rack / name)
    (rack / '使用说明.md').write_text(
        '# 余温 · Afterglow\n\n94 BPM，48 小节；MIDI 48–63 对应 16 个切片。\n\n'
        '把 Afterglow Record Chops.adg 与 phrase-chops.mid 放到同一 MIDI 轨；每个垫同组互斥，'
        '可改顺序、力度、包络与音区。WAV 已包含预处理、变调与时间伸缩。\n\n'
        '完整试听编排在 ../AbletonProject/Afterglow.als，使用已渲染音频分轨。此 Drum Rack 不含'
        '成品逐轨滤波、空间、鼓、Bass 和总线增益，所以练习预设不承诺听起来与成品完全一样。'
        '文件引用和音频可验证；Live 实际打开、重开、发声与回渲染仍待验。请保留整个文件夹。\n')
    print(json.dumps(dict(song=str(song), delivery=str(delivery), duration=manifest['duration_seconds'],
                         slices=len(slices), placements=len(placements), mix_sha256=manifest['mix_sha256']),
                     ensure_ascii=False), flush=True)


if __name__ == '__main__':
    build()
