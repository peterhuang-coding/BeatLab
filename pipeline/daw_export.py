"""构建可搬移的 DAW 工程包。

当前环境可验证的是：素材自包含、事件与素材引用完整、MIDI/Recipe/Provenance
齐全。Ableton `.als` 的真实时间线写入必须由经过 Live 12 验证的导入器完成；在
MBP 实际打开、保存和回放前，验证状态始终保持为 false。
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

import arrangement as arrangement_model


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def _asset_source(asset: dict, candidate_dir: Path) -> Path | None:
    role = asset.get("role")
    if role == "processed_audio":
        path = candidate_dir / str(asset.get("source_path") or "")
        return path if path.is_file() else None
    if role == "reference_mix":
        path = candidate_dir / "full_mix.wav"
        return path if path.is_file() else None
    if role in ("source_audio", "drum_sample"):
        return arrangement_model.resolve_source_path(str(asset.get("source_path") or ""))
    return None


def _write_handoff(path: Path, arrangement: dict) -> None:
    verification = arrangement["verification"]
    text = f"""# BeatLab Ableton 交付说明

候选：{arrangement['run_id']} / {arrangement['candidate_id']}

BPM：{arrangement['bpm']}

拍号：{arrangement['time_signature']['numerator']}/{arrangement['time_signature']['denominator']}

## 当前可用内容

- `arrangement.json`：唯一编排事实源，包含所有 Audio Clip、Drum/Bass MIDI 事件、段落与稳定 ID。
- `Samples/Original/`：实际引用的原素材或 stem。
- `Samples/Processed/`：已完成反向、滤波和拉伸等处理的独立音频剪辑。
- `Samples/DrumKit/`：本候选实际使用的 one-shot。
- `MIDI/`：鼓、Bass 与采样触发 MIDI。
- `reference/full_mix.wav`：默认应静音，仅用于 A/B 对照。
- `reference/premaster_mix.wav`：未做 master 限幅的试听参考。

## Ableton 导入约束

工程包已经可以搬移且不依赖 BeatLab 原目录。当前版本尚未把事件写入真实 Live 12
`.als` 时间线；需要在 MBP 上通过经过验证的 Live 12 导入器创建轨道、Clip、Drum Rack
和 Bass 音源。只有实际打开、保存并完整回放后，才能把 `daw_opened` 与
`daw_playback_verified` 标记为 true。

当前验证状态：

```json
{json.dumps(verification, ensure_ascii=False, indent=2, sort_keys=True)}
```
"""
    path.write_text(text, encoding="utf-8")


def _publish_atomically(staging: Path, target: Path) -> None:
    backup = target.with_name(f".{target.name}.previous")
    if backup.exists():
        shutil.rmtree(backup)
    if target.exists():
        target.rename(backup)
    try:
        os.replace(staging, target)
    except Exception:
        if backup.exists() and not target.exists():
            backup.rename(target)
        raise
    if backup.exists():
        shutil.rmtree(backup)


def build_project_package(
    candidate_dir: Path,
    arrangement: dict,
    *,
    recipe_path: Path,
    provenance_path: Path,
    spec_path: Path,
) -> tuple[dict, Path]:
    """构建 `<candidate>/project/` 并返回带最终验证状态的 arrangement。"""
    candidate_dir = candidate_dir.resolve()
    updated = copy.deepcopy(arrangement)
    verification = updated.setdefault("verification", {})
    verification.update({
        "audio_rendered": (candidate_dir / "full_mix.wav").is_file(),
        "package_built": False,
        "structure_validated": False,
        "als_built": False,
        "daw_opened": False,
        "daw_playback_verified": False,
    })

    staging = Path(tempfile.mkdtemp(prefix=".project-", dir=candidate_dir))
    target = candidate_dir / "project"
    try:
        missing_assets = []
        for asset in updated.get("assets", []):
            package_path = asset.get("package_path")
            if not package_path:
                continue
            source = _asset_source(asset, candidate_dir)
            if source is None:
                missing_assets.append(str(asset.get("asset_id")))
                continue
            destination = staging / str(package_path)
            _copy_file(source, destination)
            asset["sha256"] = _sha256(destination)
            asset["exists"] = True

        midi_sources = {
            "drums.mid": candidate_dir / "midi" / "drums.mid",
            "bass.mid": candidate_dir / "midi" / "bass.mid",
            "chops.mid": candidate_dir / "midi" / "chops.mid",
        }
        for name, source in midi_sources.items():
            if source.is_file():
                _copy_file(source, staging / "MIDI" / name)

        for source, name in (
            (recipe_path, "recipe.json"),
            (provenance_path, "provenance.json"),
            (spec_path, "spec.json"),
        ):
            if source.is_file():
                _copy_file(source, staging / name)

        premaster = candidate_dir / "premaster_mix.wav"
        if premaster.is_file():
            _copy_file(premaster, staging / "reference" / "premaster_mix.wav")

        verification["package_built"] = not missing_assets
        package_arrangement = copy.deepcopy(updated)
        for asset in package_arrangement.get("assets", []):
            if asset.get("package_path"):
                asset["source_path"] = asset["package_path"]
        (staging / "arrangement.json").write_text(
            arrangement_model.dumps(package_arrangement), encoding="utf-8"
        )
        errors = arrangement_model.validate(package_arrangement, package_root=staging)
        if missing_assets:
            errors.extend(f"缺少素材 asset_id={asset_id}" for asset_id in missing_assets)
        verification["structure_validated"] = not errors
        package_arrangement["verification"] = dict(verification)
        (staging / "arrangement.json").write_text(
            arrangement_model.dumps(package_arrangement), encoding="utf-8"
        )
        _write_handoff(staging / "ABLETON_HANDOFF.md", package_arrangement)

        files = []
        for path in sorted(p for p in staging.rglob("*") if p.is_file()):
            files.append({
                "path": path.relative_to(staging).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": _sha256(path),
            })
        manifest = {
            "schema_version": "1.0.0",
            "run_id": updated.get("run_id"),
            "candidate_id": updated.get("candidate_id"),
            "arrangement_id": updated.get("arrangement_id"),
            "portable": not errors,
            "validation_errors": errors,
            "verification": verification,
            "files": files,
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if errors:
            raise ValueError("工程包结构校验失败：" + "；".join(errors))
        integrity_errors = verify_project_package(staging)
        if integrity_errors:
            raise ValueError("Package integrity failed: " + "; ".join(integrity_errors))
        _publish_atomically(staging, target)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    return updated, target


def verify_project_package(project_dir: Path) -> list[str]:
    """Verify a relocated package independently of its recorded success flags."""
    import numpy as np
    import soundfile as sf
    from mido import MidiFile
    root = Path(project_dir)
    errors = []
    if root.is_symlink() or not root.is_dir():
        return ['Invalid package directory']

    def safe(name):
        if not isinstance(name, str) or not name or "\x00" in name:
            raise ValueError('Invalid package path')
        rel = Path(name)
        if rel.is_absolute() or '..' in rel.parts or rel.as_posix() != name:
            raise ValueError(f'Unsafe package path: {name}')
        path = root / rel
        if any(root.joinpath(*rel.parts[:i]).is_symlink() for i in range(1, len(rel.parts)+1)):
            raise ValueError(f'Symlink in package path: {name}')
        if not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
            raise ValueError(f'Missing or escaping file: {name}')
        return path

    def read_json(name):
        value = json.loads(safe(name).read_text(encoding='utf-8'))
        if not isinstance(value, dict):
            raise ValueError(f'Expected JSON object: {name}')
        return value

    try:
        manifest = read_json('manifest.json')
        arrangement = read_json('arrangement.json')
        required = {'arrangement.json', 'recipe.json', 'provenance.json', 'spec.json',
                    'reference/full_mix.wav', 'reference/premaster_mix.wav', 'ABLETON_HANDOFF.md'}
        for name in ('recipe.json','provenance.json','spec.json'):
            read_json(name)
        for asset in arrangement.get('assets', []):
            if asset.get('package_path'):
                required.add(asset['package_path'])
        for track in arrangement.get('tracks', []):
            if track.get('midi_file'):
                required.add(track['midi_file'])
        for name in required:
            safe(name)
        files = manifest.get('files')
        if not isinstance(files, list) or not files:
            raise ValueError('Missing manifest file list')
        seen = set()
        for entry in files:
            name = entry['path']
            path = safe(name)
            if name in seen or name == 'manifest.json':
                raise ValueError(f'Duplicate/recursive manifest entry: {name}')
            seen.add(name)
            if entry.get('bytes') != path.stat().st_size or entry.get('sha256') != _sha256(path):
                errors.append(f'Size/SHA256 mismatch: {name}')
            if path.suffix.lower() in ('.wav','.aif','.aiff','.flac','.ogg','.mp3'):
                try:
                    with sf.SoundFile(path) as stream:
                        count = 0
                        for block in stream.blocks(blocksize=65536, dtype='float32'):
                            count += len(block)
                            if not np.isfinite(block).all():
                                raise ValueError('Nonfinite audio')
                        if not count or count != stream.frames:
                            raise ValueError('Empty or incomplete decode')
                except (OSError, RuntimeError, ValueError) as exc:
                    errors.append(f'Audio decode failed: {name}: {exc}')
            elif path.suffix.lower() == '.mid':
                MidiFile(path)
        if not required.issubset(seen):
            errors.append(f'Required files absent from manifest: {sorted(required-seen)}')
        actual = set()
        for path in root.rglob('*'):
            if path.is_symlink():
                errors.append(f'Symlink in package: {path.relative_to(root)}')
            elif path.is_file() and path.name != 'manifest.json':
                actual.add(path.relative_to(root).as_posix())
        if actual != seen:
            errors.append('Manifest inventory differs from package files')
        errors.extend(arrangement_model.validate(arrangement, package_root=root))
    except (OSError, ValueError, TypeError, KeyError, AttributeError, EOFError) as exc:
        errors.append(f'Package invalid: {exc}')
    return errors
