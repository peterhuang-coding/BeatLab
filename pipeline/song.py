"""Render an explicit, editable song score using local sample instruments.

This is a curated composition path, not the automatic Hero Sample selector.
One common linear gain is applied to every track and the listening mix, so the
delivered stems really reconstruct what the user hears. No user database writes.
"""
from __future__ import annotations

import argparse
from fractions import Fraction
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re

import numpy as np
import soundfile as sf
from scipy.signal import butter, resample_poly, sosfilt
from mido import MidiFile, MidiTrack, Message, MetaMessage, bpm2tempo

CORE = Path('/Applications/Ableton Live 12 Suite.app/Contents/App-Resources/Core Library/Samples/One Shots')


@lru_cache(maxsize=64)
def _sample(path: str, root_midi: int, note: int, sr: int) -> np.ndarray:
    audio, source_sr = sf.read(path, dtype='float32', always_2d=True)
    if not len(audio) or not np.isfinite(audio).all():
        raise ValueError(f'Invalid sample: {path}')
    audio = np.repeat(audio, 2, axis=1) if audio.shape[1] == 1 else audio[:, :2]
    # Resampling behaves like a sampler: pitch and natural decay move together.
    ratio = (sr / source_sr) / (2 ** ((note-root_midi)/12))
    fraction = Fraction(ratio).limit_denominator(512)
    audio = resample_poly(audio, fraction.numerator, fraction.denominator, axis=0).astype('float32')
    peak = float(np.max(abs(audio)))
    if peak > 0:
        audio = audio / peak
    return audio


def sample_voice(path: Path, root_midi: int, note: int, duration_s: float,
                 sr: int = 44100) -> np.ndarray:
    if not np.isfinite(duration_s) or duration_s <= 0:
        raise ValueError('Note duration must be positive')
    n = round(duration_s*sr)
    voice = np.zeros((n, 2), dtype='float32')
    source = _sample(str(path), int(root_midi), int(note), sr)
    take = min(len(source), n)
    voice[:take] = source[:take]
    attack = min(round(.003*sr), n)
    release = min(round(.045*sr), n//3)
    voice[:attack] *= np.linspace(0, 1, attack, dtype='float32')[:, None]
    if release:
        voice[-release:] *= np.linspace(1, 0, release, dtype='float32')[:, None]
    return voice


def _midi(events: list[dict], bpm: float, path: Path, drum: bool = False,
          root_midi: int = 60) -> None:
    mid = MidiFile(ticks_per_beat=480)
    track = MidiTrack()
    mid.tracks.append(track)
    track.append(MetaMessage('set_tempo', tempo=bpm2tempo(bpm)))
    queue = []
    for e in events:
        start = round(float(e['beat'])*480)
        end = start + max(1, round(float(e['duration_beats'])*480))
        note = int(e.get('note', root_midi))
        vel = max(1, min(127, round(float(e.get('velocity', .7))*127)))
        queue.extend([(start, 1, note, vel), (end, 0, note, 0)])
    previous = 0
    for tick, on, note, vel in sorted(queue):
        track.append(Message('note_on' if on else 'note_off', note=note, velocity=vel,
                             channel=9 if drum else 0, time=tick-previous))
        previous = tick
    mid.save(path)


def _space(audio: np.ndarray, sr: int, amount: float) -> np.ndarray:
    if amount <= 0:
        return audio
    wet = np.zeros_like(audio)
    for seconds, level, swap in [(.031,.45,False),(.053,.34,True),(.089,.25,False),
                                  (.137,.18,True),(.211,.12,False),(.307,.07,True)]:
        offset = round(seconds*sr)
        if offset < len(audio):
            wet[offset:] += level*(audio[:-offset, ::-1] if swap else audio[:-offset])
    wet = sosfilt(butter(2, min(4500, sr*.4), fs=sr, output='sos'), wet, axis=0)
    return (audio + wet*amount).astype('float32')


def render_score(score: dict, out: Path, sr: int = 44100) -> dict:
    out = Path(out)
    if out.exists() and any(out.iterdir()):
        raise FileExistsError(f'Choose a new version directory: {out}')
    bpm = float(score['bpm'])
    bars = int(score['bars'])
    if not 30 <= bpm <= 250 or bars <= 0:
        raise ValueError('Invalid tempo or bar count')
    beat_s = 60/bpm
    tail = float(score.get('tail_seconds', 2))
    n = round((bars*4*beat_s+tail)*sr)
    definitions = score['tracks']
    if not definitions:
        raise ValueError('The score has no tracks')
    root = Path(score.get('sample_root', str(CORE))).expanduser()
    paths = {}
    for track in definitions:
        tid = track['id']
        if not re.fullmatch(r'[a-z][a-z0-9_-]*', tid) or tid in paths:
            raise ValueError(f'Invalid or duplicate track id: {tid}')
        paths[tid] = root / track['sample']
        if not paths[tid].is_file():
            raise FileNotFoundError(paths[tid])
        for event in track['events']:
            if not 0 <= float(event['beat']) < bars*4 or float(event['duration_beats']) <= 0:
                raise ValueError(f'Event outside score: {tid} {event}')
            if not 0 <= int(event.get('note', track.get('root_midi', 60))) <= 127:
                raise ValueError('MIDI note outside range')
    layers = {}
    for track in definitions:
        tid = track['id']
        audio = np.zeros((n, 2), dtype='float32')
        root_midi = int(track.get('root_midi', 60))
        gain = 10**(float(track.get('gain_db', -12))/20)
        for e in track['events']:
            position = round(float(e['beat'])*beat_s*sr)
            voice = sample_voice(paths[tid], root_midi, int(e.get('note', root_midi)),
                                 float(e['duration_beats'])*beat_s, sr)
            end = min(position+len(voice), n)
            audio[position:end] += voice[:end-position]*gain*float(e.get('velocity', .7))
        low = float(track.get('highpass_hz', 0))
        high = float(track.get('lowpass_hz', 0))
        if low:
            audio = sosfilt(butter(2, low, btype='highpass', fs=sr, output='sos'), audio, axis=0)
        if high:
            audio = sosfilt(butter(2, min(high, sr*.45), fs=sr, output='sos'), audio, axis=0)
        audio = _space(audio, sr, float(track.get('room', 0)))
        pan = float(track.get('pan', 0))
        if not -1 <= pan <= 1:
            raise ValueError('Pan must be between -1 and 1')
        audio[:, 0] *= min(1., 1-pan)
        audio[:, 1] *= min(1., 1+pan)
        fade = min(round(float(score.get('fade_seconds', 2))*sr), n)
        if fade:
            audio[-fade:] *= np.linspace(1, 0, fade)[:, None]
        layers[tid] = audio.astype('float32')
        print(f'[song] {tid}: {len(track["events"])} notes', flush=True)
    mix = sum(layers.values(), np.zeros((n, 2), dtype='float32'))
    peak = float(np.max(np.abs(mix)))
    if peak < 1e-6 or not np.isfinite(mix).all():
        raise ValueError('The rendered song is silent or invalid')
    gain = 10**(-1/20)/peak
    # Also constrain each stem to avoid integer-WAV clipping when tracks cancel.
    stem_peak = max(float(np.max(abs(x))) for x in layers.values())
    gain = min(gain, .98/stem_peak)
    out.mkdir(parents=True, exist_ok=True)
    (out/'stems').mkdir()
    (out/'midi').mkdir()
    sf.write(out/'full_mix.wav', mix*gain, sr, subtype='PCM_24')
    records = []
    for track in definitions:
        tid = track['id']
        stem = f'stems/{tid}.wav'
        midi = f'midi/{tid}.mid'
        sf.write(out/stem, layers[tid]*gain, sr, subtype='PCM_24')
        _midi(track['events'], bpm, out/midi, bool(track.get('drum', False)),
              int(track.get('root_midi', 60)))
        records.append({'id':tid, 'name':track.get('name',tid), 'file':stem, 'midi':midi,
                        'source':str(paths[tid]), 'source_sha256':hashlib.sha256(paths[tid].read_bytes()).hexdigest(),
                        'sha256':hashlib.sha256((out/stem).read_bytes()).hexdigest()})
    manifest = {'title':score['title'], 'bpm':bpm, 'bars':bars, 'duration_seconds':n/sr,
                'sample_rate':sr, 'tracks':records, 'mix':'full_mix.wav', 'master_gain':gain,
                'mix_processing':'identical common linear gain on listening mix and all stems',
                'composition_mode':'curated explicit note score using sample instruments',
                'license':score.get('license', {}), 'sections':score.get('sections', [])}
    (out/'score.json').write_text(json.dumps(score, ensure_ascii=False, indent=2)+'\n')
    (out/'run_manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+'\n')
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--score', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    manifest = render_score(json.loads(args.score.read_text()), args.out)
    print(json.dumps({'out':str(args.out.resolve()), 'duration':manifest['duration_seconds']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
