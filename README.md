# BeatLab

[产品方向与 Notion 看板](https://app.notion.com/p/3db3285284df81f4aa50ebe2047b6180) · [六条产品调研与工程待办](docs/product/research-backlog.md) · [白天定方向、夜间执行](docs/product/day-night-workflow.md)

本地采样型 beat 制作流水线：导入音频 → 拆轨 → 找采样片段 → 围绕一个 Hero Sample 生成 Loop / Chop / Stem 三个候选 → 试听、反馈与 Ableton 交付。

当前版本生成 **三个 60–90 秒的制作草稿**。3–4 分钟完整编排、双机任务队列和进一步质量改进见 [产品规划](docs/PRD.md)，不代表已经实现。

新增一条明确写好音符与段落的整曲制作路线：`song` 用采样乐器渲染原创乐谱，输出统一增益的 WAV 分轨和 MIDI。第一首《窗边来信》是 88 BPM、52 小节的 Soul / Hip-hop 器乐曲。它是人工编排的质量基准，自动 Hero Sample 选择器还没有达到同样的音乐验收。

乐句翻采基准《Dust Letters》从完整演奏取句，经变调、重排、短重复、倒放与八度变化形成新主题，提供原片段→采样 solo→成品对照。两条制作路线均已有用户正向反馈；创作方法见 [采样制作手册](docs/sampling-playbook.md)。

2026-09-16 今日作品 [《余温 · Afterglow》](docs/superpowers/plans/2026-09-16-afterglow.md)：从已入库的真实历史女声录音取句，94 BPM / 48 小节，16 个切片、原句对照、23 条音频分轨、Ableton 音频工程与可演奏 ChopRack。文件验收已完成，听感待用户试听；[Notion 交付记录](https://app.notion.com/p/3dc3285284df81559e03d0b09179ff54)。

当前执行顺序、未完成项和验收门槛在 [MVP 总计划](docs/superpowers/plans/2026-09-12-beatlab-mvp.md)。用户已选择 **音乐质量 → Ableton 还原 → 素材来源与商品包**；后续提交推送必须带上计划与执行进度。

## 安装

macOS Apple Silicon，Python 3.12，系统需有 `ffmpeg` / `ffprobe`；乐句独立变调/伸缩还需要 `rubberband` CLI（macOS 可用 `brew install rubberband`）。项目路径可以位于外置硬盘，带空格也可。

```bash
# 在项目目录运行；已有 Python 3.12 和 uv 时
uv venv --python 3.12 .venv
uv pip sync --python .venv/bin/python requirements.txt

# 或使用 Python 自带 venv / pip
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

`requirements.in` 记录直接依赖，`requirements.txt` 锁定版本。Essentia 当前未被代码调用，不需要额外安装。拆轨使用 audio-separator 的 Demucs v4 `htdemucs_ft.yaml`，首次运行下载约 321 MB 权重到 `.models/`。

## 运行

```bash
./beatlab ingest --path "/你的音频目录" --limit 10
./beatlab separate --all --skip-if-done
./beatlab moments --all
./beatlab score --all

# 每次新创作使用新的 run_id；以下步骤使用同一个 ID
./beatlab compose my-first-run --bpm 92
./beatlab render my-first-run
./beatlab report my-first-run

# 浏览器打开 http://127.0.0.1:8793/；Ctrl+C 停止服务
./beatlab feedback serve --port 8793
```

所有模块也可通过 `.venv/bin/python pipeline/<模块名>.py` 单独运行。`./beatlab all` 仍是旧版占位入口，**没有执行全流程**，请使用上面的逐步命令。当前 Regenerate 只记录待办请求，尚无后台 worker 自动执行。

在线 Review 支持三个候选对比试听、打分、Keep / Reject / Export。直接打开静态 HTML 可以试听，提交反馈需从本地服务访问。端口被其他项目占用时，用 `--port` 换空闲端口。

### 从老录音发现乐句

```bash
./beatlab ingest --source citizen_dj --path blues --limit 6 --timeout 120
./beatlab ingest --source citizen_dj --path jazz --limit 6 --timeout 120
```

来源为 [Library of Congress Citizen DJ](https://citizen-dj.labs.loc.gov/loc-jukebox-blues/use/) 的官方公开 WAV 乐句目录。先从不同作品各取一段，再取同一作品的其它片段；这是目录发现策略，尚不代表音乐质量排名。每次默认最多尝试 10 个新下载，`--limit` 上限 100；重复运行会跳过已入库内容并继续发现，下载失败保留原因。跨合集同内容按哈希去重，改坏的缓存重新获取。

下载原件、目录快照和来源记录在 `library/sources/citizen_dj/<blues|jazz>/`；标准化乐句在 `library/loops/<id>/source.wav`，SQLite 保留作品链接和具体合集的许可依据。原曲时间标签与 remix 毫秒偏移分别保留，未宣称它们是逐采样点精确对齐。该来源的年代和音色与 60–70 年代 Soul 不同；后者的挖歌/清样接入见 [老歌采样计划](docs/superpowers/plans/2026-09-13-old-record-crate.md)。

## 数据与迁移

### 整曲、Ableton 与商品草稿

```bash
# 首次依赖本机 Live 12 Core Library 的六个 one-shot；生成明确乐谱
.venv/bin/python examples/windowlight_score.py
./beatlab song --score examples/windowlight.json --out beats/windowlight-v1
./beatlab ableton_export --song beats/windowlight-v1 --out exports/windowlight-v1/AbletonProject
./beatlab package --song beats/windowlight-v1 --out exports/windowlight-v1/ReleaseDraft
```

输出目录已有内容时使用新的版本名。`song`、`ableton_export`、`package` 不写用户反馈数据库；这条路线尚未接自动 Regenerate。

Ableton 输出实际音频轨、段落标记、收集后的 WAV，以及另存的 MIDI/score。音色与混音处理已烧录在音频分轨中；单独 MIDI 不会还原音色。导出器校验文件哈希、音频长度、非静音和分轨相加误差，Live 实际打开及回渲染须另做验收。

`ReleaseDraft` 含原混音 WAV、MP3 试听、真实逐乐器 trackouts ZIP、MIDI、来源与校验信息、封面草图和固定路径的 DJ M3U8。始终标记为草稿；不自动上架，也不把未知的素材许可标成可商用。

这台机器本次使用的六个音色还归档到 `library/instruments/windowlight-v1/`，来源和 SHA-256 在 `source_catalog.json`。可用 `beats/windowlight-v1/score.collected.json` 重做新版，避免依赖应用目录。原始第三方音色不进 Git，也不作为采样包转售。

默认数据根目录是代码所在的项目目录，与终端的当前目录无关：

- `library/<分类>/<id>/`：音源、stems、切片和分析结果。
- `beats/<run_id>/`：三个候选的 WAV、分轨、切片、MIDI、Recipe 与来源记录。
- `db.sqlite`：素材、片段、生成任务和反馈。
- `.models/`、`kit/`：模型权重和鼓组缓存。
- `exports/<日期>/`：静态试听页与交付镜像。

`BEATLAB_ROOT` 可覆盖数据根目录；`BEATLAB_MIRROR_ROOT` 可单独覆盖导出目录。代码和 Python 环境仍从当前仓库加载。

```bash
BEATLAB_ROOT="/外置硬盘/BeatLab-data" ./beatlab feedback serve --port 8793
```

GitHub 只包含代码，**不包含素材、旧作品、数据库、模型或虚拟环境**。迁移已有库时：

1. 保留原目录；确认原始数据已下载，iCloud 占位文件仅有文件名和逻辑大小，不代表本机已有内容。
2. 在目标目录重新安装 `.venv`，不要复制带旧机器路径的 Python 环境。
3. 复制 `library/`、`beats/`、`kit/`、`.models/`；通过 SQLite backup API 备份数据库。
4. 核查数据库和 JSON 中的文件引用。运行路径需指向新副本，`orig_path` 等来源证据应保留；不能盲目全局替换路径。
5. 比较文件数量与内容哈希、检查 SQLite 完整性，再实测试听和渲染。源文件保留到验证结束。

这些运行数据都被 `.gitignore` 排除。

## Ableton 与已知边界

- 传统 `render` 路线仍只复制内置 Quick Start Beat 模板并修改 BPM，需按 `ABLETON_HANDOFF.txt` 拖入媒体。上面的 `song → ableton_export` 路线才会生成实际装入分轨的音频工程。
- 缺少模板可用 `render <run_id> --no-als`；WAV、MIDI、切片和分轨不依赖 Ableton。
- 本轮 `compose` 对指定 Hero stem 缺失会报错；需检查四条 stem 是否真实生成，源 BPM 未知时仍会标记并使用时长 fallback，尚无全局下拍校准。
- 自动鼓组和混音是制作草稿，最终质量需要试听判断。
- 素材保留来源与 rights 状态，发布前按实际许可处理采样。

## 验证

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v

# 用合成音频实跑摄入、片段分析、编排、渲染、静态试听；结果保存在 .cache/smoke-*。
# 不使用用户录音，也不下载模型或测试 Demucs 推理。
.venv/bin/python tests/smoke_pipeline.py
```

`tests/accept_d3.py` 是早期生成层 mock 验收脚本；完整链路验证应使用真实 SQLite 契约并隔离数据根目录。隔离的测试结果不能代替真实音乐素材的制作质量验收。
