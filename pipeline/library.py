"""内容寻址资产库：转码/落盘为 library/<category>/<id>/source.wav（44.1k WAV）。

- id = 内容哈希 md5[:16]，同一内容只存一份（原始文件不动，connector 只读扫描）；
- 转码优先 ffmpeg CLI，缺省回退 librosa + soundfile；
- 大文件不进数据库，库目录布局与旧版一致。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import LIBRARY, STAGING  # noqa: E402


def ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


def convert_to_wav(src: Path, dst: Path) -> None:
    """统一转 44.1kHz WAV。优先 ffmpeg CLI，缺省回退 librosa + soundfile。"""
    ff = ffmpeg_path()
    if ff:
        subprocess.run(
            [ff, "-y", "-nostdin", "-loglevel", "error",
             "-i", str(src), "-ar", "44100", "-c:a", "pcm_s16le", str(dst)],
            check=True, capture_output=True,
        )
    else:
        import librosa
        import soundfile as sf
        y, _ = librosa.load(str(src), sr=44100, mono=False)
        sf.write(str(dst), y, 44100)


def probe_duration(path: Path) -> float | None:
    """时长粗查：ffprobe 优先，回退 soundfile；失败返回 None（不阻塞摄入）。"""
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        try:
            out = subprocess.run(
                [ffprobe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                check=True, capture_output=True, text=True, timeout=30,
            )
            return round(float(out.stdout.strip()), 3)
        except Exception:
            pass
    try:
        import soundfile as sf
        info = sf.info(str(path))
        return round(info.frames / info.samplerate, 3)
    except Exception:
        return None


def content_path(category: str, asset_id: str) -> Path:
    return LIBRARY / category / asset_id / "source.wav"


def store_asset(src: Path, category: str, asset_id: str,
                force: bool = False) -> tuple[Path, float]:
    """转码入库，返回 (final_path, duration_s)。已有完整文件且非 force 则直接复用。"""
    dest = LIBRARY / category / asset_id
    final = dest / "source.wav"
    if not force and final.exists() and final.stat().st_size > 0:
        return final, probe_duration(final) or 0.0
    dest.mkdir(parents=True, exist_ok=True)
    STAGING.mkdir(parents=True, exist_ok=True)
    tmp = STAGING / f"{asset_id}.wav"
    try:
        convert_to_wav(src, tmp)
        duration = probe_duration(tmp) or 0.0
        shutil.move(str(tmp), str(final))
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return final, duration


def write_meta(dest_dir: Path, meta: dict) -> None:
    """资产目录内写 meta.json（provenance 快照，供人工追溯；DB 为准）。"""
    (dest_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
