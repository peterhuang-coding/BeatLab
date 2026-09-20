"""render_source_section 的合成夹具测试：只读、保守匹配、防泄漏、韧性。"""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pipeline'))
from song_provenance import PROVENANCE_FILE, render_source_section

PRIVATE = '/Users/someone/secret-studio/'
SHARED_LIB = '/Applications/Ableton Live 12 Suite.app/Contents/Core Library/Samples/'


def make_track(tid, sha, name=None, source=None):
    return {
        'id': tid,
        'name': name if name is not None else f'轨道 {tid}',
        'file': f'stems/{tid}.wav',
        'midi': f'midi/{tid}.mid',
        'source': source if source is not None else PRIVATE + f'{tid}.wav',
        'source_sha256': sha,
        'sha256': 'stem-' + sha[:8] if sha else 'stem-missing',
    }


def make_row(rid, rsha, *, file=None, original=None, license_source=None,
             commercial='needs_review', parent=None, **extra):
    row = {
        'id': rid,
        'file': file if file is not None else f'/library/instruments/run/{rid}.wav',
        'original_source': original if original is not None else SHARED_LIB + f'{rid}.aif',
        'sha256': rsha,
        'license_source': license_source if license_source is not None else 'https://example.org/license',
        'commercial_status': commercial,
    }
    if parent is not None:
        row['parent_collected_file'] = parent
    row.update(extra)
    return row


def make_manifest(tracks, license_=None):
    return {
        'title': '测试歌曲', 'bpm': 90, 'bars': 4, 'duration_seconds': 8.0,
        'tracks': tracks,
        'license': license_ if license_ is not None else {},
    }


class ProvenanceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.song = Path(self._tmp.name) / 'run-1'
        self.song.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def write_provenance(self, data, raw=None):
        target = self.song / PROVENANCE_FILE
        if raw is not None:
            target.write_text(raw, encoding='utf-8')
        else:
            target.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        return target

    def render(self, tracks=None, manifest=None):
        manifest = manifest or make_manifest(tracks or [])
        return render_source_section(self.song, manifest)

    # ---- 缺失 / 畸形 ----------------------------------------------------

    def test_missing_catalog_is_graceful(self):
        out = self.render([make_track('kick', 'a' * 64)])
        self.assertIn('素材来源与授权记录', out)
        self.assertIn('未提供 source-provenance.json', out)
        self.assertIn('kick.wav', out)            # 只显示文件名
        self.assertIn('a' * 64, out)              # 记录值哈希仍显示
        self.assertIn('未在来源清单中匹配到记录', out)
        self.assertNotIn(PRIVATE, out)

    def test_malformed_json_warns_but_renders(self):
        self.write_provenance(None, raw='{not valid json,,,')
        out = self.render([make_track('kick', 'a' * 64)])
        self.assertIn('不是合法 JSON', out)
        self.assertIn('kick', out)
        self.assertIn('未在来源清单中匹配到记录', out)

    def test_top_level_dict_is_rejected(self):
        self.write_provenance({'kick': make_row('kick', 'a' * 64)})
        out = self.render([make_track('kick', 'a' * 64)])
        self.assertIn('顶层应为记录列表', out)
        self.assertIn('未在来源清单中匹配到记录', out)
        self.assertNotIn('https://example.org/license', out)

    def test_malformed_list_entries_are_skipped(self):
        good = make_row('kick', 'a' * 64)
        self.write_provenance([123, 'str', None, {'id': ''}, {'sha256': '   '}, good])
        out = self.render([make_track('kick', 'a' * 64), make_track('bass', 'b' * 64)])
        self.assertIn('5 条记录格式损坏', out)
        self.assertIn('ID 与记录的来源哈希均一致', out)
        self.assertIn('未在来源清单中匹配到记录', out)  # bass 仍缺失

    def test_malformed_manifest_tracks(self):
        out = render_source_section(self.song, {'title': 'x', 'tracks': {'id': 1}})
        self.assertIn('tracks 不是列表', out)
        self.assertIn('本版本没有已用音轨记录', out)
        out2 = render_source_section(self.song, ['not', 'a', 'dict'])
        self.assertIn('素材来源与授权记录', out2)

    # ---- 匹配 -----------------------------------------------------------

    def test_exact_match_full_fields(self):
        row = make_row('kick', 'a' * 64, commercial='needs_review',
                       parent='/library/old/kick.wav')
        self.write_provenance([row])
        out = self.render([make_track('kick', 'a' * 64)])
        self.assertIn('已用 1 轨 · 已匹配 1 · 存疑 0 · 缺失 0', out)
        self.assertIn('ID 与记录的来源哈希均一致', out)
        self.assertIn('kick.aif', out)                  # original_source 仅 basename
        self.assertIn('父版本收集文件', out)
        self.assertIn('kick.wav', out)
        self.assertIn('needs_review', out)
        self.assertIn('href="https://example.org/license"', out)
        self.assertIn('不重新校验', out)
        self.assertNotIn(SHARED_LIB, out)
        self.assertNotIn(PRIVATE, out)
        self.assertNotIn('/library/instruments/run/kick.wav', out)

    def test_only_used_rows_are_shown(self):
        used = make_row('kick', 'a' * 64, license_source='https://used.example/ok')
        unused = make_row('mystery', 'f' * 64, license_source='https://unused.example/x',
                          commercial='public_domain')
        self.write_provenance([used, unused])
        out = self.render([make_track('kick', 'a' * 64),
                           make_track('phrase_0_full', '9' * 64)])
        self.assertIn('https://used.example/ok', out)
        self.assertNotIn('https://unused.example/x', out)
        self.assertNotIn('public_domain', out)
        self.assertNotIn('mystery', out)
        # mystery 行没有被任何音轨的 ID 或哈希引用，才可如实标注为未使用
        self.assertIn('未被任何音轨的 ID 或哈希引用', out)
        self.assertIn('未对应当前声部', out)
        self.assertNotIn('未建立唯一对应', out)
        # phrase_* 在真实 demo 中无清单记录：必须诚实显示缺失
        self.assertIn('phrase_0_full', out)

    def test_conflicting_recorded_hashes_is_ambiguous(self):
        # 回归：清单 voice=a*64(allowed)，音轨 voice 的 source_sha256=b*64。
        # ID 相同但双方非空哈希冲突时必须存疑：不算匹配，不展示任何来源/授权。
        row = make_row('voice', 'a' * 64, commercial='allowed',
                       license_source='https://deal.example/voice',
                       source_url='https://src.example/voice.wav')
        self.write_provenance([row])
        out = self.render([make_track('voice', 'b' * 64)])
        self.assertIn('已用 1 轨 · 已匹配 0 · 存疑 1 · 缺失 0', out)
        self.assertIn('双方记录的来源 SHA256 不一致', out)
        self.assertIn('已不采用该记录的任何来源与授权信息', out)
        self.assertNotIn('https://deal.example/voice', out)
        self.assertNotIn('https://src.example/voice.wav', out)
        self.assertNotIn('>allowed<', out)
        self.assertNotIn('商用状态（清单原值', out)
        # 该清单行是存疑候选，不得被称为「未被使用」
        self.assertIn('1 条与本版本音轨未建立唯一对应', out)
        self.assertNotIn('未对应当前声部', out)

    def test_missing_track_hash_is_unverified_id_only(self):
        # 音轨缺少 source_sha256：只是未核验的 ID 匹配，不是哈希冲突。
        self.write_provenance([make_row('voice', 'a' * 64, commercial='allowed')])
        out = self.render([make_track('voice', None)])
        self.assertIn('已用 1 轨 · 已匹配 1 · 存疑 0 · 缺失 0', out)
        self.assertIn('仅按音轨 ID 匹配', out)
        self.assertIn('run_manifest 未记录本轨来源 SHA256', out)
        self.assertIn('(未记录)', out)
        self.assertIn('allowed', out)  # 记录原值仍展示，但明确未核验文件身份

    def test_id_only_match_warns_on_missing_row_hash(self):
        row = make_row('kick', 'c' * 64)
        del row['sha256']
        self.write_provenance([row])
        out = self.render([make_track('kick', 'a' * 64)])
        self.assertIn('该清单记录缺少 SHA256', out)

    def test_hash_only_match_shows_id_difference(self):
        row = make_row('renamed_in_catalog', 'a' * 64)
        self.write_provenance([row])
        out = self.render([make_track('kick', 'a' * 64)])
        self.assertIn('仅按记录的 SHA256 匹配', out)
        self.assertIn('renamed_in_catalog', out)

    def test_ambiguous_duplicate_hash_adopts_nothing(self):
        rows = [make_row('kick', 'a' * 64, license_source='https://one.example/a',
                         commercial='cleared'),
                make_row('alt_kick', 'a' * 64, license_source='https://two.example/b',
                         commercial='needs_review')]
        self.write_provenance(rows)
        out = self.render([make_track('kick', 'a' * 64)])
        self.assertIn('存疑 1', out)
        self.assertIn('匹配不唯一', out)
        self.assertIn('alt_kick', out)
        self.assertNotIn('https://one.example/a', out)
        self.assertNotIn('https://two.example/b', out)
        self.assertNotIn('>cleared<', out)

    def test_id_and_hash_point_to_different_rows(self):
        rows = [make_row('kick', 'c' * 64, license_source='https://by-id.example/'),
                make_row('other', 'a' * 64, license_source='https://by-hash.example/')]
        self.write_provenance(rows)
        out = self.render([make_track('kick', 'a' * 64)])
        self.assertIn('分别命中不同清单记录', out)
        self.assertNotIn('https://by-id.example/', out)
        self.assertNotIn('https://by-hash.example/', out)

    def test_duplicate_id_rows_are_ambiguous(self):
        self.write_provenance([make_row('kick', 'a' * 64),
                               make_row('kick', 'a' * 64, license_source='https://dup.example/')])
        out = self.render([make_track('kick', 'a' * 64)])
        self.assertIn('同一音轨 ID 在清单中存在 2 条记录', out)
        self.assertNotIn('https://dup.example/', out)

    def test_ambiguous_catalog_rows_are_not_called_unused(self):
        # 存疑候选行（含哈希冲突/多重命中）必须标注为「未建立唯一对应」，
        # 不能因为匹配失败就宣称它们未对应当前声部。
        rows = [make_row('kick', 'a' * 64, license_source='https://one.example/a'),
                make_row('alt_kick', 'a' * 64, license_source='https://two.example/b')]
        self.write_provenance(rows)
        out = self.render([make_track('kick', 'a' * 64)])
        self.assertIn('2 条与本版本音轨未建立唯一对应', out)
        self.assertIn('不能据此断言其未被使用', out)
        self.assertNotIn('未对应当前声部', out)

    # ---- 转义 / URL 安全 ------------------------------------------------

    def test_html_escaping_in_names_and_metadata(self):
        payload = '<script>alert(1)</script>'
        row = make_row('kick', 'a' * 64, license_source=payload,
                       original='/secret/<img src=x onerror=alert(1)>.aif')
        self.write_provenance([row])
        out = self.render([make_track('kick', 'a' * 64, name=payload)])
        self.assertNotIn('<script>alert(1)</script>', out)
        self.assertNotIn('<img', out)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', out)
        self.assertIn('&lt;img', out)

    def test_malicious_urls_never_become_links(self):
        bad = [
            'javascript:alert(1)',
            'JavaScript:alert(1)',
            '  javascript:alert(1)',
            'data:text/html,<script>alert(1)</script>',
            'http://user:pass@evil.example/x',
            'https://user@evil.example/x',
            'file:///etc/passwd',
            '//evil.example/path',
            'ftp://evil.example/x',
            'https://evil.example/x\n<script>',
        ]
        rows = [make_row(f't{i}', f'{i:064x}', license_source=value)
                for i, value in enumerate(bad)]
        self.write_provenance(rows)
        tracks = [make_track(f't{i}', f'{i:064x}') for i in range(len(bad))]
        out = self.render(tracks)
        lowered = out.lower()
        for needle in ('href="javascript:', 'href="data:', 'href="file:',
                       'href="//', 'href="ftp:', 'user:pass@', 'user@evil'):
            self.assertNotIn(needle, lowered, needle)
        # 合法 http/https 仍是链接
        self.write_provenance([make_row('safe', '1' * 64,
                                        license_source='https://creativecommons.org/licenses/by/4.0/')])
        ok = self.render([make_track('safe', '1' * 64)])
        self.assertIn('href="https://creativecommons.org/licenses/by/4.0/"', ok)

    def test_filesystem_path_license_source_is_not_linked(self):
        self.write_provenance([make_row('kick', 'a' * 64,
                                        license_source='/Volumes/Secret/private-deal.pdf')])
        out = self.render([make_track('kick', 'a' * 64)])
        self.assertNotIn('href=', out)
        self.assertNotIn('/Volumes/Secret', out)
        self.assertIn('private-deal.pdf', out)

    def test_safe_source_url_shown_separately_from_license_source(self):
        row = make_row('kick', 'a' * 64, source_url='https://src.example/kick.wav')
        self.write_provenance([row])
        out = self.render([make_track('kick', 'a' * 64)])
        self.assertIn('素材来源 URL（清单记录，本页未访问）', out)
        self.assertIn('href="https://src.example/kick.wav"', out)
        # 与授权/许可来源分行、各自独立
        self.assertIn('href="https://example.org/license"', out)
        self.assertIn('授权/许可来源', out)
        self.assertLess(out.index('https://src.example/kick.wav'),
                        out.index('https://example.org/license'))

    def test_unsafe_source_url_never_shown_or_echoed(self):
        bad = [
            'javascript:alert(1)',
            'file:///Volumes/Secret/secret-source-a.wav',
            'https://evil.example/x\n<script>',
            '/secret/path/secret-source-b.wav',
        ]
        rows = [make_row(f'k{i}', f'{i + 1:064x}', source_url=value)
                for i, value in enumerate(bad)]
        self.write_provenance(rows)
        tracks = [make_track(f'k{i}', f'{i + 1:064x}') for i in range(len(bad))]
        out = self.render(tracks)
        self.assertNotIn('secret-source', out)
        self.assertNotIn('javascript:alert', out.lower())
        self.assertNotIn('<script>', out.lower())
        self.assertIn('不是安全的 http(s) 链接，已不予显示', out)

    def test_source_url_absent_adds_no_line(self):
        self.write_provenance([make_row('kick', 'a' * 64)])
        out = self.render([make_track('kick', 'a' * 64)])
        self.assertNotIn('素材来源 URL', out)

    # ---- 符号链接 -------------------------------------------------------

    def test_external_symlink_is_not_followed(self):
        outside = Path(self._tmp.name) / 'outside'
        outside.mkdir()
        secret = make_row('kick', 'a' * 64, license_source='https://symlink-secret.example/')
        (outside / PROVENANCE_FILE).write_text(
            json.dumps([secret], ensure_ascii=False), encoding='utf-8')
        os.symlink(outside / PROVENANCE_FILE, self.song / PROVENANCE_FILE)
        out = self.render([make_track('kick', 'a' * 64)])
        self.assertIn('不是普通文件', out)
        self.assertNotIn('symlink-secret.example', out)
        self.assertIn('未在来源清单中匹配到记录', out)

    # ---- 授权区分 -------------------------------------------------------

    def test_overall_license_does_not_grant_track_clearance(self):
        row = make_row('kick', 'a' * 64, commercial='needs_review')
        self.write_provenance([row])
        manifest = make_manifest(
            [make_track('kick', 'a' * 64)],
            license_={'status': 'released for private listening',
                      'composition': 'original note score'})
        out = render_source_section(self.song, manifest)
        self.assertIn('制作与授权记录', out)
        self.assertIn('仅作记录，不构成任何授权', out)
        self.assertIn('不授予作曲、编曲或任何采样的使用权利', out)
        self.assertIn('released for private listening', out)
        self.assertIn('不等于任何单个采样或历史录音的商用授权证明', out)
        self.assertIn('needs_review', out)
        self.assertIn('商业使用一律暂缓', out)

    def test_missing_commercial_status_is_explicit(self):
        row = make_row('kick', 'a' * 64)
        del row['commercial_status']
        self.write_provenance([row])
        out = self.render([make_track('kick', 'a' * 64)])
        self.assertIn('商用状态：清单未记录', out)
        self.assertIn('不能据此认为可商用', out)

    def test_unavailable_status_shown_raw(self):
        self.write_provenance([make_row('kick', 'a' * 64, commercial='unavailable')])
        out = self.render([make_track('kick', 'a' * 64)])
        self.assertIn('unavailable', out)

    def test_origin_distinction_only_when_supplied(self):
        able_row = make_row('vocal_oh', 'a' * 64)  # 路径含 Ableton，但无归类字段
        hist = make_row('blues_excerpt', 'b' * 64, origin_type='historical_recording',
                        license_source='https://citizen-dj.labs.loc.gov/credit')
        self.write_provenance([able_row, hist])
        out = self.render([make_track('vocal_oh', 'a' * 64),
                           make_track('blues_excerpt', 'b' * 64)])
        self.assertIn('不根据文件路径臆测', out)
        self.assertIn('historical_recording', out)
        self.assertIn('https://citizen-dj.labs.loc.gov/credit', out)

    # ---- 无文件系统读取 / 确定性 / 无脚本 -------------------------------

    def test_no_source_filesystem_reads(self):
        # 音轨指向不存在的路径：渲染绝不能去 stat/open 采样文件。
        self.write_provenance([make_row('kick', 'a' * 64)])
        opened = []
        real_open = Path.open

        def spy_open(self, *args, **kwargs):
            opened.append(str(self))
            return real_open(self, *args, **kwargs)

        tracks = [make_track('kick', 'a' * 64, source='/no/such/private/kick.wav')]
        with patch.object(Path, 'open', spy_open):
            out = render_source_section(self.song, make_manifest(tracks))
        self.assertTrue(opened)
        self.assertTrue(all(name.endswith(PROVENANCE_FILE) for name in opened), opened)
        self.assertIn('kick.wav', out)

    def test_deterministic_output(self):
        self.write_provenance([
            make_row('snare', 'b' * 64),
            make_row('kick', 'a' * 64),
            make_row('unused', 'f' * 64),
        ])
        tracks = [make_track('kick', 'a' * 64), make_track('snare', 'b' * 64)]
        first = self.render(tracks)
        second = self.render(tracks)
        self.assertEqual(first, second)

    def test_no_scripts_or_remote_assets(self):
        self.write_provenance([make_row('kick', 'a' * 64)])
        out = self.render([make_track('kick', 'a' * 64)])
        self.assertNotIn('<script', out.lower())
        self.assertNotIn('<img', out.lower())
        self.assertNotIn('<link', out.lower())
        self.assertNotIn('http-equiv', out.lower())
        self.assertIn('<details', out)
        self.assertIn('<summary>', out)

    def test_sha_is_always_labeled_as_recorded(self):
        self.write_provenance([make_row('kick', 'a' * 64)])
        out = self.render([make_track('kick', 'a' * 64)])
        self.assertIn('run_manifest 记录值，本页不重新校验', out)
        self.assertIn('未重新校验', out)
        self.assertNotIn('已验证', out)
        self.assertNotIn('已核验哈希', out)


if __name__ == '__main__':
    unittest.main()
