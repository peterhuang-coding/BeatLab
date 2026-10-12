"""Render the locally authorized, feedback-driven v2 revision collection.

For each of the ten frozen City Afterimages v1 drafts this module:

1. verifies the actual v1 parent (run id, full-mix bytes, score bytes) against
   the shipped v1 collection manifest and render evidence (read-only);
2. builds a revised score with the dedicated
   ``examples.city_feedback_scores.build_score`` builder (never the v1 builder),
   renders it into a brand-new ``beats/<new-id>`` song directory, and exports a
   brand-new ``Projects/<new-id>`` package with the same artifact kinds as v1;
3. writes a 20-track comparison ``collection.json`` (adjacent v1/v2 pairs) into
   the new collection directory, with per-track single linear-gain previews
   loudness-matched across all 20 entries.

The v1 library index, songs, exports, previews and feedback are never mutated.
Nothing is published, downloaded, rated, or claimed to be preferred by the
reviewer; every ``keep`` field stays ``null`` and no ratings are transferred.

CLI: ``python -m examples.render_city_feedback --media-root ROOT``
"""
from pathlib import Path
from copy import deepcopy
import argparse
import io
import json
import subprocess

OLD_COLLECTION = 'city-afterimages-20260927'
COLLECTION = 'city-afterimages-20261013-v2'
V1_SUFFIX = '-v1'
V2_SUFFIX = '-v2-20261013'
EXPECTED_NUMBERS = tuple(range(1, 11))
SAMPLE_RATE = 44100
MIN_DURATION_SECONDS = 45
MAX_DURATION_SECONDS = 62
LOUDNESS_TOLERANCE_DB = 0.15
LOUDNESS_FLOOR_LUFS = -20.0
# MP3 encoder delay/padding is allowed for in the resume decode check.
MP3_DURATION_TOLERANCE_SECONDS = 0.25
# Durable per-preview receipts (WAV/MP3 hashes + decode parameters), written
# next to the preview media so a completed run can be verified on resume.
PREVIEW_EVIDENCE_FILENAME = 'preview-evidence.json'


def revise_run_id(old_run_id):
    """Map a frozen v1 run id to its v2 revision id (``...-v1`` -> ``...-v2-20261013``)."""
    if not isinstance(old_run_id, str) or len(old_run_id) <= len(V1_SUFFIX):
        raise ValueError(f'run_id 必须是非空字符串: {old_run_id!r}')
    if not old_run_id.endswith(V1_SUFFIX):
        raise ValueError(f'父级 run_id 必须以 {V1_SUFFIX} 结尾: {old_run_id!r}')
    new_run_id = old_run_id[: -len(V1_SUFFIX)] + V2_SUFFIX
    if new_run_id == old_run_id:
        raise ValueError(f'v2 run_id 与 v1 相同: {old_run_id!r}')
    return new_run_id


def _read_json(path):
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f'缺少必需文件: {path}')
    return json.loads(path.read_text(encoding='utf-8'))


def verify_parent(root, prepared, old_track, old_evidence, sha256):
    """Verify the actual frozen v1 parent on disk before any revision work.

    Binds parent track id, full-mix SHA-256 and score SHA-256, checking real
    bytes against both the shipped collection manifest and render evidence.
    Raises ValueError on any mismatch; never writes anything.
    """
    root = Path(root)
    old_id = prepared['run_id']
    if old_track.get('id') != old_id:
        raise ValueError(f'v1 清单 id 与 prepared run_id 不一致: {old_track.get("id")!r} != {old_id!r}')
    if old_track.get('version') != 'v1':
        raise ValueError(f'{old_id}: 父级清单版本不是 v1: {old_track.get("version")!r}')
    if int(old_track['number']) != int(prepared['number']):
        raise ValueError(f'{old_id}: v1 曲号不一致')
    song = root / 'beats' / old_id
    project = root / 'exports' / OLD_COLLECTION / 'Projects' / old_id
    for rel in ('run_manifest.json', 'score.json', 'full_mix.wav'):
        if not (song / rel).is_file():
            raise FileNotFoundError(f'父级歌曲不完整，缺少 {song / rel}')
    delivery_path = project / 'delivery_manifest.json'
    if not delivery_path.is_file():
        raise FileNotFoundError(f'父级交付包不完整，缺少 {delivery_path}')
    if Path(old_track.get('editable_path', '')).resolve() != project.resolve():
        raise ValueError(f'{old_id}: v1 editable_path 与实际工程目录不一致')
    mix_sha = sha256(song / 'full_mix.wav')
    score_sha = sha256(song / 'score.json')
    if mix_sha != old_track['mix_sha256']:
        raise ValueError(
            f'{old_id}: 父级 full_mix.wav 字节与 v1 清单不符 '
            f'(实际 {mix_sha}, 清单 {old_track["mix_sha256"]})'
        )
    if mix_sha != old_evidence.get('mix_sha256'):
        raise ValueError(f'{old_id}: 父级 full_mix.wav 字节与 render-evidence 不符')
    if score_sha != old_evidence.get('score_sha256'):
        raise ValueError(f'{old_id}: 父级 score.json 字节与 render-evidence 不符')
    if Path(old_evidence.get('song', '')).resolve() != song.resolve():
        raise ValueError(f'{old_id}: render-evidence song 路径与实际父级目录不符')
    return {
        'run_id': old_id,
        'song': song,
        'project': project,
        'mix_sha256': mix_sha,
        'score_sha256': score_sha,
        'delivery_manifest': _read_json(delivery_path),
    }


def build_revised_score(prepared_clone, build_revised_score_fn):
    """Build the v2 score via the dedicated feedback builder; no v1 fallback."""
    score = build_revised_score_fn(prepared_clone)
    summary = (score.get('metadata') or {}).get('revision_summary')
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError('v2 score 缺少非空 metadata.revision_summary')
    # Same wiring as the v1 renderer.
    score['instrument_sources'] = deepcopy(prepared_clone['instrument_sources'])
    # Optional instrument tracks without notes are not delivered as silent stems.
    score['tracks'] = [t for t in score['tracks'] if t['events']]
    if not score['tracks']:
        raise ValueError('v2 score 没有任何含事件的音轨')
    return score, summary.strip()


def render_or_resume(score, song, render_score):
    """Render a new song, or resume only when the stored score is exactly equal."""
    song = Path(song)
    marker = song / 'run_manifest.json'
    if marker.is_file():
        score_path = song / 'score.json'
        if not score_path.is_file():
            raise FileNotFoundError(f'歌曲目录缺少 score.json，无法恢复: {song}')
        if _read_json(score_path) != score:
            raise ValueError(f'已存在的歌曲使用不同的 v2 总谱，请使用新的 run_id: {song}')
    elif song.exists():
        raise FileExistsError(f'不完整的歌曲目录已保留检查，未写入: {song}')
    else:
        render_score(score, song)
    if not marker.is_file():
        raise FileNotFoundError(f'render_score 未生成 run_manifest.json: {song}')
    if not (song / 'score.json').is_file():
        raise FileNotFoundError(f'render_score 未生成 score.json: {song}')
    if not (song / 'full_mix.wav').is_file():
        raise FileNotFoundError(f'render_score 未生成 full_mix.wav: {song}')
    return _read_json(marker)


def verify_completed_package(out, delivery, sha256):
    """Re-verify every recorded artifact hash/size before trusting a resume."""
    out = Path(out)
    if delivery.get('keep') is not None:
        raise ValueError(f'{out}: 交付清单 keep 必须为 null')
    parent = delivery.get('parent') or {}
    for key in ('collection', 'run_id', 'mix_sha256', 'score_sha256'):
        if key not in parent:
            raise ValueError(f'{out}: 交付清单缺少 parent.{key}')
    files = delivery.get('files')
    if not isinstance(files, list) or not files:
        raise ValueError(f'{out}: 交付清单缺少文件哈希记录')
    for record in files:
        path = out / record['path']
        if not path.is_file():
            raise FileNotFoundError(f'已完成交付包缺少文件: {path}')
        if path.stat().st_size != record['bytes']:
            raise ValueError(f'已完成交付包文件大小不一致: {path}')
        if sha256(path) != record['sha256']:
            raise ValueError(f'已完成交付包文件哈希不一致: {path}')
    return delivery


def export_package(song, out, clone, parent_run_id, qa, plan, helpers):
    """Export the same artifact kinds as the v1 renderer into a fresh package."""
    (collect_samples, write_phrase_midi, build_rack,
     build_listening, copy_sources, export_song, sha256) = helpers
    if out.exists():
        raise FileExistsError(f'不完整的交付包目录已保留检查，未写入: {out}')
    out.mkdir(parents=True)
    export_meta = export_song(song, out / 'AbletonProject', set_name=clone['title'])
    pads = collect_samples(plan, out / 'ChopRack')
    write_phrase_midi(plan, out / 'ChopRack/phrase-chops.mid')
    build_rack(pads, out / 'ChopRack', out / 'ChopRack',
               preset_name=clone['title'] + ' Chops')
    build_listening(song, plan, out / 'Listening')
    sources = copy_sources(plan, out / 'Sources')
    (out / 'source_map.json').write_text(
        json.dumps(plan, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (out / 'prepared.json').write_text(
        json.dumps(clone, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    readme = (
        f'# {clone["title"]}（v2 反馈修订版）\n\n'
        '本地反馈修订草稿（2026-10-13），依据此前对 v1 的反馈重新编曲；'
        '尚未用户 Keep 或发行验收，不代表评审已认可新版。\n\n'
        'AbletonProject 内 ALS 是与本 v2 混音一致的可编辑音频分轨；'
        'Source 内含 score 与 MIDI。ChopRack 含真实切片、原生鼓垫预设及整首切片触发 MIDI，'
        '可继续改切法。钢琴/Bass/鼓的 MIDI 没有自动在 ALS 内重建音源。'
        'Live 打开、发声、保存、重开与回渲染仍未验。\n\n'
        f'Sources/source_map 提供录音来源；Listening 可对照原片段→处理切片→成品。'
        f'本修订派生自 v1 run_id {parent_run_id}，'
        '原 v1 工程、导出与反馈均未改动。不是纯净人声分离，不声称已识别不同歌词。'
        '原厂乐器不作为独立素材包对外销售。\n'
    )
    (out / 'README.md').write_text(readme, encoding='utf-8')
    files = [
        dict(path=str(p.relative_to(out)), sha256=sha256(p), bytes=p.stat().st_size)
        for p in sorted(out.rglob('*')) if p.is_file()
    ]
    return files, sources, export_meta


def _read_stereo_full_mix(sf, np, path):
    y, sr = sf.read(path, dtype='float32', always_2d=True)
    if sr != SAMPLE_RATE:
        raise ValueError(f'{path}: 采样率应为 {SAMPLE_RATE}，实际 {sr}')
    if y.ndim != 2 or y.shape[1] != 2:
        raise ValueError(f'{path}: 必须为立体声')
    if not np.isfinite(y).all():
        raise ValueError(f'{path}: 含非有限采样')
    if float(np.max(np.abs(y))) >= 1.0:
        raise ValueError(f'{path}: 存在满幅/削波采样')
    seconds = len(y) / sr
    if not (MIN_DURATION_SECONDS <= seconds <= MAX_DURATION_SECONDS):
        raise ValueError(f'{path}: 时长 {seconds:.1f}s 超出 {MIN_DURATION_SECONDS}..{MAX_DURATION_SECONDS}s')
    return y, sr, seconds


def apply_linear_gain(np, y, gain_db):
    """Apply one scalar linear gain to every sample; attenuation only, no limiter."""
    return y * (10.0 ** (gain_db / 20.0))


def build_preview_wav_bytes(sf, np, source_path, gain_db):
    """Read a master, apply the single linear gain, encode PCM_24 WAV in memory.

    Encoding in memory first means resumes never overwrite an existing file.
    A zero gain is allowed to reproduce byte-identical PCM (a PCM_24 master
    re-encoded as PCM_24); callers must not reject an equal SHA in that case.
    """
    y, sr = sf.read(source_path, dtype='float32', always_2d=True)
    gained = apply_linear_gain(np, y, gain_db)
    data = _preview_wav_bytes(sf, gained, sr)
    return data, gained, sr


def _preview_wav_bytes(sf, gained, sr):
    """Encode the gained preview in-memory first, so resumes never overwrite."""
    buf = io.BytesIO()
    sf.write(buf, gained, sr, format='WAV', subtype='PCM_24')
    return buf.getvalue()


def _ensure_bytes_once(path, data, sha256, kind):
    """Create a file once; an existing file with different bytes blocks (no overwrite)."""
    if path.exists():
        if sha256(path) != sha256_bytes(data):
            raise ValueError(f'已有{kind}内容不一致，拒绝覆盖: {path}')
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def _ensure_preview_wav(path, data, sha256):
    _ensure_bytes_once(path, data, sha256, '预览音频')


def sha256_bytes(data):
    import hashlib
    return hashlib.sha256(data).hexdigest()


def load_preview_evidence(path):
    """Load durable preview receipts; a missing sidecar starts an empty run."""
    if not path.is_file():
        return {'version': 1, 'previews': {}}
    data = _read_json(path)
    if not isinstance(data.get('previews'), dict):
        raise ValueError(f'预览证据侧车损坏，缺少 previews 映射: {path}')
    return data


def decode_mp3(mp3):
    """Decode an MP3 to a WAV byte blob via ffmpeg; corruption raises ValueError."""
    try:
        proc = subprocess.run(
            ['ffmpeg', '-v', 'error', '-nostdin', '-i', str(mp3), '-f', 'wav', '-'],
            check=True, stdout=subprocess.PIPE, timeout=120)
    except subprocess.CalledProcessError as exc:
        raise ValueError(f'预览 MP3 无法解码（文件已损坏或不是音频）: {mp3}') from exc
    if not proc.stdout:
        raise ValueError(f'预览 MP3 解码结果为空: {mp3}')
    return proc.stdout


def verify_decoded_preview(sf, np, blob, *, frames, sample_rate, channels,
                           duration_seconds, where):
    """Actually decode preview audio and check it is finite, nonempty, in spec."""
    try:
        y, sr = sf.read(io.BytesIO(blob), dtype='float32', always_2d=True)
    except Exception as exc:
        raise ValueError(f'{where}: 预览音频无法解码: {exc}') from exc
    if y.size == 0:
        raise ValueError(f'{where}: 预览音频为空')
    if not np.isfinite(y).all():
        raise ValueError(f'{where}: 预览音频含非有限采样')
    if int(sr) != int(sample_rate):
        raise ValueError(
            f'{where}: 解码采样率 {sr} 与记录 {sample_rate} 不符')
    if y.ndim != 2 or int(y.shape[1]) != int(channels):
        raise ValueError(
            f'{where}: 解码声道与记录不符: shape={getattr(y, "shape", None)}')
    seconds = len(y) / sr
    if abs(seconds - float(duration_seconds)) > MP3_DURATION_TOLERANCE_SECONDS:
        raise ValueError(
            f'{where}: 解码时长 {seconds:.3f}s 与记录 {duration_seconds:.3f}s 不符')
    return seconds


def ensure_preview_mp3(mp3, wav, receipt, *, expected, sf, np, sha256):
    """Verify an existing MP3 against its durable receipt or encode a new one.

    Resume path: the MP3 must exist, its SHA-256 must equal the receipt, the
    WAV SHA in the receipt must match the in-memory preview, and the MP3 must
    actually decode to finite nonempty stereo audio of the recorded duration.
    Fresh path: the MP3 is encoded and immediately decoded/verified before a
    receipt is issued. Size is never trusted on its own.
    """
    wav_sha = sha256(wav)
    if receipt is not None and receipt.get('wav_sha256') != wav_sha:
        raise ValueError(f'{mp3}: 收据记录的 WAV 哈希与实际预览不符，拒绝继续')
    if mp3.exists():
        if receipt is None:
            raise ValueError(
                f'{mp3}: 已存在 MP3 但缺少持久预览收据，无法验证，拒绝重做')
        for key in ('frames', 'sample_rate', 'channels', 'duration_seconds'):
            if receipt.get(key) != expected[key]:
                raise ValueError(f'{mp3}: 收据 {key} 与当前总谱/混音不符')
        mp3_sha = sha256(mp3)
        if mp3_sha != receipt.get('mp3_sha256'):
            raise ValueError(f'{mp3}: 字节哈希与收据不符（文件被改动），拒绝继续')
        blob = decode_mp3(mp3)
        verify_decoded_preview(sf, np, blob, where=mp3, **expected)
        return mp3_sha, True
    if receipt is not None:
        raise FileNotFoundError(
            f'{mp3}: 预览收据存在但 MP3 缺失；拒绝在原路径上重新编码覆盖')
    subprocess.run(
        ['ffmpeg', '-v', 'error', '-nostdin', '-i', str(wav),
         '-codec:a', 'libmp3lame', '-b:a', '256k', str(mp3)],
        check=True, timeout=90)
    if not mp3.is_file() or mp3.stat().st_size == 0:
        raise RuntimeError(f'预览 MP3 生成失败: {mp3}')
    mp3_sha = sha256(mp3)
    blob = decode_mp3(mp3)
    verify_decoded_preview(sf, np, blob, where=mp3, **expected)
    return mp3_sha, False


def run(root):
    # --- Runtime imports: tests may provide sys.modules stubs, and the v1
    # renderer/export pipeline must never be imported at module import time. ---
    import numpy as np
    import soundfile as sf
    from examples.render_city_afterimages import plan_for, loudness
    from examples.real_record_delivery import (
        sha256, collect_samples, write_phrase_midi, build_rack,
        build_listening, copy_sources)
    # Dedicated revision builder. An ImportError here must surface: there is
    # deliberately no fallback to the v1 ten_beat_scores builder.
    from examples.city_feedback_scores import build_score as build_revised_score_fn
    from pipeline.song import render_score
    from pipeline.ableton_export import validate_song, export_song
    from examples.album_audition import load_manifest

    root = Path(root).resolve()
    index_path = root / 'library' / 'instruments' / OLD_COLLECTION / 'prepared-index.json'
    prepared_paths = _read_json(index_path)

    prepared_by_number = {}
    for raw_path in prepared_paths:
        payload = _read_json(raw_path)
        number = int(payload['number'])
        if number in EXPECTED_NUMBERS:
            if number in prepared_by_number:
                raise ValueError(f'prepared-index 中曲号 {number} 重复')
            prepared_by_number[number] = payload
    if set(prepared_by_number) != set(EXPECTED_NUMBERS):
        raise ValueError(
            f'prepared-index 必须恰好包含曲号 1..10，实际 {sorted(prepared_by_number)}')

    old_dir = root / 'exports' / OLD_COLLECTION
    old_manifest = _read_json(old_dir / 'collection.json')
    if old_manifest.get('id') != OLD_COLLECTION:
        raise ValueError(f'v1 collection.json id 不是 {OLD_COLLECTION}')
    old_tracks = {t['id']: t for t in old_manifest['tracks']}
    old_evidence = {
        Path(e['song']).name: e for e in _read_json(old_dir / 'render-evidence.json')
    }

    new_dir = root / 'exports' / COLLECTION
    helpers = (collect_samples, write_phrase_midi, build_rack,
               build_listening, copy_sources, export_song, sha256)
    units = []  # ordered: adjacent v1, v2 pairs

    # --- Pass 1: verify EVERY frozen parent before rendering anything. ------
    # A corrupted or missing parent — even for the last number — must fail
    # here with zero renders and zero new directories on disk.
    verified = {}
    for number in EXPECTED_NUMBERS:
        prepared = prepared_by_number[number]
        old_id = prepared['run_id']
        new_id = revise_run_id(old_id)
        old_track = old_tracks.get(old_id)
        old_ev = old_evidence.get(old_id)
        if old_track is None or old_ev is None:
            raise ValueError(f'v1 清单/证据中找不到 {old_id}')
        parent = verify_parent(root, prepared, old_track, old_ev, sha256)
        v1_mix = parent['song'] / 'full_mix.wav'
        _, _, v1_seconds = _read_stereo_full_mix(sf, np, v1_mix)
        v1_levels = loudness(v1_mix)
        verified[number] = dict(
            prepared=prepared, old_id=old_id, new_id=new_id,
            old_track=old_track, parent=parent,
            v1_mix=v1_mix, v1_seconds=v1_seconds, v1_levels=v1_levels)

    # --- Pass 2: all parents are good; only now may revision work start. ----
    for number in EXPECTED_NUMBERS:
        info = verified[number]
        prepared = info['prepared']
        old_id = info['old_id']
        new_id = info['new_id']
        old_track = info['old_track']
        parent = info['parent']

        # --- v1 side: read-only measurements of the frozen parent ----------
        units.append(dict(
            number=number, version='v1', run_id=old_id, prepared=prepared,
            song=parent['song'], project=parent['project'],
            mix_path=info['v1_mix'], mix_sha256=parent['mix_sha256'],
            score_sha256=parent['score_sha256'],
            levels=info['v1_levels'], duration_seconds=info['v1_seconds'],
            qa=parent['delivery_manifest'].get('qa'),
            artifacts=parent['delivery_manifest'].get('files'),
            old_track=old_track, parent_block=None, revision_summary=None,
        ))

        # --- v2 side: clone prepared payload, new id, independent dirs ------
        clone = deepcopy(prepared)
        clone['run_id'] = new_id
        score, summary = build_revised_score(clone, build_revised_score_fn)

        song = root / 'beats' / new_id
        out = new_dir / 'Projects' / new_id
        manifest = render_or_resume(score, song, render_score)
        qa = validate_song(song)
        plan = plan_for(score, manifest)

        delivery_path = out / 'delivery_manifest.json'
        if delivery_path.is_file():
            delivery = verify_completed_package(out, _read_json(delivery_path), sha256)
        else:
            files, sources, export_meta = export_package(
                song, out, clone, old_id, qa, plan, helpers)
            delivery = dict(
                song=str(song), qa=qa, files=files, sources=sources,
                export=export_meta, keep=None, publication='not published',
                revision='v2', revision_summary=summary,
                parent=dict(
                    collection=OLD_COLLECTION, run_id=old_id,
                    mix_sha256=parent['mix_sha256'],
                    score_sha256=parent['score_sha256'],
                ),
            )
            delivery_path.write_text(
                json.dumps(delivery, ensure_ascii=False, indent=2) + '\n',
                encoding='utf-8')
        expected_parent = dict(
            collection=OLD_COLLECTION, run_id=old_id,
            mix_sha256=parent['mix_sha256'],
            score_sha256=parent['score_sha256'])
        if delivery.get('parent') != expected_parent:
            raise ValueError(f'{out}: 已完成交付包的 parent 绑定与实际父级不符')

        v2_mix = song / 'full_mix.wav'
        y2, _, v2_seconds = _read_stereo_full_mix(sf, np, v2_mix)
        v2_levels = loudness(v2_mix)
        units.append(dict(
            number=number, version='v2', run_id=new_id, prepared=clone,
            song=song, project=out, mix_path=v2_mix,
            mix_sha256=sha256(v2_mix), score_sha256=sha256(song / 'score.json'),
            levels=v2_levels, duration_seconds=v2_seconds, qa=qa,
            artifacts=delivery.get('files'), old_track=None,
            parent_block=expected_parent, revision_summary=summary,
        ))
        print('v2 render/export', number, new_id, v2_levels, flush=True)

    # --- One linear gain per preview across ALL 20; never a limiter --------
    target = min(LOUDNESS_FLOOR_LUFS, min(u['levels']['lufs'] for u in units))
    audio_dir = new_dir / 'audio'
    audio_dir.mkdir(parents=True, exist_ok=True)
    sidecar_path = audio_dir / PREVIEW_EVIDENCE_FILENAME
    receipts = load_preview_evidence(sidecar_path)['previews']
    tracks = []

    for u in units:
        number = u['number']
        gain_db = target - u['levels']['lufs']
        if gain_db > 1e-9:
            raise ValueError(f"{u['run_id']}: 只允许衰减匹配，计算得到正增益 {gain_db}")
        wav = audio_dir / (u['run_id'] + '.wav')
        mp3 = wav.with_suffix('.mp3')
        # Encode the gained WAV in memory first; an existing differing WAV
        # blocks the run rather than being overwritten.
        data, gained, sr = build_preview_wav_bytes(sf, np, u['mix_path'], gain_db)
        _ensure_preview_wav(wav, data, sha256)
        expected = dict(
            frames=int(gained.shape[0]), sample_rate=int(sr),
            channels=int(gained.shape[1]),
            duration_seconds=float(gained.shape[0] / sr))
        receipt = receipts.get(u['run_id'])
        mp3_sha, resumed_mp3 = ensure_preview_mp3(
            mp3, wav, receipt, expected=expected, sf=sf, np=np, sha256=sha256)
        if not resumed_mp3:
            receipts[u['run_id']] = dict(
                wav=(Path('audio') / wav.name).as_posix(),
                mp3=(Path('audio') / mp3.name).as_posix(),
                wav_sha256=sha256(wav), mp3_sha256=mp3_sha, **expected)
        actual = loudness(wav)
        if abs(actual['lufs'] - target) > LOUDNESS_TOLERANCE_DB:
            raise ValueError(
                f"{u['run_id']}: 预览响度 {actual['lufs']} 与目标 {target} 偏差超限")
        preview_sha = sha256(wav)
        # NOTE: a zero attenuation gain can legitimately reproduce the master
        # bytes (PCM_24 -> PCM_24), so an equal SHA is accepted here.
        stored_score = _read_json(u['song'] / 'score.json')
        change_point = (
            stored_score['metadata']['middle_switch']['beat'] * 60.0
            / u['prepared']['bpm']
        )
        prepared = u['prepared']
        if u['version'] == 'v1':
            title = f'{prepared["title"]} · v1 原版'
            direction = 'v1 原版方向：' + u['old_track']['direction']
        else:
            title = f'{prepared["title"]} · v2 反馈修订版'
            direction = 'v2 修订：' + u['revision_summary']
        tracks.append(dict(
            id=u['run_id'], number=int(number), title=title, bpm=prepared['bpm'],
            duration_seconds=u['duration_seconds'], direction=direction,
            audio=(Path('audio') / mp3.name).as_posix(),
            original_audio=(Path('audio') / wav.name).as_posix(),
            source_summary=[
                dict(title=s['title'], source_url=s['source_url'])
                for s in prepared['source_records']
            ],
            editable_path=str(u['project']), version=u['version'],
            mix_sha256=u['mix_sha256'], change_point_seconds=change_point,
            listening_gain_db=gain_db, listening_lufs=actual['lufs'],
            matched_audio_sha256=preview_sha, keep=None,
        ))
        u.update(listening_gain_db=gain_db, listening_lufs=actual['lufs'],
                 matched_audio_sha256=preview_sha, mp3_sha256=mp3_sha,
                 true_peak_db=actual['true_peak_db'])

    assert len(tracks) == 20

    # Durable preview receipts: hash-guarded like the media itself, so a
    # changed/tampered sidecar blocks the run instead of being overwritten.
    sidecar_bytes = (
        json.dumps({'version': 1, 'previews': receipts},
                   ensure_ascii=False, indent=2, sort_keys=True) + '\n'
    ).encode('utf-8')
    _ensure_bytes_once(sidecar_path, sidecar_bytes, sha256, '预览证据侧车')
    data = dict(
        id=COLLECTION,
        title='城市余像 · 原版 v1 / 反馈修订 v2 对照试听',
        subtitle=(
            '相邻两首为同一曲号：v1 是 2026-09-27 冻结原版，'
            'v2 是 2026-10-13 按反馈制作的修订版。仅作本地 A/B 对照试听，'
            '不代表新版已被认可或已决定保留；请在新版页面重新留下反馈。'),
        tracks=tracks,
        purpose='original-vs-revision audition',
        loudness_target_lufs=target,
        keep=None,
    )
    (new_dir / 'collection.json').write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    # Self-check against the actual audition-server manifest contract.
    validated = load_manifest(new_dir)
    if len(validated['tracks']) != 20:
        raise ValueError('collection.json 未通过 20 曲校验')

    evidence = [
        dict(
            id=u['run_id'], number=u['number'], version=u['version'],
            song=str(u['song']), project=str(u['project']), qa=u['qa'],
            levels=dict(lufs=u['levels']['lufs'],
                        true_peak_db=u['levels']['true_peak_db']),
            preview=dict(
                listening_gain_db=u['listening_gain_db'],
                listening_lufs=u['listening_lufs'],
                true_peak_db=u['true_peak_db'],
                matched_audio_sha256=u['matched_audio_sha256'],
                mp3_sha256=u['mp3_sha256']),
            mix_sha256=u['mix_sha256'], score_sha256=u['score_sha256'],
            artifacts=u['artifacts'], parent=u['parent_block'],
            revision_summary=u['revision_summary'],
        )
        for u in units
    ]
    (new_dir / 'render-evidence.json').write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return new_dir


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--media-root', required=True, type=Path)
    print(run(parser.parse_args(argv).media_root))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
