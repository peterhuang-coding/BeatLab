"""Explicit gain feedback -> immutable, validated song child; no taste inference."""
from copy import deepcopy
from math import isfinite


def revise_score(score, gains_db):
    if not isinstance(gains_db, dict) or not gains_db:
        raise ValueError("gains_db must be a nonempty mapping")

    tracks = score.get("tracks")
    if not isinstance(tracks, list) or not tracks:
        raise ValueError("score has no tracks")

    known = set()
    for track in tracks:
        if not isinstance(track, dict) or "id" not in track:
            raise ValueError("each track must have an id")
        if track["id"] in known:
            raise ValueError("duplicate track id")
        known.add(track["id"])

    unknown = [track_id for track_id in gains_db if track_id not in known]
    if unknown:
        raise ValueError(f"unknown track ids: {unknown}")

    normalized = {}
    for track_id, delta in gains_db.items():
        if isinstance(delta, bool) or not isinstance(delta, (int, float)):
            raise ValueError("gain deltas must be finite numbers")
        delta = float(delta)
        if not isfinite(delta):
            raise ValueError("gain deltas must be finite")
        if not (-60.0 <= delta <= 6.0):
            raise ValueError("gain deltas must be within [-60, 6] dB")
        normalized[track_id] = delta

    if all(delta == 0.0 for delta in normalized.values()):
        raise ValueError("at least one gain delta must be nonzero")

    revised = deepcopy(score)
    for track in revised["tracks"]:
        if track["id"] in normalized:
            current = track.get("gain_db", -12.0)
            if isinstance(current, bool) or not isinstance(current, (int, float)):
                raise ValueError("track gain_db must be numeric")
            if not isfinite(current):
                raise ValueError("track gain must be finite")
            track["gain_db"] = float(current) + normalized[track["id"]]

    return revised


def revise_song(parent, gains_db):
    import hashlib, json, os, shutil
    from pathlib import Path
    from song import render_score
    from ableton_export import validate_song
    parent = Path(parent).resolve()
    checked = validate_song(parent)
    score = json.loads((parent/'score.json').read_text())
    updated = revise_score(score, gains_db)
    records = {t['id']:t for t in checked['manifest']['tracks']}
    if {t['id'] for t in updated['tracks']} != set(records):
        raise ValueError('Score and rendered manifest track sets differ')
    changes = {key:float(value) for key,value in sorted(gains_db.items()) if value != 0}
    token = hashlib.sha256(json.dumps(dict(manifest=checked['manifest_sha256'],
        score=checked['score_sha256'], mix=checked['mix_sha256'], changes=changes),sort_keys=True).encode()).hexdigest()[:12]
    child = parent.with_name(parent.name+'-r'+token)
    if child.exists():
        validate_song(child)
        saved=json.loads((child/'revision.json').read_text())
        if saved['request_id'] != token: raise ValueError('Revision identity mismatch')
        return dict(song=str(child),request_id=token,reused=True)
    # Check original rendering sources before reserving a child version.
    from song import CORE
    root = Path(score.get('sample_root', CORE)).expanduser()
    for track in updated['tracks']:
        source = (root/track['sample']).resolve()
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        if digest != records[track['id']]['source_sha256']:
            raise ValueError(f'Source changed: {track["id"]}')
        track['sample'] = str(source)
    updated['fixed_master_gain'] = checked['manifest']['master_gain']
    staging = child.with_name('.'+child.name+'.pending')
    try: staging.mkdir()
    except FileExistsError: raise ValueError('Revision is running or interrupted; inspect pending version')
    state = staging/'state.json'
    state.write_text(json.dumps(dict(status='running',parent=str(parent),changes=changes)))
    output = staging/'render'
    try:
        render_score(updated, output, sr=checked['sample_rate'])
        valid = validate_song(output)
        if valid['mix_sha256'] == checked['mix_sha256']:
            raise ValueError('Requested change produced identical audio')
        (output/'revision.json').write_text(json.dumps(dict(parent=str(parent),request_id=token,
            parent_mix_sha256=checked['mix_sha256'],gains_db=changes,status='rendered',
            subjective_acceptance='pending',master_gain_policy='frozen parent gain'),indent=2)+'\n')
        for name in ('source-provenance.json','source_catalog.json','slice-map.json'):
            if (parent/name).is_file(): shutil.copy2(parent/name,output/name)
        os.rename(output,child)
        shutil.rmtree(staging)
    except Exception as exc:
        state.write_text(json.dumps(dict(status='failed',error=str(exc),parent=str(parent),changes=changes)))
        raise
    return dict(song=str(child),request_id=token,reused=False)


def main():
    import argparse,json
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--song',required=True)
    parser.add_argument('--gains',required=True,help='JSON mapping: track id to dB change')
    args=parser.parse_args()
    print(json.dumps(revise_song(args.song,json.loads(args.gains))))

if __name__=='__main__': main()
