# 2026-09-16 今日作品：余温 · Afterglow

状态：音频、对照与工程文件交付完成，待用户试听。用户明确要求「今天的音乐做一下」，沿用已经认可的乐句翻采方向；没有为新曲填写评分或 Keep。

## 创作与来源

94 BPM，48 小节，124.553197 秒。原始素材是 Marion Harris 演唱的 [After You've Gone，1918-10-18](https://www.loc.gov/item/jukebox-313413/)，来源为已入库的 Citizen DJ National Jukebox Jazz 片段。本轮没有新增抓取或调用音乐生成模型。

选取源片段 4.203–8.127 秒作为完整主题，并从 16.347–18.437、22.477–24.520、25.519–27.655 秒取得应答句。先做轻量降噪、压缩，随后独立伸缩、下降 2 个半音，并按估计录音偏差额外下调 11 cents；形成 Eb / Gm / Cm / Bb 的工作和声方案。音高、和弦与节拍来自信号分析和编配判断，未经人耳逐项确认，不能当作原作品标准谱。

以完整主题建立记忆点，副歌用短切重复、空拍、倒放和八度回应；桥段下降音区并减少鼓，最后四小节主题加快和声切换。主采样事件不重叠，鼓和 Bass 使用已归档的 Windowlight v2 乐器，有逐文件来源与哈希。复杂度来自句法变化，未加入额外噪声层。

## 本机交付

全部媒体位于 2TB，Git 只保存配方、来源说明和证据。

- `beats/afterglow-2026-09-16-v1/full_mix.wav`：24-bit / 44.1 kHz 完整曲。
- 同目录 `before-after.wav`：原句 → 副歌采样独奏 → 副歌成品，RMS 对齐并限制峰值，非 LUFS 对齐。
- `original-source.wav`、`original-phrase.wav`、`sample-flip-solo.wav`：原片段和完整采样独奏。
- `score.json`、`source-provenance.json`、`slice-map.json`、`phrase-chops.mid`：编排、来源、16 切片及 110 次 MIDI 触发。
- `exports/afterglow-2026-09-16-v1/AbletonProject/Afterglow.als`：23 条已渲染音频轨，效果写入音频。
- `exports/afterglow-2026-09-16-v1/ChopRack/`：16 个互斥鼓垫、MIDI 和样本，供继续重排。练习预设未还原成品逐轨滤波、空间与总线增益。
- [Notion 今日记录](https://app.notion.com/p/3dc3285284df81559e03d0b09179ff54)。

制作配方：[examples/afterglow_daily.py](../../../examples/afterglow_daily.py)。运行 `.venv/bin/python -m examples.afterglow_daily`；需要本机已入库片段、Windowlight v2 音色、Rubber Band / ffmpeg / Live 12 默认预设。版本目录存在时拒绝覆盖，再创作需使用新 RUN。

## 本轮验证

实际运行配方与音频导出成功；证据 [evidence/afterglow-2026-09-16.json](../../../evidence/afterglow-2026-09-16.json)。没有修改公共渲染器、导出器或用户反馈数据库，也未把历史测试次数作为本轮结论。

- 5,492,796 帧，有限值、非静音，0 个削波采样点；23 个非静音分轨等长。
- 分轨和相对试听残差 RMS **-120.664 dB**，峰值差 4.768e-7；这是文件求和，不是 Live 回渲染。
- ffmpeg ebur128：**-14.7 LUFS**、4.6 LU LRA、-1.0 dBFS true peak。响度指标不证明好听。
- 16 个实际切片的 SHA、目标帧数、鼓垫媒体引用核验；MIDI 110 个 note-on，16 个音符映射完整，全部 MIDI 保留 192 拍整曲长度。
- ALS 23 条 AudioTrack，媒体副本哈希核验；Live 实际打开、重开、发声与回渲染仍待验。
- Windowlight v1 / v2 和 Dust Letters 完整混音哈希未改变。
- 新曲 SHA256：`9e030c07f632b2bcb50971adc83f2aa5d9634f9c3de71cae55068483b66ca3a2`。

首次来源检查失败，原因是把 **48 kHz 下载原件**的哈希与 **44.1 kHz 入库转码件**比较，而非音频被修改。失败发生在新媒体写入前；修正为分别验证两个文件，保存两段来源链，之后整次生成成功。下载原件 SHA `65c87d6b556672460a6beba36fdb90318e13950ed338060d6a3b51e81d92c4dd`，库内源 SHA `c98a33cc7696e93f534cd56d1e4d9d4c9ad04f6d3129cd12343db1be15e45714`。源时间范围相对下载片段，不声称与整张唱片逐采样点对齐。

## 反馈与主线

建议反馈时间点：0:31 第一段副歌、0:51 桥段、1:22 第二段副歌。区分「素材」「切法」「鼓/Bass」「嘈杂程度」，然后只改对应声部并保留本版。

本次完成一首明确配方的创作，不等于自动选材、反馈 worker、Live 还原或商品上架完成。R01–R06 / E01–E04 与分支整合继续保留；音乐未发布到销售/流媒体平台。

下一步：用户试听《余温》并指出一个最想保留或修改的时间段，再据此做下一版。
