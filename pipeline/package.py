"""Create a local release draft from a validated song; never publish it."""
from __future__ import annotations

import argparse
import hashlib
from html import escape
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import zipfile


def build_package(song: Path, out: Path) -> dict:
    from ableton_export import validate_song

    song, out = Path(song).resolve(), Path(out).resolve()
    if out.exists():
        raise FileExistsError(f'Choose a new delivery directory: {out}')
    check = validate_song(song)
    manifest = check['manifest']
    if not shutil.which('ffmpeg'):
        raise FileNotFoundError('ffmpeg is required for the MP3 preview')
    out.parent.mkdir(parents=True, exist_ok=True)
    # Complete in a sibling staging directory; failed encodes cannot look ready.
    with tempfile.TemporaryDirectory(prefix='.release-', dir=out.parent) as work:
        stage = Path(work)/'package'
        stage.mkdir()
        shutil.copy2(song/manifest['mix'], stage/'master.wav')
        subprocess.run(['ffmpeg','-nostdin','-v','error','-i',str(stage/'master.wav'),
                        '-codec:a','libmp3lame','-b:a','320k',str(stage/'preview.mp3')], check=True)
        with zipfile.ZipFile(stage/'trackouts.zip','w',zipfile.ZIP_DEFLATED) as archive:
            for track in manifest['tracks']:
                archive.write(song/track['file'], f"stems/{track['id']}.wav")
        (stage/'midi').mkdir()
        for track in manifest['tracks']:
            shutil.copy2(song/track['midi'],stage/'midi'/f"{track['id']}.mid")
        shutil.copy2(song/'score.json', stage/'score.json')
        shutil.copy2(song/'run_manifest.json', stage/'source_manifest.json')
        # Sources may be edited while MP3 encoding runs. Verify what is really
        # inside this delivery against the preflight snapshot before labeling it.
        expected_files = {'master.wav': check['mix_sha256'], 'score.json': check['score_sha256'],
                          'source_manifest.json': check['manifest_sha256']}
        expected_files.update({f"midi/{t['id']}.mid":t['midi_sha256'] for t in check['tracks']})
        for name, expected in expected_files.items():
            with (stage/name).open('rb') as stream:
                actual = hashlib.file_digest(stream,'sha256').hexdigest()
            if actual != expected:
                raise ValueError(f'Source changed during packaging: {name}')
        with zipfile.ZipFile(stage/'trackouts.zip') as archive:
            for track in check['tracks']:
                name = f"stems/{track['id']}.wav"
                with archive.open(name) as stream:
                    actual = hashlib.file_digest(stream,'sha256').hexdigest()
                if actual != track['sha256']:
                    raise ValueError(f'Source changed during packaging: {name}')
        (stage/'DJ.m3u8').write_text(
            '#EXTM3U\n'+f"#EXTINF:{round(check['duration_seconds'])},{manifest['title']}\n"+
            str(out/'master.wav')+'\n', encoding='utf-8')
        title = escape(manifest['title'])
        (stage/'cover-draft.svg').write_text(f'''<svg xmlns="http://www.w3.org/2000/svg" width="3000" height="3000" viewBox="0 0 1000 1000">
<rect width="1000" height="1000" fill="#202b28"/>
<rect x="140" y="110" width="720" height="570" rx="4" fill="#ceb994"/>
<path d="M140 395H860M500 110V680" stroke="#202b28" stroke-width="18"/>
<circle cx="685" cy="245" r="76" fill="#f2dec0"/>
<path d="M140 570L400 455L635 680H140Z" fill="#667467"/>
<text x="140" y="805" font-family="sans-serif" font-size="39" fill="#f2dec0">{title}</text>
<text x="140" y="862" font-family="sans-serif" font-size="18" letter-spacing="4" fill="#ceb994">{manifest['bpm']:g} BPM · INSTRUMENTAL · DRAFT</text>
</svg>\n''', encoding='utf-8')
        (stage/'README.txt').write_text(
            f"{manifest['title']} — local review draft\n\n"
            'master.wav is the unchanged listening mix. trackouts.zip contains the actual\n'
            'individual production tracks, all aligned at 0, with their mix gain baked in.\n'
            'MIDI files contain note patterns; they do not recreate instruments by themselves.\n'
            'DJ.m3u8 uses absolute paths to this delivery on the 2 TB drive.\n\n'
            'This is not a published product or a grant of a sample/beat license.\n'
            'Review the composition, source licenses, ownership, credits, artwork and platform\n'
            'requirements before publishing. Do not resell the underlying instrument samples.\n'
            'cover-draft.svg is a layout draft, not a verified platform upload format.\n', encoding='utf-8')
        metadata = {
            'title':manifest['title'],'bpm':manifest['bpm'], 'duration_seconds':check['duration_seconds'],
            'sample_rate':check['sample_rate'], 'publication_status':'draft','ready_to_publish':False,
            'user_acceptance':'pending','license':manifest.get('license',{}),
            'sections':manifest.get('sections',[]), 'track_granularity':[t['id'] for t in manifest['tracks']],
            'source_song':str(song), 'mix_sha256':check['mix_sha256'],
            'technical_validation':check['stem_sum'],
            'files':{str(p.relative_to(stage)):hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in sorted(stage.rglob('*')) if p.is_file()}}
        (stage/'metadata.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2)+'\n')
        # Refuse to merge into an output created concurrently.
        if out.exists():
            raise FileExistsError(out)
        stage.rename(out)
    return metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--song', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    metadata = build_package(args.song,args.out)
    print(json.dumps({'out':str(args.out.resolve()), 'status':metadata['publication_status']},ensure_ascii=False))


if __name__ == '__main__':
    main()
