"""Build a private, playable 16-pad practice kit from Windowlight v2 sources.

Run from the repository: python -m examples.windowlight_drum_practice
The factory preset XML is read locally, never vendored into the repository.
"""
from copy import deepcopy
import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import xml.etree.ElementTree as ET

from mido import MidiFile, MidiTrack, Message, MetaMessage, bpm2tempo
import numpy as np
import soundfile as sf
from scipy.signal import butter, sosfilt

try:
    from pipeline.song import sample_voice
    from pipeline.ableton_export import _global_id
except ModuleNotFoundError as exc:
    # Existing CLI/tests also expose pipeline/ as a flat module directory.
    if exc.name not in ('pipeline.song', 'pipeline.ableton_export'):
        raise
    from song import sample_voice
    from ableton_export import _global_id

REPO = Path(__file__).resolve().parents[1]
CORE = Path('/Applications/Ableton Live 12 Suite.app/Contents/App-Resources/Core Library')
PARENT_SHA = 'f339d0743e5f8f0f5d76920fdc0339babd546e9cbc1b0146e096a1562a1e80b7'
BPM = 88


def pads():
    definitions = [
        ('kick', 36, 'Kick', 0), ('snare', 38, 'Snare', 0),
        ('hat', 42, 'Closed Hat', 1), ('open_hat', 46, 'Open Hat', 1),
        ('rim', 37, 'Rim', 0), ('shaker', 70, 'Shaker', 0),
        ('vocal_oh', 53, 'Oh F', 2), ('vocal_oh', 55, 'Oh G', 2),
        ('vocal_oh', 57, 'Oh A', 2), ('vocal_oh', 59, 'Oh B', 2),
        ('vocal_oh', 60, 'Oh C', 2), ('vocal_oh', 62, 'Oh D', 2),
        ('vocal_ah', 57, 'Ah A', 2), ('vocal_ah', 60, 'Ah C', 2),
        ('vocal_ah', 62, 'Ah D', 2), ('vocal_ah', 64, 'Ah E', 2),
    ]
    return [dict(midi=36+i, source_id=source, pitch=pitch, name=name, choke=choke,
                 file=f'Samples/{36+i:02d}-{name.replace(" ", "-")}.wav')
            for i, (source, pitch, name, choke) in enumerate(definitions)]


def patterns():
    result = []
    lookup = {(p['source_id'], p['pitch']): p['midi'] for p in pads()}
    for index, title in enumerate(['01-Foundation', '02-Pocket', '03-Vocal-Call-Response', '04-Fill-Turnaround']):
        events = []

        def hit(beat, note, velocity, length=.25):
            events.append(dict(beat=round(beat, 5), note=note, velocity=velocity,
                               duration_beats=min(length, 32-beat)))

        for bar in range(8):
            at = bar*4
            for off, vel in [(0, .78), (2.5 if bar % 2 else 2.0, .63)]:
                hit(at+off, 36, vel, .5)
            for off in [1.025 if index else 1, 3.025 if index else 3]:
                hit(at+off, 37, .70 if off < 2 else .76, .45)
            for eighth in range(8):
                off = eighth*.5 + (.06 if index and eighth % 2 else 0)
                hit(at+off, 38, .42 if eighth % 2 == 0 else .29, .18)
            if index:
                if bar % 2:
                    hit(at+3.76, 37, .21, .17)
                    hit(at+1.76, 40, .31, .2)
                for off in [.56, 1.56, 2.56, 3.56]:
                    hit(at+off, 41, .33, .18)
            if index == 3 and bar % 4 == 3:
                # Replace the last hat with an open hat; next closed hat chokes it.
                events[:] = [e for e in events if not (e['note'] == 38 and at+3.5 <= e['beat'] < at+4)]
                hit(at+3.5, 39, .42, .48)
                for off, vel in [(2.76, .23), (3.25, .32), (3.75, .42)]:
                    hit(at+off, 37, vel, .19)
                hit(at+3.75, 36, .46, .24)
        if index >= 2:
            motifs = [[60, 60, 57, 55], [59, 62, 59, 55], [57, 60, 57, 53], [59, 62, 60, 59]]
            for phrase, pitches in enumerate(motifs):
                at = phrase*8
                for off, pitch, duration, velocity in zip([.15,.74,1.65,2.38], pitches, [.42,.70,.52,.95], [.67,.78,.66,.76]):
                    hit(at+off, lookup[('vocal_oh', pitch)], velocity, duration)
                hit(at+5.56, lookup[('vocal_ah', [64,62,60,62][phrase])], .46, .5)
                if index == 3:
                    hit(at+6.75, lookup[('vocal_ah', 60)], .30, .2)
                    hit(at+7.125, lookup[('vocal_oh', pitches[-1])], .37, .23)
        result.append(dict(id=title, bars=8, events=sorted(events, key=lambda e:(e['beat'],e['note']))))
    return result


def write_midi(events, beats, path):
    mid = MidiFile(type=0, ticks_per_beat=960)
    track = MidiTrack()
    mid.tracks.append(track)
    track.extend([MetaMessage('track_name', name=Path(path).stem),
                  MetaMessage('set_tempo', tempo=bpm2tempo(BPM)),
                  MetaMessage('time_signature', numerator=4, denominator=4)])
    queue = []
    for event in events:
        start = round(event['beat']*960)
        end = round((event['beat']+event['duration_beats'])*960)
        note = event['note']
        if not (36 <= note <= 51 and 0 <= start < end <= beats*960
                and 0 < event['velocity'] <= 1):
            raise ValueError('Invalid or unmapped drum-pad event')
        queue += [(start, 1, note, max(1, round(event['velocity']*127))), (end, 0, note, 0)]
    last = 0
    for tick, on, note, velocity in sorted(queue):
        track.append(Message('note_on' if on else 'note_off', note=note, velocity=velocity,
                             channel=9, time=tick-last))
        last = tick
    track.append(MetaMessage('end_of_track', time=round(beats*960)-last))
    mid.save(path)


def xml(path):
    return ET.fromstring(gzip.decompress(Path(path).read_bytes()))


def value(root, path, val):
    node = root.find(path)
    if node is None:
        raise ValueError(f'Factory schema is missing {path}')
    node.set('Value', str(val))


def build_rack(pad_records, folder, final_folder):
    root = xml(CORE/'Defaults/Slicing/Default.adg')
    group = root.find('GroupDevicePreset')
    rack = group.find('Device/DrumGroupDevice')
    value(rack, 'UserName', 'Windowlight 16 Pads')
    value(rack, 'PadScrollPosition', 9)
    value(rack, 'AreMacroControlsVisible', 'false')
    value(rack, 'IsMidiSectionVisible', 'true')
    branches = group.find('BranchPresets')
    base = deepcopy(branches[0])
    branches.clear()
    group.find('ReturnBranchPresets').clear()
    part_template = xml(CORE/'Racks/Drum Racks/Drum Machines/808 Core Kit.adg').find('.//MultiSamplePart')
    for i, pad in enumerate(pad_records):
        branch = deepcopy(base)
        branch.set('Id', str(i))
        value(branch, 'Name', pad['name'])
        value(branch, 'DocumentColorIndex', 13 if i < 6 else 55)
        for key, val in [('ReceivingNote', pad['midi']), ('SendingNote', 60), ('ChokeGroup', pad['choke'])]:
            value(branch, 'ZoneSettings/'+key, val)
        simpler = branch.find('.//OriginalSimpler')
        for key, val in [('UserName',pad['name']), ('Filter/IsOn/Manual','false'),
                         ('VolumeAndPan/Volume/Manual', -6), ('VolumeAndPan/VolumeVelScale/Manual', .5),
                         ('VolumeAndPan/Envelope/AttackTime/Manual', 3),
                         ('VolumeAndPan/Envelope/ReleaseTime/Manual', 45)]:
            value(simpler, key, val)
        part = deepcopy(part_template)
        info = sf.info(folder/pad['file'])
        for key, val in [('Name',Path(pad['file']).name), ('SampleEnd',info.frames-1),
                         ('SustainLoop/End',info.frames-1), ('ReleaseLoop/End',info.frames-1),
                         ('SampleRef/DefaultDuration',info.frames), ('SampleRef/DefaultSampleRate',info.samplerate)]:
            value(part, key, val)
        ref = part.find('SampleRef/FileRef')
        for key, val in [('RelativePathType',3), ('RelativePath',pad['file']),
                         ('Path',str(final_folder/pad['file'])), ('LivePackName',''), ('LivePackId',''),
                         ('OriginalFileSize',(folder/pad['file']).stat().st_size), ('OriginalCrc',0)]:
            value(ref,key,val)
        simpler.find('Player/MultiSampleMap/SampleParts').append(part)
        branches.append(branch)
    # Remove stale provenance and factory author-machine paths, retaining actual sample refs.
    for e in root.iter():
        if e.tag in ('SourceContext','PresetRef'):
            e.clear()
        elif e.tag == 'LastPresetRef':
            e.clear()
            ET.SubElement(e,'Value')
    (folder/'Windowlight 16 Pads.adg').write_bytes(gzip.compress(ET.tostring(root, encoding='utf-8', xml_declaration=True),mtime=0))


def build_set(folder):
    """Use native lesson container shapes, replacing all notes, devices and media."""
    root = xml(CORE/'Defaults/Creating Tracks/MIDI Track/Default MIDI Track.als')
    lesson = xml(CORE/'Lessons/Sets/Drums Lesson.als')
    preset = xml(folder/'Windowlight 16 Pads.adg')
    rack = deepcopy(preset.find('.//DrumGroupDevice'))
    rack.set('Id','0')
    template = lesson.find('.//DrumGroupDevice/Branches/DrumBranch')
    for i, branch_preset in enumerate(preset.findall('GroupDevicePreset/BranchPresets/DrumBranchPreset')):
        branch = deepcopy(template)
        branch.set('Id',str(i))
        label = branch_preset.find('Name').get('Value')
        for key in ['EffectiveName','UserName']:
            value(branch,'Name/'+key,label)
        branch.find('SourceContext').clear()
        value(branch,'Color',13 if i<6 else 55)
        devices = branch.find('DeviceChain/MidiToAudioDeviceChain/Devices')
        devices.clear()
        devices.append(deepcopy(branch_preset.find('.//OriginalSimpler')))
        branch.find('DeviceChain/MidiToAudioDeviceChain/SignalModulations').clear()
        mixer = deepcopy(branch_preset.find('MixerPreset/AbletonDevicePreset/Device/AudioBranchMixerDevice'))
        mixer.tag='MixerDevice'
        mixer.attrib.clear()
        mixer_index=list(branch).index(branch.find('MixerDevice'))
        branch.remove(branch.find('MixerDevice'))
        branch.insert(mixer_index,mixer)
        for key in ['ReceivingNote','SendingNote','ChokeGroup']:
            value(branch,'BranchInfo/'+key,branch_preset.find('ZoneSettings/'+key).get('Value'))
        rack.find('Branches').append(branch)
    # Presets use unallocated target Id 0; no mappings are inherited from them.
    if rack.findall('.//PointeeId'):
        raise ValueError('Unexpected target reference in factory rack')
    next_id=max(int(e.get('Id')) for e in root.iter() if _global_id(e))+1
    for e in rack.iter():
        if _global_id(e):
            e.set('Id',str(next_id))
            next_id+=1
    live=root.find('LiveSet')
    track=live.find('Tracks/MidiTrack')
    value(track,'Name/EffectiveName','Windowlight 16 Pads')
    value(track,'Name/UserName','Windowlight 16 Pads')
    value(track,'DeviceChain/AudioOutputRouting/Target','AudioOut/Main')
    value(track,'DeviceChain/MainSequencer/MonitoringEnum',1)
    track.find('DeviceChain/DeviceChain/Devices').append(rack)
    events=track.find('DeviceChain/MainSequencer/ClipTimeable/ArrangerAutomation/Events')
    for i, exercise in enumerate(patterns()):
        clip=deepcopy(lesson.find('.//MidiClip'))
        clip.set('Id',str(i))
        clip.set('Time',str(i*32))
        for key,val in [('CurrentStart',i*32),('CurrentEnd',(i+1)*32),('Loop/LoopStart',0),
                        ('Loop/LoopEnd',32),('Loop/StartRelative',0),('Loop/LoopOn','true'),
                        ('Loop/OutMarker',32),('Loop/HiddenLoopStart',0),('Loop/HiddenLoopEnd',32),
                        ('Name',exercise['id']),('Disabled','false'),('VelocityAmount',0)]:
            value(clip,key,val)
        clip.find('Envelopes/Envelopes').clear()
        keys=clip.find('Notes/KeyTracks')
        keys.clear()
        clip.find('Notes/PerNoteEventStore/EventLists').clear()
        clip.find('Notes/NoteProbabilityGroups').clear()
        note_id=1
        for pad in pads():
            notes=[e for e in exercise['events'] if e['note']==pad['midi']]
            if not notes:
                continue
            key=ET.SubElement(keys,'KeyTrack',Id=str(pad['midi']-36))
            container=ET.SubElement(key,'Notes')
            for event in notes:
                ET.SubElement(container,'MidiNoteEvent',Time=str(event['beat']),
                              Duration=str(event['duration_beats']),Velocity=str(round(event['velocity']*127)),
                              OffVelocity='64',NoteId=str(note_id),IsEnabled='true',Probability='1')
                note_id+=1
            ET.SubElement(key,'MidiKey',Value=str(pad['midi']))
        value(clip,'Notes/NoteIdGenerator/NextId',note_id)
        events.append(clip)
    value(live,'NextPointeeId',next_id)
    value(live,'MainTrack/DeviceChain/Mixer/Tempo/Manual',BPM)
    value(live,'Transport/LoopLength',32)
    value(live,'Transport/LoopOn','true')
    value(live,'Transport/CurrentTime',0)
    value(live,'Annotation','Original Windowlight MIDI drum practice. 4 x 8 bars; local private kit.')
    ids=[e.get('Id') for e in root.iter() if _global_id(e)]
    if len(ids)!=len(set(ids)) or len(root.findall('.//SampleRef/FileRef'))!=16:
        raise ValueError('Invalid native rack targets or media count')
    (folder/'Windowlight Practice.als').write_bytes(gzip.compress(ET.tostring(root,encoding='utf-8',xml_declaration=True),mtime=0))


def render_preview(folder):
    """Audition the actual MIDI file with pad audio; this is not a Live render."""
    sr=44100
    sounds={p['midi']:sf.read(folder/p['file'],always_2d=True)[0] for p in pads()}
    groups={p['midi']:p['choke'] for p in pads()}
    result=np.zeros((round((128*60/BPM+1)*sr),2))
    messages=[]
    tick=0
    pending={}
    mid=MidiFile(folder/'Practice-32-bars.mid')
    for m in mid.tracks[0]:
        tick+=m.time
        if m.type=='note_on' and m.velocity:
            pending[m.note]=(tick,m.velocity)
        elif m.type in ('note_off','note_on') and m.note in pending:
            start,velocity=pending.pop(m.note)
            messages.append(dict(start=start,end=tick,note=m.note,velocity=velocity))
    messages.sort(key=lambda e:e['start'])
    for i,event in enumerate(messages):
        start=round(event['start']/mid.ticks_per_beat*60/BPM*sr)
        end=round(event['end']/mid.ticks_per_beat*60/BPM*sr)+round(.045*sr)
        group=groups[event['note']]
        next_choke=next((e for e in messages[i+1:] if e['note']==event['note'] or (group and groups[e['note']]==group)),None)
        if next_choke:
            end=min(end,round(next_choke['start']/mid.ticks_per_beat*60/BPM*sr))
        audio=sounds[event['note']][:max(0,end-start)].copy()
        fade=min(len(audio),round(.004*sr))
        if fade:
            audio[-fade:]*=np.linspace(1,0,fade)[:,None]
        result[start:start+len(audio)]+=audio*(event['velocity']/127)*10**(-6/20)
    peak=float(np.max(abs(result)))
    if not 1e-5<peak<1:
        raise ValueError(f'Preview is silent or clips: {peak}')
    sf.write(folder/'Practice-demo.wav',result,sr,subtype='PCM_24')
    return dict(peak_dbfs=float(20*np.log10(peak)),duration_seconds=len(result)/sr,
                rendering='actual exported MIDI + collected pads, approximate software sampler; not Live rerender')


def build():
    source = REPO/'beats/windowlight-v2'
    if hashlib.sha256((source/'full_mix.wav').read_bytes()).hexdigest() != PARENT_SHA:
        raise ValueError('Approved v2 reference changed')
    score = json.loads((source/'score.json').read_text())
    manifest = json.loads((source/'run_manifest.json').read_text())
    tracks = {t['id']:t for t in score['tracks']}
    source_records = {t['id']:t for t in manifest['tracks']}
    catalog = {t['id']:t for t in json.loads((Path(score['sample_root'])/'source_catalog.json').read_text())}
    out = REPO/'exports/windowlight-midi-practice'
    if out.exists():
        raise FileExistsError(out)
    records = pads()
    with tempfile.TemporaryDirectory(prefix='.drum-practice-',dir=out.parent) as temp:
        stage = Path(temp)/'project'
        for name in ['Samples','MIDI','Ableton Project Info']:
            (stage/name).mkdir(parents=True)
        for pad in records:
            track = tracks[pad['source_id']]
            original = Path(source_records[pad['source_id']]['source'])
            digest = hashlib.sha256(original.read_bytes()).hexdigest()
            if digest != source_records[pad['source_id']]['source_sha256']:
                raise ValueError(f'Sample source changed: {original}')
            audio = sample_voice(original,track['root_midi'],pad['pitch'], 1.2 if pad['midi']>=42 else .8)
            for freq, kind in [(track.get('highpass_hz',0),'highpass'),(track.get('lowpass_hz',0),'lowpass')]:
                if freq:
                    audio = sosfilt(butter(2,freq,btype=kind,fs=44100,output='sos'),audio,axis=0)
            audio *= 10**(track['gain_db']/20)*manifest['master_gain']
            pan = track.get('pan',0)
            audio[:,0] *= min(1,1-pan)
            audio[:,1] *= min(1,1+pan)
            if np.max(abs(audio))>=.98:
                raise ValueError(f'Pad gain would clip: {pad["name"]}')
            sf.write(stage/pad['file'], audio,44100,subtype='PCM_24')
            pad.update(sha256=hashlib.sha256((stage/pad['file']).read_bytes()).hexdigest(),
                       provenance=catalog[pad['source_id']], source_sha256=digest,
                       processing='repitched single note, click fades, source-track EQ/gain/pan; no room FX')
        build_rack(records,stage,out)
        exercises = patterns()
        all_events=[]
        for i, exercise in enumerate(exercises):
            write_midi(exercise['events'],32,stage/'MIDI'/(exercise['id']+'.mid'))
            all_events += [dict(e,beat=e['beat']+i*32) for e in exercise['events']]
        write_midi(all_events,128,stage/'Practice-32-bars.mid')
        build_set(stage)
        preview=render_preview(stage)
        result = dict(title='Windowlight MIDI Drum Practice',bpm=BPM,parent_mix_sha256=PARENT_SHA,
                      pads=records,patterns=exercises,license=score['license'],
                      intended_use='private local practice; not a sample product for redistribution',
                      preview=preview,live_validation='pending actual rack import and MIDI playback')
        (stage/'practice_manifest.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
        table='\n'.join(f'| {p["midi"]} | {p["name"]} | {p["choke"] or "无"} |' for p in records)
        (stage/'开始练习.md').write_text('# Windowlight 鼓机练习\n\n88 BPM，4/4。素材与原歌保留在 2TB。\n\n'
          '打开 Windowlight Practice.als。工程含 1 条 MIDI 轨、16 个采样器、4 段 MIDI 乐句。Drum Rack 把 MIDI 36–51 映射到 16 个鼓垫；力度控制轻重，音符长度控制持续时间。闭/开镲为互斥组 1；人声为互斥组 2。\n\n'
          '默认循环第 1–8 小节；将循环框移到第 9、17、25 小节可练后续段落，关闭循环可连续听四段。点轨道录音待命按钮可演奏鼓垫；录制前复制 clip 保留原乐句。\n\n'
          'MIDI 文件夹中每个文件都是完整 8 小节：基础鼓点 → Pocket 轻重与偏移 → 人声呼应 → 鼓花。Practice-32-bars.mid 按这个顺序串联。\n\n'
          '练法：先只改军鼓轻重；再改踩镲的后半拍；再移动一颗人声切片；最后给第 8 小节加鼓花。每次只改一个维度，复制 clip 保存前后版本。\n\n'
          '如需重新加载：把 Windowlight 16 Pads.adg 拖到 MIDI 轨，再将 MIDI 文件拖到该轨。整个文件夹一起移动。此包是可演奏练习音色，未承诺与 v2 音频工程逐样本相同。\n\n'
          '验证边界：程序已核验 MIDI、音源及工程结构；Live 内打开与播放仍需实际验证。Practice-demo.wav 来自导出的 MIDI 与同一组采样，由 Python 试听渲染器合成，并非 Live 导出。\n\n'
          '| MIDI 音符 | 鼓垫 | 互斥组 |\n|---|---|---|\n'+table+'\n\n来源逐项见 practice_manifest.json。仅供本地个人创作，不作为第三方采样包发布。\n')
        stage.rename(out)
    print(json.dumps(dict(out=str(out),pads=len(records),midi_files=5,events=len(all_events)),ensure_ascii=False))


if __name__ == '__main__':
    build()
