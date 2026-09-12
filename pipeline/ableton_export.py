"""Collect rendered song stems into an editable Ableton Live 12 audio Set.

This exporter validates media and XML, not Live's audio engine. An actual Live
open, reopen, and render comparison remain separate, explicitly unverified steps.
The included MIDI/score are composition sources; no sampler instruments are rebuilt.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from copy import deepcopy
import gzip
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import tempfile
import xml.etree.ElementTree as ET

from mido import MidiFile
import numpy as np
import soundfile as sf

CORE = Path('/Applications/Ableton Live 12 Suite.app/Contents/App-Resources/Core Library')
DEFAULT_TEMPLATE = CORE / 'Defaults/Creating Tracks/Audio Track/Default Audio Track.als'
DEFAULT_CLIP_TEMPLATE = CORE / 'Lessons/Sets/Driver Error Compensation.als'
ARRANGEMENT = 'DeviceChain/MainSequencer/Sample/ArrangerAutomation/Events'


def _sha256(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _media(root: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or '..' in path.parts:
        raise ValueError(f'Media must use a project-relative path: {relative}')
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f'Media points outside the song: {relative}')
    if not resolved.is_file():
        raise FileNotFoundError(resolved)
    return resolved


def _check_hash(path: Path, expected: str | None, required: bool = False) -> str:
    actual = _sha256(path)
    if expected is None and not required:
        return actual
    if not isinstance(expected, str) or not re.fullmatch(r'[a-fA-F0-9]{64}', expected):
        raise ValueError(f'Missing or invalid SHA256 hash: {path.name}')
    if actual != expected.lower():
        raise ValueError(f'SHA256 hash mismatch: {path.name}')
    return actual


def validate_song(song: Path) -> dict:
    """Read-only shared delivery check, returning JSON-serializable metadata.

    Required inputs are manifest, score, full mix, stems, and MIDI. The original
    one-shot sources are provenance, not dependencies of this audio delivery.
    Audio is compared in blocks to keep memory use independent of song length.
    """
    song = Path(song).expanduser().resolve()
    manifest_path = _media(song, 'run_manifest.json')
    manifest = json.loads(manifest_path.read_text())
    definitions = manifest.get('tracks')
    if not isinstance(definitions, list) or not definitions:
        raise ValueError('The song has no tracks')
    bpm = float(manifest['bpm'])
    duration = float(manifest['duration_seconds'])
    sr = int(manifest['sample_rate'])
    if not 30 <= bpm <= 250 or not math.isfinite(duration) or duration <= 0 or sr <= 0:
        raise ValueError('Invalid tempo, duration, or sample rate')
    score = _media(song, 'score.json')
    json.loads(score.read_text())
    mix_path = _media(song, manifest['mix'])
    mix_sha = _check_hash(mix_path, manifest.get('mix_sha256'))
    mix_info = sf.info(mix_path)
    if not mix_info.frames or mix_info.channels not in (1, 2):
        raise ValueError('Empty mix or unsupported channel count')
    if mix_info.samplerate != sr:
        raise ValueError('Manifest and mix sample rate differ')
    if abs(mix_info.frames / sr - duration) > .5 / sr:
        raise ValueError('Manifest duration does not match audio length')
    tracks, paths, ids = [], [], set()
    for definition in definitions:
        tid = definition['id']
        if not re.fullmatch(r'[a-z][a-z0-9_-]*', tid) or tid in ids:
            raise ValueError(f'Invalid or duplicate track id: {tid}')
        ids.add(tid)
        for key in ('start_seconds', 'start_beats', 'start_frame', 'start_sample'):
            if float(definition.get(key, 0)) != 0:
                raise ValueError(f'All tracks must start at origin 0: {tid}')
        path = _media(song, definition['file'])
        sha = _check_hash(path, definition.get('sha256'), required=True)
        info = sf.info(path)
        if (info.frames, info.samplerate, info.channels) != (
                mix_info.frames, mix_info.samplerate, mix_info.channels):
            raise ValueError(f'Track length/sample rate/channels do not align with mix: {tid}')
        midi = _media(song, definition['midi'])
        midi_sha = _check_hash(midi, definition.get('midi_sha256'))
        if not any(m.type == 'note_on' and m.velocity > 0 for t in MidiFile(midi).tracks for m in t):
            raise ValueError(f'Empty MIDI note source: {tid}')
        tracks.append({**definition, 'sha256': sha, 'midi_sha256': midi_sha, 'peak': 0.0})
        paths.append(path)
    peak_error, residual_energy, mix_energy = 0.0, 0.0, 0.0
    with ExitStack() as stack:
        mix_file = stack.enter_context(sf.SoundFile(mix_path))
        stems = [stack.enter_context(sf.SoundFile(path)) for path in paths]
        while True:
            reference = mix_file.read(65536, dtype='float64', always_2d=True)
            if not len(reference):
                break
            if not np.isfinite(reference).all():
                raise ValueError('Mix contains nonfinite samples')
            summed = np.zeros_like(reference)
            for record, stream in zip(tracks, stems):
                audio = stream.read(len(reference), dtype='float64', always_2d=True)
                if audio.shape != reference.shape or not np.isfinite(audio).all():
                    raise ValueError(f'Invalid or changing audio: {record["id"]}')
                record['peak'] = max(record['peak'], float(np.max(np.abs(audio))))
                summed += audio
            residual = summed - reference
            peak_error = max(peak_error, float(np.max(np.abs(residual))))
            residual_energy += float(np.sum(residual ** 2))
            mix_energy += float(np.sum(reference ** 2))
    for track in tracks:
        if track['peak'] <= 1e-12:
            raise ValueError(f'Silent required track: {track["id"]}')
    if mix_energy <= 1e-20:
        raise ValueError('Silent listening mix')
    rms_error = math.sqrt(residual_energy / (mix_info.frames * mix_info.channels))
    relative_rms = math.sqrt(residual_energy / mix_energy)
    # song.py writes every stem and the float32 sum separately as PCM_24. Allow
    # the independent 24-bit quantization errors plus float32 summation rounding.
    tolerance = (len(tracks) + 2) * 2 ** -23
    if peak_error > tolerance or relative_rms > 1e-3:
        raise ValueError(f'Stems do not reconstruct full mix: residual peak={peak_error:.9g}, '
                         f'relative RMS={relative_rms:.9g}')
    return {'song': str(song), 'manifest': manifest, 'manifest_sha256': _sha256(manifest_path),
            'sample_rate': sr, 'frames': mix_info.frames, 'channels': mix_info.channels,
            'duration_seconds': mix_info.frames / sr, 'mix': manifest['mix'],
            'mix_sha256': mix_sha, 'score': 'score.json', 'score_sha256': _sha256(score),
            'tracks': tracks,
            'stem_sum': {'comparison': 'decoded source stems summed versus source full_mix; not a Live render',
                         'peak_error': peak_error, 'rms_error': rms_error,
                         'relative_rms_db': 20 * math.log10(relative_rms) if relative_rms else None,
                         'tolerance': tolerance, 'passed': True},
            'live_verification': {'open': 'unverified', 'reopen': 'unverified',
                                  'relocated_open': 'unverified', 'rerender': 'unverified'}}


def _value(element: ET.Element, path: str, value) -> None:
    child = element.find(path)
    if child is None:
        raise ValueError(f'Unsupported Ableton template: missing {path}')
    child.set('Value', str(value))


def _global_id(element: ET.Element) -> bool:
    return element.tag in ('Pointee', 'AutomationTarget') or element.tag.endswith('ModulationTarget')


def _reassign_ids(element: ET.Element, next_id: int) -> int:
    """Remap Live's global targets and references; collection-local Ids stay local."""
    mapping = {}
    for child in element.iter():
        if _global_id(child):
            old = child.attrib['Id']
            if old in mapping:
                raise ValueError(f'Duplicate global target in template: {old}')
            mapping[old] = str(next_id)
            child.set('Id', str(next_id))
            next_id += 1
    for child in element.iter():
        if child.tag.endswith(('PointeeId', 'TargetId')):
            old = child.get('Value')
            if old in mapping:
                child.set('Value', mapping[old])
        elif child.tag.endswith(('PointeeRef', 'TargetRef')):
            for attr in ('Id', 'Value'):
                old = child.get(attr)
                if old in mapping:
                    child.set(attr, mapping[old])
    return next_id


def _unity(track: ET.Element) -> None:
    for path, value in {'DeviceChain/Mixer/Volume/Manual': 1,
                        'DeviceChain/Mixer/Pan/Manual': 0,
                        'DeviceChain/Mixer/PanMode': 0,
                        'DeviceChain/Mixer/Speaker/Manual': 'true',
                        'DeviceChain/Mixer/On/Manual': 'true',
                        'DeviceChain/Mixer/SoloSink': 'false',
                        'DeviceChain/Mixer/CrossFadeState/Manual': 1,
                        'TrackDelay/Value': 0}.items():
        _value(track, path, value)
    track.find('AutomationEnvelopes/Envelopes').clear()


def _set_xml(data: dict, template: Path, clip_template: Path) -> ET.Element:
    root = ET.fromstring(gzip.decompress(template.read_bytes()))
    live = root.find('LiveSet')
    if live is None or not root.get('MinorVersion', '').startswith('12.'):
        raise ValueError('Expected a Live 12 default Audio Track template')
    tracks = live.find('Tracks')
    if tracks is None or len(tracks) != 1 or tracks[0].tag != 'AudioTrack':
        raise ValueError('Expected exactly one default AudioTrack')
    if root.findall('.//FileRef') or root.findall('.//AudioClip') or root.findall('.//MidiClip'):
        raise ValueError('Default template must not contain media or clips')
    if any(len(e) for e in root.findall('.//Devices')):
        raise ValueError('Default template must not contain FX or instruments')
    base = deepcopy(tracks[0])
    tracks.clear()
    example = ET.fromstring(gzip.decompress(clip_template.read_bytes()))
    clip = example.find('.//AudioTrack/' + ARRANGEMENT + '/AudioClip')
    if clip is None:
        raise ValueError('Clip template has no arrangement AudioClip')
    clip = deepcopy(clip)
    if clip.findall('.//Devices'):
        raise ValueError('Clip template contains devices')
    duration = data['duration_seconds']
    bpm = float(data['manifest']['bpm'])
    beats = duration * bpm / 60
    next_id = max([int(e.get('Id')) for e in root.iter() if _global_id(e)] +
                  [int(live.find('NextPointeeId').get('Value'))]) + 1
    for index, record in enumerate(data['tracks']):
        track = deepcopy(base)
        track.set('Id', str(index + 1))
        next_id = _reassign_ids(track, next_id)
        _unity(track)
        _value(track, 'Name/EffectiveName', record.get('name', record['id']))
        _value(track, 'Name/UserName', record.get('name', record['id']))
        _value(track, 'Color', (index * 8 + 12) % 70)
        _value(track, 'DeviceChain/MainSequencer/MonitoringEnum', 2)
        _value(track, 'DeviceChain/MainSequencer/Recorder/IsArmed', 'false')
        _value(track, 'DeviceChain/AudioOutputRouting/Target', 'AudioOut/Main')
        audio = deepcopy(clip)
        audio.set('Id', str(index))
        audio.set('Time', '0')
        next_id = _reassign_ids(audio, next_id)
        for path, value in {'CurrentStart': 0, 'CurrentEnd': beats,
                            'Loop/LoopStart': 0, 'Loop/LoopEnd': duration,
                            'Loop/StartRelative': 0, 'Loop/LoopOn': 'false',
                            'Loop/OutMarker': duration, 'Loop/HiddenLoopStart': 0,
                            'Loop/HiddenLoopEnd': duration, 'Name': record.get('name', record['id']),
                            'Color': (index * 8 + 12) % 70, 'IsWarped': 'false',
                            'Disabled': 'false', 'SampleVolume': 1, 'PitchCoarse': 0,
                            'PitchFine': 0, 'Fade': 'false', 'Fades/FadeInLength': 0,
                            'Fades/FadeOutLength': 0, 'IsSongTempoLeader': 'false',
                            'SampleRef/DefaultDuration': data['frames'],
                            'SampleRef/DefaultSampleRate': data['sample_rate'],
                            'SampleRef/LastModDate': 0, 'SampleRef/SamplesToAutoWarp': 0}.items():
            _value(audio, path, value)
        audio.find('Envelopes/Envelopes').clear()
        audio.find('SampleRef/SourceContext').clear()
        audio.find('Onsets/UserOnsets').clear()
        _value(audio, 'Onsets/HasUserOnsets', 'false')
        relative = f'Samples/Imported/{record["id"]}.wav'
        source = Path(data['song']) / record['file']
        for path, value in {'RelativePathType': 3, 'RelativePath': relative, 'Path': '',
                            'Type': 2, 'LivePackName': '', 'LivePackId': '',
                            'OriginalFileSize': source.stat().st_size, 'OriginalCrc': 0,
                            'SourceHint': ''}.items():
            _value(audio, 'SampleRef/FileRef/' + path, value)
        markers = audio.find('WarpMarkers')
        markers.clear()
        ET.SubElement(markers, 'WarpMarker', Id='0', SecTime='0', BeatTime='0')
        ET.SubElement(markers, 'WarpMarker', Id='1', SecTime=str(duration), BeatTime=str(beats))
        audio.find('SavedWarpMarkersForStretched').clear()
        events = track.find(ARRANGEMENT)
        if events is None:
            raise ValueError('Default track has no arrangement events container')
        events.clear()
        events.append(audio)
        tracks.append(track)
    master = live.find('MainTrack')
    if master is None:
        raise ValueError('Default Live 12 template has no MainTrack')
    _unity(master)
    _value(master, 'DeviceChain/Mixer/Tempo/Manual', bpm)
    _value(master, 'DeviceChain/Mixer/TimeSignature/Manual', 201)  # Live's 4/4 enum.
    _value(live, 'NextPointeeId', next_id)
    for path, value in {'Transport/LoopStart': 0, 'Transport/LoopLength': beats,
                        'Transport/LoopOn': 'false', 'Transport/CurrentTime': 0,
                        'Transport/LoopIsSongStart': 'true', 'Transport/PunchIn': 'false',
                        'Transport/PunchOut': 'false', 'SelectedDocumentViewInMainWindow': 0,
                        'Annotation': data['manifest']['title']}.items():
        _value(live, path, value)
    locators = live.find('Locators/Locators')
    locators.clear()
    for index, section in enumerate(data['manifest'].get('sections', [])):
        position = float(section['start_bar']) * 4
        if not 0 <= position < beats:
            raise ValueError(f'Section outside arrangement: {section["name"]}')
        locator = ET.SubElement(locators, 'Locator', Id=str(index))
        for key, value in {'LomId': 0, 'Time': position, 'Name': section['name'],
                           'Annotation': '', 'IsSongStart': 'true' if position == 0 else 'false'}.items():
            ET.SubElement(locator, key, Value=str(value))
    ids = [int(e.attrib['Id']) for e in root.iter() if _global_id(e)]
    if len(ids) != len(set(ids)) or next_id <= max(ids):
        raise ValueError('Duplicate or unallocated global Ableton target Id')
    if any(int(e.get('Value')) not in ids for e in root.findall('.//PointeeId')):
        raise ValueError('Dangling Ableton PointeeId reference')
    if len(root.findall('.//FileRef')) != len(data['tracks']):
        raise ValueError('Unexpected media reference in generated Set')
    return root


def export_song(song: Path, out: Path, *, template: Path = DEFAULT_TEMPLATE,
                clip_template: Path = DEFAULT_CLIP_TEMPLATE, set_name: str = 'Windowlight') -> dict:
    """Validate first, build in a temporary sibling, then publish to a new folder."""
    out = Path(out).expanduser().resolve()
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise FileExistsError(f'Choose a new empty project directory: {out}')
    if not set_name or set_name in ('.', '..') or '/' in set_name or '\\' in set_name:
        raise ValueError('Set name must be a single filename without a directory')
    data = validate_song(song)
    root = _set_xml(data, Path(template), Path(clip_template))
    result = {**data, 'project': str(out), 'als': str(out / f'{set_name}.als'),
              'export_mode': 'collected audio arrangement, warp off, no added FX, unity gains',
              'midi_reconstruction': 'MIDI and score copied as editable sources; instruments not recreated',
              'template_creator': root.get('Creator'),
              'template_sha256': _sha256(Path(template)),
              'clip_template_sha256': _sha256(Path(clip_template)), 'media': []}
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f'.{out.name}-', dir=out.parent) as temporary:
        stage = Path(temporary) / 'project'
        for directory in ('Samples/Imported', 'Source/midi', 'Reference', 'Ableton Project Info'):
            (stage / directory).mkdir(parents=True)
        copies = [(data['mix'], 'Reference/full_mix.wav', data['mix_sha256']),
                  ('score.json', 'Source/score.json', data['score_sha256']),
                  ('run_manifest.json', 'Source/run_manifest.json', data['manifest_sha256'])]
        for track in data['tracks']:
            relative = f'Samples/Imported/{track["id"]}.wav'
            copies += [(track['file'], relative, track['sha256']),
                       (track['midi'], f'Source/midi/{track["id"]}.mid', track['midi_sha256'])]
            result['media'].append({'track_id': track['id'], 'relative_path': relative,
                                    'sha256': track['sha256']})
        for source, destination, expected in copies:
            copied = stage / destination
            shutil.copy2(Path(data['song']) / source, copied)
            _check_hash(copied, expected, required=True)
        xml = ET.tostring(root, encoding='utf-8', xml_declaration=True)
        (stage / f'{set_name}.als').write_bytes(gzip.compress(xml, mtime=0))
        result['als_sha256'] = _sha256(stage / f'{set_name}.als')
        (stage / 'export_manifest.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
        (stage / 'README.txt').write_text(
            f'{data["manifest"]["title"]}\n\n'
            f'Open {set_name}.als in Ableton Live 12. Keep this entire project folder together.\n'
            'Each track is the original rendered stem at time zero. Warp off; track and master gain 0 dB.\n'
            'Reference/full_mix.wav is the listening reference, excluded from the arrangement.\n'
            'Source/midi and Source/score.json allow further composition editing. MIDI instruments\n'
            'and source sample processing are not reconstructed in this audio Set.\n\n'
            'Verified: source hashes, equal audio lengths/rates, non-silent stems, source stem-sum residual,\n'
            'collected media hashes and XML structure. Live open/reopen/relocation and Live render remain\n'
            'UNVERIFIED. The numerical comparison is not a Live rerender.\n')
        # Recheck after staging so user files created meanwhile cannot be overwritten.
        if out.exists():
            out.rmdir()  # Refuses a nonempty directory, including a concurrent user edit.
        stage.rename(out)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--song', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--template', type=Path, default=DEFAULT_TEMPLATE)
    parser.add_argument('--clip-template', type=Path, default=DEFAULT_CLIP_TEMPLATE)
    parser.add_argument('--set-name', default='Windowlight')
    args = parser.parse_args()
    result = export_song(args.song, args.out, template=args.template,
                         clip_template=args.clip_template, set_name=args.set_name)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
