# BeatLab：2 → 4 → 5 执行进度

总计划：[MVP 改进计划](2026-09-12-beatlab-mvp.md)。用户选的是原表格第 2、4、5 项，对应总计划阶段 1、3、4；首要交付是可听的整曲。此文记录实际完成情况，不替代总计划的验收门槛。

## 第一首《窗边来信》

已生成新原创器乐编排，88 BPM、52 小节、143.818 秒。Intro 4 → Verse A 12 → Hook A 8 → Bridge 4 → Verse B 12 → Hook B 8 → Outro 4。副歌约 0:44 开始；六轨为电钢琴、Electric Bass、钢琴旋律、Kick、Snare、Hat。

- 试听：`beats/windowlight-v1/full_mix.wav`；短试听：`hook_preview.wav`。
- 乐谱生成器：`examples/windowlight_score.py`；可编辑乐谱：`examples/windowlight.json`。
- 原料已归档到 2TB：`library/instruments/windowlight-v1/`，六文件共 12,196,640 bytes，逐一 SHA-256 匹配原件。
- 本地归档重做入口：`beats/windowlight-v1/score.collected.json`。原始乐谱和试听文件未覆盖旧作品。
- 实际音源是本机 Ableton Core Library 的乐器和鼓 one-shot，逐条路径/哈希在 run manifest 和原料 catalog 中。没有使用 Demo Song，也没有伪称重建缺素材的旧曲 `run-pilot-02`。
- 这条路线是明确写出音符、和声与段落的制作基准；自动 Hero Sample 选择器的音乐质量尚未验收。

交付时用户听感待反馈。2026-09-13 用户回复“很好，还需要一些人声采样啥的 复杂点”，确认 v1 的音乐方向，并要求人声版。修订与证据见 [人声修订记录](2026-09-13-windowlight-vocal-revision.md)。先前“嘈杂”的评价仍归属旧曲，不自动套到新曲；不把一次正面反馈等同整个 MVP 已验收。

## 已实现与仍缺什么

| 所选项目 | 本次交付 | 仍待完成 |
|---|---|---|
| 2 音乐质量 | 新整曲、明确乐谱、六分轨；源 BPM 小节换算、Bass MIDI 28–47、调名兼容；缺指定 Hero stem 报错；人声渲染实际消费 stretch_to | 听感验收、按时间点修改；全局 beat/downbeat 分析、旧生成器减层 A/B、自动反馈 worker |
| 4 Ableton | 实际 6 AudioTrack / 6 AudioClip / 7 Locator，0 起点，Warp Off，无 FX，统一增益；项目内媒体、MIDI/score、参考混音 | Live 内 Missing Media、重开/换目录、回渲染残差、用户继续制作的验收 |
| 5 素材与商品 | 本次实际六音色归档；自动导出主 WAV、MP3、六轨 ZIP、MIDI、metadata、来源信息、封面 SVG 草图、DJ M3U8 | 单一网络来源的发现/授权下载/去重入库、用户 Keep 后扩曲、商用许可完整核验、平台字段及上架 |

网络自动找歌仍未实现。下一来源不能仅因音频标称 CC0 就默认其 API 商业用途也获许可；具体接口条件与已授权清单入口保留在总计划阶段 4。商品包始终为 `draft / ready_to_publish=false`，不代表已经销售或拿到授权。

## Ableton 验证事实

输出：`exports/windowlight-v1/AbletonProject/Windowlight.als`。它包含实际音频片段，不是旧版只改 BPM 的模板。MIDI 与 score 放在 Source 供深度编辑；音色和混音处理已烧录进音频分轨，不能把 MIDI 单独拖入后称为同样的音色。

2026-09-12 23:47，通过 Live 原生打开对话框选择并打开该工程。Live 12.3.2 的 `Log.txt` 记录了 Open document、Loading document 和随后 End ExchangeDocument。**点击 Open 后 CUA 的界面读取与截图连续超时，重连也未恢复**，因此没有取得载入后六轨可见、Missing Media 检查或 Live 回渲染证据。日志载入事实不等于上述验收已通过；未强制退出 Live，未更改其全局音频设置。

## 本轮验证

- `python -m unittest discover -s tests -p 'test_*.py' -v`：51/51 通过，4.101 秒。包括 14 项音乐对齐、2 项真实渲染对齐、5 项乐器渲染、13 项 Ableton 完整性、5 项商品包及原有 12 项契约测试。
- `git diff --check` 通过。没有把这次测试替换成模型下载、真实拆轨或完整 all 流水线验收。
- 新歌 44.1 kHz、24-bit stereo；综合响度 -15.2 LUFS，真峰值约 -1.0 dBFS，响度范围 3.1 LU。它们只证明技术范围，不证明音乐审美。
- 原始六分轨与试听混音：峰值残差 `5.960464477539062e-7`，相对 RMS `-120.576 dB`。此项是分轨求和，**不是 Live 回渲染**。
- 商品包 14 文件哈希核验通过，ZIP 有六个真实分轨，包内 master.wav 与试听文件逐字节相同。
- 试听 SHA-256：`43f56d6f28fdf6c97ddecafa71e2ebad78055e44bcc150f1a6f980fe59e230fe`。
- 证据：`evidence/windowlight-v1.json`、`evidence/ableton-windowlight-v1.json`、`evidence/release-windowlight-v1.json`；测试日志 `.cache/windowlight-test-suite.log`。

## Git 与后续交接

分支 `codex/sandisk-migration`，HEAD `4999be06a1a2656c09b042b50959c90b4448d918`。本次沿用已有专用工作分支，保留前轮未提交的迁移和反馈修复；未另建工作树，未合并远端，未提交、推送或发布。不能把这份执行进度写成整个 MVP 已完成。

后续 git commit / push 的必要文件包括本总计划、此执行进度、根目录 AGENTS.md、实现代码及相关测试。音频、原始音色、数据库、模型和用户本地工程留在 2TB 的忽略目录；不得为了带计划而 `git add -f` 这些运行数据。

下一步已随下方最新乐句翻采反馈更新；完整 Live 回渲染、网络来源与商品主线继续保留，尚未验收。

## 2026-09-13 MIDI / 鼓机实践补充

用户希望采样成为可演奏的鼓机素材，已交付独立 16-pad 练习包、4 段 8 小节 MIDI、32 小节串联与 Python 试听。Native ALS 实含 1 MIDI 轨、16 Simplers、4 clips / 560 notes；不是只有附带 MIDI 的音频工程。文件层面核验通过，全套 54 测试通过；Live 原生文件窗口不响应自动打开，工程内发声仍待验证。完整整曲 MIDI 音源还原及原有网络来源/反馈/回渲染主线仍未完成。见 [MIDI 练习计划](2026-09-13-midi-drum-practice.md)。

## 2026-09-13 最新：从完整乐句翻采

用户纠正上一轮偏向单音鼓机练习的理解。现以本机 Core Library 的 `Rhodes Dust BbMaj 115 bpm.wav` 完整演奏为主素材，升 2 半音、独立伸缩到 92 BPM 后切成 8 块，配合倒放/八度变化和已有 Windowlight 人声乐句，重排成 36 小节《Dust Letters》。完整音乐 95.913 秒，14 个切片、96 次 MIDI 触发、21 条分轨；31.217 秒对照按原片段→采样 solo→成品排列。来源、切点、变调参数、目标时长、MIDI note 与 SHA-256 均保留；乐句库 5 条。

全套 57/57 测试通过（3.958 秒），实际检查 14 个切片时值/来源、96 个 MIDI 起止、21 个非静音分轨与求和、14 个 Rack 媒体和 21 个 ALS 音频片段引用。综合响度 -19.1 LUFS，真峰值 -1.0 dBFS；这些是技术检查，不代表已经听感通过。旧 v1/v2 母版哈希不变。Live 新工程/预设加载发声未验，本轮没有继续操作此前卡住的原生打开窗口。

生成代码 `examples/dust_letters_flip.py`，通用切片 `pipeline/sample_flip.py`；证据 `evidence/dust-letters-v1.json`；完整交付记录见 [翻采计划](2026-09-13-phrase-sample-flip.md)。音频、Rack、工程与乐句库留在 2TB 忽略目录，原创配方/实现/计划纳入后续 commit/push；本轮未提交、推送、发布。

下一步：以 Dust Letters 对照试听获取对选句、重复与段落变化的具体反馈，修订这个翻采基准；不把鼓机练习或自动批量下载抢到此项之前。


## 2026-09-13 老歌爬取与提交准备

用户对 Dust Letters 反馈“很好很好”，要求继续老歌采样、爬虫、git push 和沉淀。已接入 Citizen DJ Blues/Jazz 官方公开目录，实际归档与入库14段乐句；重跑跳过已有文件并继续发现，跨合集重复音频没有再次入库。总计383.165秒。全套64/64测试通过（4.336秒），合成端到端烟测通过，独立审查的两项CLI参数问题已修复。下载、来源、许可快照、哈希和数据库检查证据见 `evidence/citizen-dj-crate-2026-09-13.json`；制作方法见 [采样制作手册](../../sampling-playbook.md)。

当前准备把之前累计的路径迁移、数据契约/反馈修复、整曲与Ableton/商品草稿导出、乐句翻采/MIDI、老歌发现、测试与完整计划一并提交到当前 `codex/sandisk-migration` 分支并推送。原始音频、生成媒体、模型、数据库、应用日志和Hub私人记录留在忽略目录。实际提交/推送结果以Git记录和本轮Hub复盘为准；不覆盖远端main或强推。原有Live实际发声、自动反馈再生成、自动音乐质量筛选与完整M1/M2仍待完成。
