# BeatLab `crate` 命令用户说明

`crate` 用于从 CitizenDJ 官方 Blues/Jazz 摘录合集下载 WAV 音频，并生成可用于后续音乐制作流程的素材目录。它不会搜索整个互联网，也不会下载完整歌曲；仅处理官方合集中允许使用的摘录。

## 运行环境

当前工作目录应在：

```bash
cd /Volumes/SanDisk2TB/BeatLab/.cache/album-execution-20260919
```

该目录本身没有 `.venv`，请使用仓库共享虚拟环境，并设置根目录：

```bash
export BEATLAB_ROOT=/Volumes/SanDisk2TB/BeatLab
/Volumes/SanDisk2TB/BeatLab/.venv/bin/python pipeline/pipeline.py crate --help
```

不要在当前工作树中直接使用 `./beatlab`，也不需要切换旧 root 分支。

## 新建下载批次

参数之间使用空格分隔。示例：创建一个包含 blues、jazz 的新批次，最多选择 8 条素材，本次运行的协作式时间预算 240 秒：

```bash
export BEATLAB_ROOT=/Volumes/SanDisk2TB/BeatLab
/Volumes/SanDisk2TB/BeatLab/.venv/bin/python pipeline/pipeline.py crate \
  --batch-id new-crate \
  --collections blues jazz \
  --limit 8 \
  --timeout 240
```

新建批次默认选择 8 条；全局最多 100 条。选择时会优先选取数据库中尚未保存的作品，每个作品在一个批次中只使用一次。跨来源会按内容 MD5 去重，因此最终资产数可能少于选择数量。

如果只想保存候选清单、不下载音频：

```bash
/Volumes/SanDisk2TB/BeatLab/.venv/bin/python pipeline/pipeline.py crate \
  --batch-id discover-crate \
  --collections blues jazz \
  --limit 8 \
  --discover-only
```

## 断点恢复

恢复已有批次时，只需提供原批次 ID 和 `--resume`：

```bash
/Volumes/SanDisk2TB/BeatLab/.venv/bin/python pipeline/pipeline.py crate \
  --batch-id new-crate \
  --resume
```

恢复会使用该批次已记录的选项，不能再添加新的合集、数量或超时等选择/时间参数。失败项目会重试；已完成项目不会重新发起网络下载，而是校验本地 SHA 和数据库记录。若文件损坏，需将损坏的原件或标准化文件恢复为该文件保存的 SHA 对应内容，才能通过校验；系统不会强制覆盖。不能用同一批次 ID 调用“新建批次”。

连接/超时、HTTP 429 或 5xx 最多尝试 3 次；新发现时若来源不可用或合集权利声明变更，会保留失败；已固定批次按保存并校验过的目录快照恢复。每个音频下载大小上限为 32 MB，新音频下载前默认暂停 1 秒。`--timeout` 是协作检查和 socket 超时控制，不是绝对硬计时器。

## 输出位置

批次文件写入：

```text
library/crates/<batch-id>/manifest.json
library/crates/<batch-id>/catalog.json
library/crates/<batch-id>/README.md
library/crates/<batch-id>/playlist.m3u8
```

来源缓存位于：

```text
library/sources/citizen_dj/<collection>/<urlhash>/download.wav
library/sources/citizen_dj/<collection>/<urlhash>/source.json
```

标准化音频位于：

```text
library/loops/<asset-id>/source.wav
```

原始文件与标准化文件分别记录 SHA；标准化音频固定为 44100 Hz。

## 后续使用

`crate` 只完成下载、校验、标准化和目录写入，不会自动分析、挑选切片或生成节拍。需要时可在现有 Python 流程中针对某个资产运行 `pipeline/moments.py`，例如传入 `ASSET_ID` 创建候选点；它不会自动执行 `--all`。

权利依据以 CitizenDJ Blues/Jazz 官方合集使用说明为准；重复素材会保留数据库中已有权利记录。素材主要来自 20 世纪早期来源，并非六七十年代灵魂乐，也不连接 Tracklib 账户。已验证一次 2/2 冒烟流程，重复运行不会重新 GET 已完成音频。

## 本轮真实结果（2026-09-19）

正式批次 `old-records-20260919` 已完成：8 段、8 条不同录音，Blues/Jazz 各4段；资产从14增至22。目录共发现309/339个WAV链接，没有全量下载。正式与隔离批次恢复均无网络请求，音频SHA和mtime保持；此前24份音频保持。参见 [来源与文件证据](../../evidence/crate-harvest-2026-09-19.json) 和 [执行计划](../superpowers/plans/2026-09-19-crate-harvest.md)。下载成功不代表已筛出好听的乐句，后续制作需试听。

来源页面：[Blues](https://citizen-dj.labs.loc.gov/loc-jukebox-blues/use/) · [Jazz](https://citizen-dj.labs.loc.gov/loc-jukebox-jazz/use/)。原片均为48kHz，库内为44.1kHz，分别保留哈希。
