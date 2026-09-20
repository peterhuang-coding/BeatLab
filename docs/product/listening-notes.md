# 试听笔记（Listening Notes）使用说明

针对 `beats/<run_id>/` 下已渲染的 score song（含 `run_manifest.json`、`score.json`、
`full_mix.wav` 与 stems），在 Review 页按播放时间记录人工试听反馈。笔记只做记录，
不会自动改音量、改词、换采样、重新编曲，也不会触发 reweight；是否接受仍由人判断。

## 在页面上怎么用

1. 启动本地服务（仅监听 127.0.0.1）：
   在本执行工作区运行 `BEATLAB_ROOT=/Volumes/SanDisk2TB/BeatLab /Volumes/SanDisk2TB/BeatLab/.venv/bin/python pipeline/feedback.py serve --port 8796`
2. 打开 `http://127.0.0.1:8796/review/<run_id>`。
3. 播放主音频；听到问题或亮点时点「抓取当前播放位置」，时间点自动填入；
   也可以手动输入 0 到歌曲时长之间的秒数。
4. 选择类别并写一句话，点「保存笔记」。保存成功或修改草稿后会自动换新请求 ID；
   网络失败后直接重试即可，服务端按同一请求 ID 幂等去重，不会产生重复笔记。
5. 「此版本已保存的笔记」列表中点击时间按钮即可跳转回该播放位置。

类别（固定六项）：`noisy` 太嘈杂、`repetitive` 重复、`drums` 鼓组、
`transition` 过渡、`like` 喜欢、`other` 其他。

笔记位于主播放器下方；逐层音量和原句试听折叠显示。页面显示当前版本的真实时长与音频哈希前缀，完整 SHA256 可悬停查看。版本上下文只在
页面加载时读取一次；保存始终绑定这个 SHA，不会在提交时静默刷新。音频重新渲染后
请刷新页面再记录。页面既有的分层音量「生成反馈版」（`POST /api/revise`）保持不变。

## HTTP API

### `GET /api/listening-notes?run_id=<run_id>`

返回**当前** `full_mix.wav` 版本的信息与笔记：

```json
{
  "ok": true,
  "run_id": "song-001",
  "mix_sha256": "64 位小写十六进制",
  "duration_seconds": 210.0,
  "notes": [
    {"id": "...", "run_id": "song-001", "mix_sha256": "...",
     "request_id": "uuid", "time_seconds": 12.5, "category": "noisy",
     "text": "镲片有爆音", "created_at": "2026-09-20T08:30:00+00:00"}
  ]
}
```

重新渲染后旧版本笔记仍保留在数据库中，但 GET 只返回与当前 SHA 匹配的行。

### `POST /api/listening-notes`

请求体（JSON）：

| 字段 | 要求 |
| --- | --- |
| `run_id` | 非空；仅字母、数字、`.`、`_`、`-`，且必须对应 `beats/` 下真实 score song |
| `mix_sha256` | 恰好 64 位小写十六进制；必须等于当前 `full_mix.wav` 的实际 SHA256 |
| `request_id` | 去空白后 1–128 字符；同一 (run, SHA, request_id) 幂等 |
| `time_seconds` | 有限数字（不接受布尔、字符串、NaN/Infinity 或溢出的超大整数），`0 ≤ t ≤ 实际 WAV 时长` |
| `category` | 必须是 `noisy / repetitive / drums / transition / like / other` 之一 |
| `text` | 去首尾空白后 1–1000 字符 |

返回 `200 {"ok": true, "note": {...}}`。

状态码：

- `400` 参数缺失或非法（含非法 run_id、时间越界、类别/文本不合规）。
- `404` run 不存在、缺少 `run_manifest.json`/`full_mix.wav`。
- `409` 两种情况：
  - `mix_sha256` 已过期（音频重新渲染）——请刷新页面读取新版本；
  - 同一 `request_id` 曾保存过**不同内容**——原笔记不会被覆盖，请换新请求 ID。
- 同样的请求（含相同内容）重试返回原笔记（`note.deduplicated = true`）。

## 存储与安全边界

- 笔记写入独立文件 `$BEATLAB_ROOT/listening-notes.sqlite`，表 `listening_notes`
  （`id / run_id / mix_sha256 / request_id / time_seconds / category / text /
  created_at`，`UNIQUE(run_id, mix_sha256, request_id)`），全部 SQL 参数化，
  时间戳为 UTC，只追加，不更新、不删除。
- 与既有 `db.sqlite`（feedback 等表）完全分离；仅只读音频文件用于版本识别，不修改歌曲文件；
  哈希与时长直接来自 `full_mix.wav`（流式 SHA256 + 标准库 `wave` 读帧数/采样率）。
- 路径白名单 + `resolve()` 包含校验：目录穿越（`../`）、指向库外的符号链接、
  指向库外的音频文件一律拒绝；请求体中的路径不会被当作文件读取。

## 非目标（本期不做）

- 不打口味分、不推断偏好、不做自动 reweight 或任何自动修改。
- 来源追溯由同页独立区块展示；缺失来源会明确标出。
- 不提供笔记编辑/删除接口；如需更正请追加一条新笔记。
