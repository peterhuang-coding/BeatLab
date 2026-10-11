# 候选片段本地供应运行手册（supply cuts）

本手册描述每日把最多 4 个来源切成技术候选 WAV 的本地流程。所有字节操作只在本机隔离暂存区完成：**音频与数据库永不发送给模型或任何下游服务**，不付费、不上传、不做商业授权承诺。规划侧只准备脚本与清单，本地 worker（父级）执行实际字节操作并独立验收。

## 1. 边界与固定约束

- 仅产出**技术候选**：清单固定标注 `content_identity="unverified"`、`listening_review="pending"`、`rights_scope="requires_per-use-review"`。
- 不得标注歌词差异、人声/干净人声或音乐质量；不声称已获得商业授权。
- 单次任务：最多 4 个来源、每来源最多 6 个窗口；窗口时长 2–16 秒；每文件 ≤32 MiB，合计 ≤128 MiB；输入绝对峰值 >1.0 拒绝。
- 导出保留源采样率/声道，FLOAT WAV，两端各 5 ms 线性淡入淡出；不做归一化、变调、时间拉伸或任何音乐编辑。
- 源文件、原始数据库、音乐库永不修改；导出只写入**新建**输出目录，失败自动清理本次新建目录；对已存在目录重跑会直接失败且不改动其内容。
- 不新增守护进程/调度器；沿用现有的每日 22:00 心跳。遇到工具权限被拒、限流、未知状态：**停止，不要重试**。

## 2. 本项目已验证的目录和去重账本

执行目录：`/Volumes/SanDisk2TB/BeatLab/.cache/album-execution-20260919`。
隔离素材/DB根（后续继续复用，不每天重新清空）：`/Volumes/SanDisk2TB/BeatLab/library/supply-staging/supply-20261011`。
其 `db.sqlite` 来自主库的SQLite在线备份；所有后续下载仅写该独立DB，以记住历日已下载URL/MD5。每批验收同时与当前主库只读比对，防止主库后来增加同源素材。不要把备份命名为seed.sqlite后假设crate会读取它；crate固定读取BEATLAB_ROOT/db.sqlite。

批次清单位于该根的 `library/crates/supply-YYYYMMDD/manifest.json`，源文件在 `library/sources/`、标准化文件在 `library/loops/`。候选音频验收后登记在主媒体根 `library/candidates/supply-YYYYMMDD/`，不修改既有作品。真实音频、完整任务JSON和DB只在本机，均不进Git或CC任务包。

## 3. 执行步骤

### 3.1 受限下载（既有 crate CLI）

先读取 `.claude/autopilot/supply-library.json`：本日批次已存在则只核查恢复点；有未知/失败任务或2批待技术验收时，不新建下载。未消费候选达到96个时先整理存量，不机械扩库。

以下为首批实际命令；后续仅将batch-id换成当天北京时间日期，同一天不重复新建。已完成批次可在禁用网络测试下验证 `--resume`；失败批次不得不检查原因便resume或换ID重发。

```bash
BEATLAB_ROOT=/Volumes/SanDisk2TB/BeatLab/library/supply-staging/supply-20261011 \
  /Volumes/SanDisk2TB/BeatLab/.venv/bin/python pipeline/pipeline.py crate \
  --batch-id supply-20261011 --collections blues jazz --limit 4 --timeout 240
```

- 每日最多4来源；每文件32MiB，最多128MiB。连接器内置最多3次网络重试，240秒是协作式检查/socket时限，并非硬墙钟。外层不额外重发批次。
- 主代理须先查前批结果、磁盘容量、在途进程与来源允许范围。权利说明变化或来源失败时隔离并保留现场，不借新日期绕过。

### 3.2 既有 moments 分析（不写库）

对每个下载来源调用既有的本地分析器：

```python
moments.analyze_asset(asset, write_db=False, candidate_mode="phrase_v1")
```

- `write_db=False`：有界、只读分析，不写数据库。
- 不导入/重写分析器或下载器；导出器与二者无代码耦合。
- 不做全库分析；只分析当日 ≤4 个来源，保留至多6个候选；空结果记录缺口，不能编造切片。
- `moments` 使用22.05kHz分析，候选末端可能因重采样/九位小数超出实际文件至多一个源采样点。依据 `soundfile.info` 的真实frames/samplerate校正，并记录旧/新边界；超过一个源采样点则拒收，不随意截断句子。首批真实触发3次此校正。
- 输出窗重叠以同源时间区间检查，超过0.5秒需重新筛选；这不是歌词内容身份鉴别。

### 3.3 组装 job.json（父级本地编写）

```json
{
  "batch_id": "supply-YYYYMMDD",
  "sources": [
    {
      "asset_id": "synthetic1",
      "path": "/absolute/source.wav",
      "sha256": "<源文件 64 位小写 sha256>",
      "source_url": "https://example.org/item/1",
      "rights_evidence_url": "https://example.org/rights",
      "windows": [
        {"start_sec": 0.2, "end_sec": 2.5, "type": "melody", "basis": "phrase_v1"}
      ]
    }
  ]
}
```

- `asset_id` 仅允许 `A-Z a-z 0-9 _ -`（1–64 字符），不得含路径分隔符、`..` 或点号；同一任务内不得重复。
- 每个来源必须有绝对路径、小写 sha256、http(s) 的来源 URL 与权利证据 URL。
- 窗口边界必须有限、`0 ≤ start < end`、时长 2–16 秒、不超出音频；帧边界按 `int(seconds * sr)` 换算。

### 3.4 运行导出器

```bash
/Volumes/SanDisk2TB/BeatLab/.venv/bin/python \
  pipeline/supply_cuts.py \
  /Volumes/SanDisk2TB/BeatLab/library/supply-staging/supply-20261011/cut-job.json \
  /Volumes/SanDisk2TB/BeatLab/library/candidates/supply-20261011
```

- 先完整校验任务与**全部**音频（存在性、大小、sha256、有限非空、峰值、边界），全部通过后才创建输出目录；因此后一个来源不合格也不会留下任何输出。
- 成功时 stdout 仅一行计数，例如：
  `batch=supply-YYYYMMDD sources=2 windows=6 candidates=5 skipped=1`
- 退出码：合法执行 `0`（即使窗口全部因静音跳过）；校验/用法失败 `2`；IO 等运行失败 `1`。
- RMS < −50 dBFS 的窗口跳过、不写 WAV，但记录在 `manifest.json` 的 `skipped` 中。
- 候选文件名：`<asset_id>_<两位窗口序号>.wav`；窗口序号与 job 中位置一致（跳过也占位）。

## 4. 父级验收清单（接纳前逐项独立核对）

1. **来源/provenance**：每个候选的 `source_url`、`rights_evidence_url` 可核对、与下载批次一致。
2. **哈希**：`source_sha256` 与源文件现状一致；`output_sha256` 与候选 WAV 字节一致；重算通过。
3. **音频技术指标**：采样率/声道与源一致；帧范围 `start_frame/end_frame == int(sec*sr)` 且与清单吻合；两端恰为 5 ms 线性淡变；峰值 ≤1.0、无归一化/时间/音高改动。
4. **静音与重复**：确认跳过项符合预期；人工核对候选之间及与既有库的**重叠**，去重后再接纳。
5. **清单标记**：`content_identity="unverified"`、`listening_review="pending"`、`rights_scope="requires_per-use-review"` 未被改动。
6. 全部通过前，候选目录仅为暂存物；通过后由父级决定本地接纳，规划侧不做最终判断。

## 5. 每日限额汇总

| 项目 | 限额 |
| --- | --- |
| 每日来源数 | ≤4（`--limit 4`，超时 240s） |
| 单源大小 | ≤32 MiB |
| 全部来源合计 | ≤128 MiB |
| 每来源窗口 | 1–6 个，每个 2–16 秒 |
| 调度 | 仅沿用现有每日 22:00 心跳，无新 daemon |
| 下游/付费 | 禁止上传模型、禁止付费回退、禁止全库分析 |

## 6. 故障恢复

固定批次/CC逻辑ID保留。校验失败先记录精确原因，核对是否已有输出；导出器正常失败不会覆盖旧目录。运行失败保留日志和输入，确认进程终止、目录清理情况后才决定范围内修正；不能通过新日期/新ID绕过限次。不要删除整个暂存库或用旧DB备份覆盖累计去重账本。

输出已存在时仅核对manifest与真实SHA；验证通过则复用，不重新导出。新版本仅在明确修订时使用有父子关系的新目录。权限拒绝、429、unknown或持续同因失败停止该链路与后续新下载，保存恢复点。只读巡检发现状态恢复并不自动授权无限重试。

## 7. 首批实际验收

2026-10-11：4段源录音、10个真实WAV候选。主代理独立复跑22项合成测试、逐样本核对5ms淡变、FLOAT格式/声道/44.1kHz、原/新SHA、同源重叠、重复导出拒绝覆盖和20份已交付音频SHA/mtime。尚未主观试听，不宣称唱句完整、不同歌词或可售。
