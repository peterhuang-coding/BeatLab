"""只读的「素材来源与授权记录」渲染块，供 score song 审阅页嵌入。

``render_source_section(song, manifest)`` 输出一段可折叠的中文 HTML 片段。
安全边界：

* 只读取 ``song/source-provenance.json``，且该文件必须是 song 目录下的普通
  文件；遇到符号链接（可能指向目录外）一律跳过，不跟随。
* 不读取、不拷贝任何采样或音频文件，不访问数据库/网络，不写任何文件。
* 所有 SHA256 只作为「清单记录值」展示，本模块从不重新计算或声称已核验。
* 仅以 run_manifest 中实际出现的音轨为单位匹配；未与任何音轨建立唯一对应的
  记录不会被展示，也不会因为匹配失败就被断言为「未被使用」。
* run_manifest 的 license 只作为「已记录的制作/授权信息」展示，不授予任何
  作曲或采样使用权，且与单条采样的授权状态严格分开。
"""
from __future__ import annotations

import html
import json
import os
from pathlib import Path
import stat
from urllib.parse import urlsplit

PROVENANCE_FILE = 'source-provenance.json'
# 只有清单显式提供这些键时，才展示「来源归类」；绝不根据文件路径猜测。
_ORIGIN_KEYS = ('origin_type', 'source_kind', 'origin')


def _esc(value) -> str:
    """HTML-escape any value for both text and attribute contexts."""
    return html.escape('' if value is None else str(value), quote=True)


def _text(value):
    """Return a stripped non-empty string or None."""
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return None


def _basename_label(value):
    """只显示路径最后一段，避免泄露本机私有绝对路径。"""
    text = _text(value)
    if text is None:
        return None
    text = text.replace('\\', '/').rstrip('/')
    if '/' in text:
        text = text.rsplit('/', 1)[-1]
    return text or None


def _safe_http_url(value):
    """只允许无凭据的 http/https 链接成为 <a>；其余一律按文本处理。"""
    text = _text(value)
    if text is None:
        return None
    if any(ch in text for ch in ('\n', '\r', '\t', ' ', '<', '>', '"')):
        return None
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    if parts.scheme.lower() not in ('http', 'https'):
        return None
    if not parts.hostname or '@' in (parts.netloc or ''):
        return None
    if parts.username is not None or parts.password is not None:
        return None
    return text


def _load_provenance(song: Path):
    """读取并解析来源清单。返回 (rows|None, notes, warnings)，不跟随符号链接。"""
    notes: list[str] = []
    warnings: list[str] = []
    path = Path(song) / PROVENANCE_FILE
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        notes.append('未提供 source-provenance.json：以下仅显示 run_manifest 记录的音轨与哈希，'
                     '各采样的来源、URL 与授权状态均缺失。')
        return None, notes, warnings
    except OSError as exc:
        warnings.append(f'来源清单无法访问（{type(exc).__name__}），已跳过。')
        return None, notes, warnings
    if not stat.S_ISREG(info.st_mode):
        warnings.append('来源清单不是普通文件（可能是符号链接），已跳过；'
                        '不跟随链接读取 song 目录之外的内容。')
        return None, notes, warnings
    try:
        text = path.read_text(encoding='utf-8')
    except OSError as exc:
        warnings.append(f'来源清单读取失败（{type(exc).__name__}），已跳过。')
        return None, notes, warnings
    try:
        data = json.loads(text)
    except ValueError:
        warnings.append('来源清单不是合法 JSON，已跳过全部来源元数据。')
        return None, notes, warnings
    if not isinstance(data, list):
        kind = '字典' if isinstance(data, dict) else type(data).__name__
        warnings.append(f'来源清单顶层应为记录列表，实际为{kind}，已跳过全部来源元数据。')
        return None, notes, warnings
    return data, notes, warnings


def _index_rows(rows: list):
    """按 ID 与 SHA256 建索引；无法识别的坏行只计数，不影响其他行。"""
    by_id: dict[str, list[dict]] = {}
    by_hash: dict[str, list[dict]] = {}
    malformed = 0
    clean: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            malformed += 1
            continue
        rid = _text(row.get('id'))
        rsha = _text(row.get('sha256'))
        if rid is None and rsha is None:
            malformed += 1
            continue
        clean.append(row)
        if rid is not None:
            by_id.setdefault(rid, []).append(row)
        if rsha is not None:
            by_hash.setdefault(rsha.lower(), []).append(row)
    return by_id, by_hash, malformed, clean


def _row_label(row: dict) -> str:
    rid = _text(row.get('id'))
    return rid if rid is not None else '(无ID)'


def _match(track: dict, by_id, by_hash):
    """保守匹配，返回 (status, row|None, reason, candidates)。

    candidates 为存疑时涉及的清单记录：其内容一律不采用、不展示，但页脚需要
    据此把这些行标注为「未建立唯一对应」，而不是谎称它们未被使用。
    """
    tid = _text(track.get('id'))
    tsha = _text(track.get('source_sha256'))
    id_rows = by_id.get(tid, []) if tid is not None else []
    hash_rows = by_hash.get(tsha.lower(), []) if tsha is not None else []

    if not id_rows and not hash_rows:
        return ('missing', None,
                '未在来源清单中匹配到记录；该轨来源、URL 与授权状态缺失，需人工补充。', [])
    if len(id_rows) == 1 and len(hash_rows) == 1 and id_rows[0] is hash_rows[0]:
        return 'exact', id_rows[0], '', []
    if len(id_rows) > 1:
        return ('ambiguous', None,
                f'同一音轨 ID 在清单中存在 {len(id_rows)} 条记录，匹配不唯一，'
                '已不采用其中任何来源与授权信息。', id_rows)
    if len(hash_rows) > 1:
        candidates = '、'.join(sorted({_row_label(r) for r in hash_rows}))
        return ('ambiguous', None,
                f'记录的来源哈希同时命中 {len(hash_rows)} 条清单记录（候选 ID：{candidates}），'
                '匹配不唯一，已不采用其中任何来源与授权信息。', hash_rows)
    if len(id_rows) == 1 and len(hash_rows) == 1:
        return ('ambiguous', None,
                f'ID 与记录哈希分别命中不同清单记录（ID 命中「{_row_label(id_rows[0])}」，'
                f'哈希命中「{_row_label(hash_rows[0])}」），已不采用其中任何来源与授权信息。',
                [id_rows[0], hash_rows[0]])
    if len(id_rows) == 1:
        row = id_rows[0]
        rsha = _text(row.get('sha256'))
        if tsha is not None and rsha is not None:
            # ID 唯一命中，但双方都非空的记录哈希冲突：无法确认同一文件，
            # 既不算匹配成功，也不展示该记录的任何来源/授权信息。
            return ('ambiguous', None,
                    f'音轨 ID「{tid}」命中唯一清单记录，但双方记录的来源 SHA256 不一致，'
                    '无法确认同一文件；已不采用该记录的任何来源与授权信息。', [row])
        if tsha is None:
            if rsha is None:
                reason = '仅按音轨 ID 匹配；run_manifest 与清单均未记录 SHA256，无法核验文件身份。'
            else:
                reason = ('仅按音轨 ID 匹配；run_manifest 未记录本轨来源 SHA256，无法核验文件身份，'
                          '以下来源与授权为该清单记录的原值，未确认对应同一文件。')
        else:
            reason = '仅按音轨 ID 匹配；该清单记录缺少 SHA256，无法确认与本次使用的文件是否相同。'
        return 'id_only', row, reason, []
    tid_label = tid if tid is not None else '(无ID)'
    return ('hash_only', hash_rows[0],
            f'仅按记录的 SHA256 匹配；清单记录 ID「{_row_label(hash_rows[0])}」'
            f'与本轨 ID「{tid_label}」不一致，来源与授权按文件哈希对应，已如实标注。', [])


def _source_url_fragment(row: dict):
    """清单可另记录 source_url（素材出处页面）。只渲染安全的无凭据 http(s) 链接，
    与 license_source（授权/许可来源）分行；本模块绝不访问该 URL，也不读取音频。
    缺省不产生任何行；给了但不安全时只提示、不回显原值，避免泄漏路径或注入。"""
    if 'source_url' not in row:
        return None
    url = _safe_http_url(row.get('source_url'))
    if url is not None:
        return ('素材来源 URL（清单记录，本页未访问）：<a class="sp-link" href="' + _esc(url)
                + '" target="_blank" rel="noopener noreferrer">' + _esc(url) + '</a>')
    return '<span class="sp-miss">素材来源 URL：清单记录值不是安全的 http(s) 链接，已不予显示。</span>'


def _license_source_fragment(row: dict) -> str:
    raw = row.get('license_source')
    url = _safe_http_url(raw)
    if url is not None:
        return ('授权/许可来源：<a class="sp-link" href="' + _esc(url)
                + '" target="_blank" rel="noopener noreferrer">' + _esc(url) + '</a>')
    label = _basename_label(raw)
    if label is None:
        text = _text(raw)
        if text is None:
            return '<span class="sp-miss">授权/许可来源：清单未记录</span>'
        label = text
    return '授权/许可来源（非链接，原样显示）：' + _esc(label)


def _origin_fragment(row: dict) -> str:
    for key in _ORIGIN_KEYS:
        value = _text(row.get(key))
        if value is not None:
            return f'来源归类（清单字段 {key} 的原值，未核验）：{_esc(value)}'
    return '<span class="sp-muted">来源归类：清单未提供，不根据文件路径臆测（原创编排 / Ableton 库 / 历史录音需由清单字段支持）。</span>'


def _render_matched(row: dict, status: str, reason: str) -> str:
    bits: list[str] = []
    if status == 'exact':
        bits.append('<div class="sp-ok">匹配方式：音轨 ID 与记录的来源哈希均一致。</div>')
    else:
        bits.append('<div class="sp-warn">匹配方式：' + _esc(reason) + '</div>')
    rid = _text(row.get('id'))
    if rid is not None and status == 'hash_only':
        bits.append('<div class="sp-row">清单记录 ID：<code>' + _esc(rid) + '</code></div>')
    rsha = _text(row.get('sha256'))
    if rsha is not None:
        tail = '与本轨记录值一致；二者均为记录值，未重新校验。' if status == 'exact' else '清单记录值，未重新校验。'
        bits.append('<div class="sp-row">清单 SHA256：<code>' + _esc(rsha) + '</code>（' + tail + '）</div>')
    else:
        bits.append('<div class="sp-row"><span class="sp-miss">清单 SHA256：记录缺失</span></div>')
    origin = _basename_label(row.get('original_source'))
    if origin is not None:
        bits.append('<div class="sp-row">清单记录的原始来源（仅文件名）：<code>' + _esc(origin) + '</code></div>')
    else:
        bits.append('<div class="sp-row"><span class="sp-miss">原始来源：清单未记录文件名</span></div>')
    collected = _basename_label(row.get('file'))
    parent = _basename_label(row.get('parent_collected_file'))
    extras = []
    if collected is not None:
        extras.append('清单收集副本 <code>' + _esc(collected) + '</code>')
    if parent is not None:
        extras.append('父版本收集文件 <code>' + _esc(parent) + '</code>')
    if extras:
        bits.append('<div class="sp-row sp-muted">（仅文件名）' + '；'.join(extras) + '</div>')
    source_url = _source_url_fragment(row)
    if source_url is not None:
        bits.append('<div class="sp-row">' + source_url + '</div>')
    bits.append('<div class="sp-row">' + _license_source_fragment(row) + '</div>')
    status_value = _text(row.get('commercial_status'))
    if status_value is not None:
        bits.append('<div class="sp-row">商用状态（清单原值，未独立核验）：<code>'
                    + _esc(status_value) + '</code></div>')
    else:
        bits.append('<div class="sp-row"><span class="sp-miss">商用状态：清单未记录——不能据此认为可商用</span></div>')
    bits.append('<div class="sp-row">' + _origin_fragment(row) + '</div>')
    return ''.join(bits)


def _render_track(track: dict, by_id, by_hash):
    tid = _text(track.get('id'))
    name = _text(track.get('name')) or tid or '(未命名音轨)'
    used_file = _basename_label(track.get('source')) or '(run_manifest 未记录文件路径)'
    used_sha = _text(track.get('source_sha256'))
    status, row, reason, candidates = _match(track, by_id, by_hash)
    parts = ['<li><div class="sp-h"><span class="sp-name">', _esc(name), '</span>',
             '<code class="sp-id">', _esc(tid if tid is not None else '(无ID)'), '</code></div>',
             '<div class="sp-row">本轨使用素材（仅文件名）：<code>', _esc(used_file), '</code></div>',
             '<div class="sp-row">SHA256（run_manifest 记录值，本页不重新校验）：<code>',
             _esc(used_sha if used_sha is not None else '(未记录)'), '</code></div>']
    if status in ('exact', 'id_only', 'hash_only'):
        parts.append(_render_matched(row, status, reason))
    else:
        parts.append('<div class="sp-warn">' + _esc(reason) + '</div>')
    parts.append('</li>')
    return ''.join(parts), status, row, candidates


def _render_license_footer(manifest: dict) -> str:
    lic = manifest.get('license') if isinstance(manifest, dict) else None
    parts = ['<div class="sp-legal"><div class="sp-legal-t">制作与授权记录（来自 run_manifest '
             '的记录值，仅作记录，不构成任何授权）</div>']
    if isinstance(lic, dict) and lic:
        parts.append('<dl class="sp-dl">')
        for key in sorted(lic, key=str):
            value = lic[key]
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False, sort_keys=True)
            parts.append('<dt>' + _esc(key) + '</dt><dd>' + _esc(value) + '</dd>')
        parts.append('</dl>')
    elif isinstance(lic, str) and lic.strip():
        parts.append('<div>' + _esc(lic.strip()) + '</div>')
    else:
        parts.append('<div class="sp-muted">run_manifest 未记录制作/授权信息。</div>')
    parts.append('<p class="sp-warn">以上仅为 run_manifest 记录的制作与授权信息，'
                 '不授予作曲、编曲或任何采样的使用权利，'
                 '也不等于任何单个采样或历史录音的商用授权证明；'
                 '各轨以来源清单的记录为准，needs_review、unavailable 等原值表示仍需人工核实，'
                 '在核实完成前商业使用一律暂缓。</p></div>')
    return ''.join(parts)


_STYLE = (
    '.sp-src{border:1px solid #43464a;border-radius:8px;padding:12px 16px;margin:24px 0;'
    'background:#1f242b;color:#e8e5de;font-size:15px}'
    '.sp-src summary{cursor:pointer;color:#eac578;font-weight:600}'
    '.sp-src .sp-row{margin:4px 0;word-break:break-all}'
    '.sp-src .sp-h{display:flex;justify-content:space-between;gap:12px;margin-bottom:4px}'
    '.sp-src .sp-name{font-weight:600}.sp-src code{color:#d7e3c4;background:#2a2f37;'
    'padding:1px 5px;border-radius:4px;font-size:13px}'
    '.sp-src ol{padding-left:20px;margin:10px 0}.sp-src li{border-bottom:1px solid #34383f;padding:8px 0}'
    '.sp-src .sp-warn{color:#f0a96b}.sp-src .sp-ok{color:#9cc28a}'
    '.sp-src .sp-muted,.sp-src .sp-miss{color:#b8b6ae}'
    '.sp-src .sp-link{color:#eac578;word-break:break-all}'
    '.sp-src .sp-dl{margin:6px 0}.sp-src .sp-dl dt{color:#eac578;margin-top:4px}'
    '.sp-src .sp-dl dd{margin:0 0 4px 12px}'
)


def _render(song: Path, manifest: dict) -> str:
    rows, notes, warnings = _load_provenance(song)
    by_id, by_hash, malformed, clean = _index_rows(rows or [])
    if malformed:
        warnings.append(f'来源清单中有 {malformed} 条记录格式损坏（非字典或缺少 id/sha256），已跳过。')

    raw_tracks = manifest.get('tracks') if isinstance(manifest, dict) else None
    if not isinstance(raw_tracks, list):
        warnings.append('run_manifest 中 tracks 不是列表，无法列出已用音轨。')
        raw_tracks = []
    bad_tracks = sum(1 for item in raw_tracks if not isinstance(item, dict))
    if bad_tracks:
        warnings.append(f'run_manifest 中 {bad_tracks} 个音轨条目格式损坏，已跳过。')

    items = []
    matched = ambiguous = missing = 0
    used_rows: set[int] = set()
    ambiguous_rows: set[int] = set()
    for track in raw_tracks:
        if not isinstance(track, dict):
            continue
        fragment, status, row, candidates = _render_track(track, by_id, by_hash)
        items.append(fragment)
        if status in ('exact', 'id_only', 'hash_only'):
            matched += 1
            used_rows.add(id(row))
        elif status == 'ambiguous':
            ambiguous += 1
            ambiguous_rows.update(id(candidate) for candidate in candidates)
        else:
            missing += 1
    used_count = len(items)

    messages = ['<p class="sp-warn">' + _esc(msg) + '</p>' for msg in warnings]
    messages += ['<p class="sp-muted">' + _esc(msg) + '</p>' for msg in notes]

    catalog_note = ''
    if rows is not None:
        all_rows = {id(row) for row in clean}
        # 存疑候选 ≠ 未使用：匹配失败时不能断言这些行与本版本无关。
        unresolved_rows = ambiguous_rows - used_rows
        unused_rows = all_rows - used_rows - unresolved_rows
        parts = [f'来源清单共 {len(clean)} 条可用记录。']
        if unresolved_rows:
            parts.append(f'其中 {len(unresolved_rows)} 条与本版本音轨未建立唯一对应'
                         '（ID/哈希冲突或多重命中），不展示其来源与授权信息，'
                         '也不能据此断言其未被使用。')
        if unused_rows:
            parts.append(f'另有 {len(unused_rows)} 条未被任何音轨的 ID 或哈希引用，'
                         '未对应当前声部，不展示。')
        catalog_note = '<p class="sp-muted">' + _esc(''.join(parts)) + '</p>'

    return (
        '<details class="sp-src"><summary>素材来源与授权记录'
        f'（已用 {used_count} 轨 · 已匹配 {matched} · 存疑 {ambiguous} · 缺失 {missing}）</summary>'
        f'<style>{_STYLE}</style>'
        '<p class="sp-muted">所有 SHA256 均为各清单的记录值；本区块不重新哈希、'
        '不读取或核验音频文件，也不访问网络或数据库。</p>'
        + ''.join(messages)
        + ('<ol>' + ''.join(items) + '</ol>' if items else '<p class="sp-muted">本版本没有已用音轨记录。</p>')
        + catalog_note
        + _render_license_footer(manifest if isinstance(manifest, dict) else {})
        + '</details>'
    )


def render_source_section(song: Path, manifest: dict) -> str:
    """渲染来源区块。任何意外异常都退化为显式警告，不破坏整个审阅页。"""
    try:
        return _render(Path(song), manifest if isinstance(manifest, dict) else {})
    except Exception as exc:  # noqa: BLE001 - 页面韧性优先，且不回显可能含路径的异常文本
        return ('<details class="sp-src"><summary>素材来源与授权记录（渲染异常）</summary>'
                '<p class="sp-warn">来源区块渲染时出现未预期错误（'
                + _esc(type(exc).__name__) + '），已跳过；其余审阅内容不受影响。</p></details>')
