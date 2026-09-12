"""Build the user-requested vocal revision from the preserved Windowlight v1."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import random
import shutil

REPO = Path(__file__).resolve().parents[1]
CORE = Path('/Applications/Ableton Live 12 Suite.app/Contents/App-Resources/Core Library/Samples/One Shots')
PARENT_SHA = '43f56d6f28fdf6c97ddecafa71e2ebad78055e44bcc150f1a6f980fe59e230fe'


def build():
    parent = REPO/'beats/windowlight-v1'
    if hashlib.sha256((parent/'full_mix.wav').read_bytes()).hexdigest() != PARENT_SHA:
        raise ValueError('The approved parent audio changed; do not revise an unidentified version')
    score = deepcopy(json.loads((parent/'score.collected.json').read_text()))
    sources = {t['id']:Path(score['sample_root'])/t['sample'] for t in score['tracks']}
    tracks = {t['id']:t for t in score['tracks']}
    additions = {
        'vocal_oh': dict(name='Vocal Oh chops', sample='Vocal/Vocal BNYX Oh Ab.aif', root_midi=56,
                         gain_db=-15.5, highpass_hz=125, lowpass_hz=5800, room=.24, pan=-.08),
        'vocal_ah': dict(name='Vocal Ah responses', sample='Vocal/Vocal BNYX Ah B.aif', root_midi=59,
                         gain_db=-18.5, highpass_hz=145, lowpass_hz=5500, room=.30, pan=.16),
        'choir': dict(name='Wordless harmony', sample='Vocal/Vocal Choir Pure C4.wav', root_midi=72,
                      gain_db=-27, highpass_hz=230, lowpass_hz=4200, room=.32, pan=.08),
        'shaker': dict(name='Shaker pocket', sample='Drums/Shaker/Shaker Tamuz.wav', root_midi=70,
                       gain_db=-31, highpass_hz=1900, lowpass_hz=7600, pan=-.26, drum=True),
        'rim': dict(name='Rim syncopation', sample='Drums/Rim/Rim Sidestick Tamuz 1.wav', root_midi=37,
                    gain_db=-24, highpass_hz=260, lowpass_hz=5200, pan=.2, drum=True),
        'open_hat': dict(name='Open hat transitions', sample='Drums/Hihat/Hihat Open Tamuz.wav', root_midi=46,
                         gain_db=-30, highpass_hz=2300, lowpass_hz=7400, pan=.12, drum=True),
    }
    for tid, definition in additions.items():
        sources[tid] = CORE/definition['sample']
        tracks[tid] = dict(id=tid, **definition, events=[])
    rng = random.Random(2452)

    def note(tid, beat, pitch, duration, velocity):
        tracks[tid]['events'].append(dict(beat=round(beat,4), note=pitch,
                                          duration_beats=duration, velocity=round(velocity,4)))

    # The new vocal owns the first half of each hook phrase; the existing
    # piano keeps the answer. Both melodic phrases do not play continuously.
    piano = []
    for event in tracks['piano']['events']:
        event = dict(event)
        hook_start = next((start for start in (64,160) if start <= event['beat'] < start+32), None)
        if hook_start is not None:
            if (event['beat']-hook_start) % 8 < 4:
                continue
            event['velocity'] *= .88
        piano.append(event)
    tracks['piano']['events'] = piano
    for event in tracks['rhodes']['events']:
        if 64 <= event['beat'] < 96 or 160 <= event['beat'] < 192:
            event['velocity'] *= .90

    motifs = [[60,60,57,55], [59,62,59,55], [57,60,57,53], [59,62,60,59]]
    answers = [64,62,60,62]
    for hook_bar in (16,40):
        second = hook_bar == 40
        for phrase, pitches in enumerate(motifs):
            at = hook_bar*4 + phrase*8
            for offset,pitch,duration,velocity in zip([.15,.74,1.65,2.38],pitches,[.42,.70,.52,.95],[.67,.78,.66,.76]):
                note('vocal_oh',at+offset,pitch,duration,velocity)
            note('vocal_ah',at+7.48,answers[phrase],.46,.49 if second else .40)
            if second:
                note('vocal_ah',at+3.50,pitches[-1]+12,.36,.38)
                if phrase % 2 == 1:
                    note('vocal_oh',at+7.05,pitches[0],.25,.37)
        if second:
            for bar,pitches in [(44,[65,69]),(46,[67,71])]:
                for pitch in pitches:
                    note('choir',bar*4+.04,pitch,3.1,.34)

    # Verse ad-libs introduce the texture without replacing the piano theme.
    for start in (4,28):
        for local,pitches in [(3,[59,55]),(7,[62,59]),(11,[59,60])]:
            at = (start+local)*4
            note('vocal_oh',at+2.28,pitches[0],.52,.43)
            note('vocal_ah',at+3.30,pitches[1],.58,.39)
    note('vocal_oh',10.56,59,.68,.38)
    note('vocal_ah',15.36,62,.48,.32)
    for bar,pitches in [(24,[64,67]),(25,[64,69]),(26,[65,69]),(27,[62,67])]:
        for pitch in pitches:
            note('choir',bar*4+.04,pitch,2.9,.45)
    note('vocal_oh',24*4+2.5,60,1.15,.54)
    note('vocal_ah',26*4+2.5,57,1.05,.46)
    note('vocal_oh',48*4+.3,60,.85,.48)
    note('vocal_ah',49*4+2.55,59,.7,.40)
    for pitch in [64,67]:
        note('choir',50*4+.10,pitch,6.5,.40)

    # Small rhythmic differences across sections; fills lead into a new part.
    for section in score['sections']:
        start, count = section['start_bar'],section['bars']
        if not section['name'].startswith(('Verse','Hook')):
            continue
        hook = section['name'].startswith('Hook')
        later = section['name'] in ('Verse B','Hook B')
        for local in range(count):
            at = (start+local)*4
            if hook or later:
                for off in ([.57,1.07,1.57,2.57,3.07,3.57] if hook else [.57,1.57,2.57,3.57]):
                    note('shaker',at+off+rng.uniform(-.008,.008),70,.21,.45+rng.uniform(-.08,.08))
            if local % 2 == 1:
                note('rim',at+1.78,37,.23,.38)
                if hook or later:
                    note('rim',at+3.78,37,.23,.30)
            if hook and local % 2 == 1:
                note('open_hat',at+2.56,46,.44,.48)
            if local % 4 == 3:
                for off,vel in [(2.79,.23),(3.55,.33),(3.80,.26)]:
                    note('snare',at+off,38,.23,vel)
    for bar,approach in [(15,40),(23,35),(27,40),(39,40),(47,40)]:
        note('bass',bar*4+3.64,approach,.25,.44)
        note('kick',bar*4+3.65,36,.30,.42)
    for track in tracks.values():
        track['events'].sort(key=lambda e:(e['beat'],e['note']))

    collected = REPO/'library/instruments/windowlight-v2'
    collected.mkdir(parents=True,exist_ok=True)
    catalog = []
    for tid,track in tracks.items():
        source = sources[tid]
        destination = collected/(tid+source.suffix)
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        if destination.exists():
            if hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
                raise ValueError(f'Existing collected sample changed: {destination}')
        else:
            shutil.copy2(source,destination)
        if hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
            raise ValueError(f'Copy verification failed: {destination}')
        track['sample'] = destination.name
        catalog.append(dict(id=tid,file=str(destination),original_source=str(source),sha256=digest,
                            license_source=score['license']['source_url'],commercial_status='needs_review'))
    score.update(title='窗边来信 · Windowlight — Vocal Cut',sample_root=str(collected),tracks=list(tracks.values()),
                 revision=dict(parent_run='windowlight-v1',parent_mix_sha256=PARENT_SHA,
                               feedback='很好，还需要一些人声采样啥的 复杂点',
                               changes=['wordless vocal hook and responses','piano answers vocal phrases',
                                        'bridge and outro vocal harmony','shaker/rim/open-hat and transition fills']))
    (collected/'source_catalog.json').write_text(json.dumps(catalog,ensure_ascii=False,indent=2)+'\n')
    destination = REPO/'examples/windowlight-vocal.json'
    destination.write_text(json.dumps(score,ensure_ascii=False,indent=2)+'\n')
    print(f'{len(tracks)} tracks, {sum(len(t["events"]) for t in tracks.values())} events -> {destination}')


if __name__ == '__main__':
    build()
