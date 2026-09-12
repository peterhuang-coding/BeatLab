"""Original Windowlight score. Run from the repository root to write its JSON."""
import json
from pathlib import Path
import random

rng = random.Random(245)
tracks = {
    'rhodes': dict(name='Warm Rhodes', sample='Instrument/Piano & Keys/E-Piano Roads C3.aif', root_midi=60, gain_db=-13, highpass_hz=125, lowpass_hz=4800, room=.16, pan=-.08),
    'bass': dict(name='Electric Bass', sample='Instrument/Bass/Electric Bass C1.aif', root_midi=36, gain_db=-12, highpass_hz=32, lowpass_hz=1100),
    'piano': dict(name='Piano melody', sample='Instrument/Piano & Keys/Grand Piano C3 f.aif', root_midi=60, gain_db=-17, highpass_hz=220, lowpass_hz=5800, room=.25, pan=.1),
    'kick': dict(name='Kick', sample='Drums/Kick/Kick Tamuz 1.wav', root_midi=36, gain_db=-11, lowpass_hz=4600, drum=True),
    'snare': dict(name='Soft snare', sample='Drums/Snare/Snare Butta Soft.wav', root_midi=38, gain_db=-17, highpass_hz=150, lowpass_hz=5200, drum=True),
    'hat': dict(name='Closed hat', sample='Drums/Hihat/Hihat Closed Tamuz Tight.wav', root_midi=42, gain_db=-28, highpass_hz=2300, lowpass_hz=8200, pan=.15, drum=True),
}
for t in tracks.values():
    t['events'] = []

def hit(track, beat, note, duration, velocity):
    tracks[track]['events'].append(dict(beat=round(max(0, beat), 4), note=note,
                                       duration_beats=duration, velocity=round(velocity, 4)))

chords = {
    'F': (41, [57, 60, 64, 67]),
    'Em': (40, [55, 59, 62, 64]),
    'Dm': (38, [53, 57, 60, 64]),
    'G': (43, [53, 57, 59, 64]),
    'C': (36, [52, 55, 59, 62]),
    'Am': (33, [55, 60, 64, 67]),
}
sections = [
    ('Intro', ['F', 'F', 'Em', 'G']),
    ('Verse A', ['F', 'F', 'Em', 'Em', 'Dm', 'Dm', 'G', 'G', 'F', 'F', 'G', 'G']),
    ('Hook A', ['F', 'F', 'Em', 'Em', 'Dm', 'Dm', 'G', 'G']),
    ('Bridge', ['C', 'Am', 'Dm', 'G']),
    ('Verse B', ['F', 'F', 'Em', 'Em', 'Dm', 'Dm', 'G', 'G', 'F', 'F', 'G', 'G']),
    ('Hook B', ['F', 'F', 'Em', 'Em', 'Dm', 'Dm', 'G', 'G']),
    ('Outro', ['F', 'G', 'C', 'C']),
]
# A singable two-bar question/answer, with chord-specific resolutions.
melody = {
    'F': [(0.55, 72, .6), (1.55, 76, .6), (2.7, 74, .7), (4.55, 72, .8), (6.0, 69, 1.35)],
    'Em': [(0.55, 71, .6), (1.55, 74, .6), (2.7, 72, .7), (4.55, 71, .8), (6.0, 67, 1.35)],
    'Dm': [(0.55, 69, .6), (1.55, 72, .6), (2.7, 71, .7), (4.55, 69, .8), (6.0, 65, 1.35)],
    'G': [(0.55, 67, .6), (1.55, 69, .6), (2.7, 71, .7), (4.55, 74, .8), (6.0, 71, 1.35)],
}
bar = 0
markers = []
for section, progression in sections:
    start = bar
    markers.append(dict(name=section, start_bar=start, bars=len(progression)))
    for local, chord in enumerate(progression):
        at = bar * 4
        root, voicing = chords[chord]
        hook = section.startswith('Hook')
        intro = section == 'Intro'
        outro = section == 'Outro'
        bridge = section == 'Bridge'
        # Low velocity, open voicings and deliberate rests keep the middle clear.
        chord_level = .44 if hook else .38
        positions = [(0.02, 2.5, chord_level)]
        if not (intro or bridge or outro) and local % 2 == 1:
            positions.append((2.72, .95, chord_level*.70))
        if outro and local >= 2:
            positions = [(0.02, 6.8 if local == 2 else 3.7, .33)] if local == 2 else []
        for offset, length, velocity in positions:
            for order, note in enumerate(voicing):
                hit('rhodes', at+offset+order*.019, note, length, velocity+rng.uniform(-.025,.025))
        if not intro or local >= 2:
            bass_notes = [(0, root, 1.3, .77), (2.65, root, .72, .58)]
            if hook:
                bass_notes += [(1.8, root+12, .48, .48)]
            if bridge or outro:
                bass_notes = [(0, root, 2.7, .64)]
            if outro and local == 3:
                bass_notes = []
            for offset, note, length, velocity in bass_notes:
                hit('bass', at+offset+.012, note, length, velocity)
        if not intro and not bridge and not (outro and local >= 2):
            kicks = [(0,.86), (2.52,.70)]
            if local % 4 == 2:
                kicks.append((1.78,.57))
            if hook and local % 2 == 1:
                kicks.append((3.55,.60))
            for offset, velocity in kicks:
                hit('kick', at+offset, 36, .56, velocity)
            for offset in [1.025, 3.025]:
                hit('snare', at+offset, 38, .5, .71+rng.uniform(-.045,.045))
            if hook and local % 4 == 3:
                hit('snare', at+2.79, 38, .28, .23)
            hats = [0, .57, 1, 1.57, 2, 2.57, 3, 3.57] if hook else [0, 1, 1.57, 2, 3, 3.57]
            for i, offset in enumerate(hats):
                hit('hat', at+offset+rng.uniform(-.007,.007), 42, .18,
                    (.58 if offset == int(offset) else .35)+rng.uniform(-.045,.045))
        # Tease the theme late in each verse; hooks repeat it with breathing room.
        if chord in melody and local % 2 == 0 and (hook or (section.startswith('Verse') and local >= 8)):
            notes = melody[chord]
            if section.startswith('Verse'):
                notes = [notes[0], notes[2], notes[-1]]
            for offset, note, duration in notes:
                hit('piano', at+offset, note, duration, .62 if hook else .46)
            if section == 'Hook B' and local == 6:
                hit('piano', at+7.35, 72, .48, .40)
        if outro and local == 2:
            for offset, note, duration in [(0.6, 71,.5), (1.6,67,.6), (2.7,64,1.0), (4.0,60,3.5)]:
                hit('piano', at+offset, note, duration, .42)
        bar += 1

score = dict(title='窗边来信 · Windowlight', bpm=88, bars=bar, tail_seconds=2,
             fade_seconds=2, sections=markers, tracks=[dict(id=k, **v) for k,v in tracks.items()],
             license=dict(source='Ableton Live Core Library instrument and drum one-shots',
                          use='New original composition; private local project',
                          source_url='https://help.ableton.com/hc/en-us/articles/209768885-Commercial-Use-rights-for-Live-content',
                          commercial_status='needs_review',
                          restriction='Do not redistribute Core Library samples as a sample pack; verify user license before sale.'))
path = Path(__file__).with_name('windowlight.json')
path.write_text(json.dumps(score, ensure_ascii=False, indent=2)+'\n')
print(f'{bar} bars, {sum(len(t["events"]) for t in tracks.values())} notes -> {path}')
