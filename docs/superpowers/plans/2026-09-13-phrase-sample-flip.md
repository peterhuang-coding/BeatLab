# Phrase Sample Flip Implementation Plan

**Goal:** 按用户纠正，从完整音乐乐句取材，经变调、切片重排、重复和段落变奏形成一段有主题的新 beat，交付原片段与改编试听。

**Architecture:** 2TB 乐句库保留原始 WAV、SHA-256、BPM/调性来源和许可状态；显式切片表保存源起止、变调、倒放、目标长度与 MIDI 映射。复用当前 song renderer / Ableton audio exporter，避免把当前目标再次换成鼓机教学工程。

**Tech Stack:** 已安装 Rubber Band R3、Python / NumPy / soundfile / scipy / mido；当前 BeatLab 渲染模块。

2026-09-13 用户纠正：希望学习 Kanye 等制作人从一段音乐中取样、变调、前后切、重复的做法，更有趣、更复杂；音乐采样库要有可改编的乐句。上一轮“鼓机实践”只是部分理解，不是最终目标。

研究依据（只提炼方法，不复制具体作品）：

- Manny Marroquin 在 Sound On Sound 的《Stronger》制作访谈介绍，Kanye 将词句和和弦变化切开映射到琴键，让采样承担旋律和节奏。来源：https://www.soundonsound.com/techniques/secrets-mix-engineers-manny-marroquin 。搜索索引可读正文，直接打开返回 403，未绕过。
- Kirk Knight 的 Ableton 访谈说明音高、时间伸缩、小片段拉长和不同素材的音高配合。来源：https://www.ableton.com/en/blog/input-output-kirk-knight/ 。
- Tracklib 官方 Songs 是完整录音，Sounds 是 loops / one-shots；适合后续“挖歌→取句”来源评估，本轮未登录、订阅或获取付费素材。来源：https://www.tracklib.com/ 。

本轮选择：Core Library 的 Rhodes Dust BbMaj 115 bpm 是现有 4 小节演奏乐句，归档后为主采样。完整翻采已有 Windowlight 与网络歌曲采样是后续可替换素材路线；本轮不把单音堆叠称为老唱片采样。

- [x] 新增 `pipeline/sample_flip.py`：源时间范围检查、倒放、独立变调/时长、短淡入淡出；测试真实频率变化和时值，不只检查参数。
- [x] 新增 `examples/dust_letters_flip.py`：五条乐句库索引、主采样切片表、约 90 秒编排、原片段/采样 solo/成品，MIDI 和 Ableton 音频工程。
- [x] 验证切片来源、渲染求和、音频无削波、MIDI 触发映射、旧作品不变；听感待用户反馈。
- [x] 更新 AGENTS / 总计划，后续随代码提交，修正旧的“鼓机教学包就是目标”理解。Hub 写回结果见独立复盘记录。

段落设计：4 小节引子 → 8 小节主题 → 8 小节密集切片 → 4 小节低八度桥段 → 8 小节副歌变奏 → 4 小节尾奏。92 BPM。复杂度主要来自同一乐句的顺序、重复与响应变化；鼓和 Bass 跟随采样。

## 实际交付与复现

- `beats/dust-letters-v1/full_mix.wav`：95.913 秒、44.1kHz / 24-bit stereo；`before-after.wav`：31.217 秒，原片段 0–8.348s，间隔 1s，重排 solo 9.348–19.783s，间隔 1s，成品 20.783–31.217s。对照作 RMS 匹配与峰值保护，不声称 LUFS 相等。
- `library/loops/phrase-bank-v1/乐句库.md`：4 条 Core Library 键盘/吉他演奏 + 1 条自己的 Windowlight v2 人声编排 resample。人声是既有 Oh/Ah 编排的再采样，没有伪称来自新录音歌手或 Kanye 唱片。当前库是本地素材归档与候选，尚未实现网络自动发现。
- `exports/dust-letters-v1/AbletonProject/DustLetters.als`：21 条音频轨保留试听编排；`ChopRack/Dust Letters Phrase Chops.adg`：14 个实际采样器，接收 MIDI 48–61；对应 `phrase-chops.mid` 为 144 拍、96 个事件。预设供改编，成品滤波、混响、鼓/Bass、总线增益未在预设重建，不能当成相同混音。
- 复现入口 `.venv/bin/python -m examples.dust_letters_flip`，依赖现有 Windowlight v2 及本机 Core Library；独立变调/伸缩需要可执行的 `rubberband` CLI（本机 `/opt/homebrew/bin/rubberband`）。已有输出时拒绝覆盖，后续修订需另取版本或归档本轮生成物。

验证：新增 3 个 DSP 测试先红后绿，检查选中源区域、实际变调后主频、目标帧数、倒放能量位置与非法范围。全套 57/57 通过，3.958 秒，日志 `.cache/dust-letters-tests.log`。初次编排的 d 片未使用；已在应答句引入该片并重新渲染，旧尝试保留 `.cache/dust-letters-first-render`。最终所有 21 轨非静音；分轨求和相对 RMS -116.594 dB、峰值差 5.96e-7。原片段、切片、MIDI 与 Rack 引用逐一核验；Windowlight v1/v2 母版哈希未变。

FFmpeg 测量：-19.1 LUFS、真峰值 -1.0 dBFS、LRA 7.3 LU，日志 `.cache/dust-letters-loudness.log`。未声称这些数值证明好听；当前用户尚未听本次 Dust Letters。Live 实际打开、播放与回渲染仍未验收。完整证据 `evidence/dust-letters-v1.json`。

后续自动化所需的创作单元应保存源乐句、切点、操作和 MIDI 编排，并让用户在“原句选材 / 翻采主题 / 鼓与混音”三个层面定位反馈。本轮先交付这套可听对照与明确配方，尚未接入反馈 worker 或自动候选排名。

后续实际反馈（2026-09-13）：用户回复“很好很好”，并要求探索更多老歌、运行爬虫、git push 及方法沉淀。已将正向反馈关联到本次完整混音 SHA `6780d6f6e4069b4483489539b6e8f7633cbaad01cbb476bc823c4e86a0aa0adb`；证据 `evidence/dust-letters-feedback-2026-09-13.json`。上文“待试听”是交付时历史状态；本次反馈确认该翻采方向，不自动创建星级、Keep 或整套 MVP 验收。
