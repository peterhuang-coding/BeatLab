# 尘里有金 · Gold From Dust

2026-09-27：按用户新反馈，制作真实录音自行裁切的 Kanye-inspired beat 草稿。92 BPM，20 小节，54.174 秒。主旋律和人声来自历史录音；鼓与 Bass 使用已有 Ableton Core Library one-shot。没有用 TTS 或振荡器合成人声/主旋律。风格参考为升调乐句、重排、回答、重鼓与留白，不是采样 Kanye 的歌曲或仿冒其演唱。

本轮交付一个可听版本，尚无用户 Keep；不是两个盲听候选，也不是整张专辑完成。

## 试听与工程

- 本机试听：<http://127.0.0.1:8796/review/gold-from-dust-20260927-v1>。
- 音乐：`/Volumes/SanDisk2TB/BeatLab/beats/gold-from-dust-20260927-v1/full_mix.wav`。
- 工程包：`/Volumes/SanDisk2TB/BeatLab/exports/gold-from-dust-20260927-v1`。
- `AbletonProject/Gold From Dust.als`：17 条音频分轨，warp 关闭、unity gain；可编辑音频编排，不宣称重建了全部 MIDI 乐器。
- `ChopRack/Gold From Dust Record Chops.adg`：10 个真实录音切片鼓垫；`phrase-chops.mid` 为25次触发、20小节完整时间线。它用于重新演奏，不承诺与已混音分轨音色/响度逐样本相同。
- `Listening/original-cut-mix.wav`：原录音窗口 → 处理切片 → 使用该片段的混音位置；间隔1秒，线性 RMS 匹配并记录峰值保护。这是制作过程对照，不是 LUFS 盲听。
- `Listening/sample-flip-solo.wav`：单独翻采层；`Sources/`、`source_provenance.json`、`delivery_manifest.json` 保存来源和文件核验信息。

约10.4秒进入主段，31.3秒切半拍，41.7秒回归。复杂度来自片段接力、鼓型与留白；每一时刻最多一条完整历史录音切片做前景。

## 真实来源与处理

| 录音 | 当前角色 | 处理 |
| --- | --- | --- |
| [After you've gone](https://www.loc.gov/item/jukebox-313413/) | 主句与主题回忆 | 4个短片段及1个长片段，升3半音 |
| [Blues (My naughty sweetie gives to me)](https://www.loc.gov/item/jukebox-33694/) | 回答 | 4个相邻源区间，降2半音 |
| [Crazy blues](https://www.loc.gov/item/jukebox-188857/) | 转场 | 1个片段，降2半音并倒放 |

使用已有 Citizen DJ 采集片段，本轮没有下载新歌曲。依据为官方 [Jazz](https://citizen-dj.labs.loc.gov/loc-jukebox-jazz/use/) / [Blues](https://citizen-dj.labs.loc.gov/loc-jukebox-blues/use/) 用途说明；Core one-shot 的依据为 [Ableton 内容使用说明](https://help.ableton.com/hc/en-us/articles/209768885-Commercial-Use-rights-for-Live-content)。本轮不发布，不转售单独素材。

源窗口、变调、处理顺序、文件 SHA 都存于 `prepared.json` 和歌曲 `slice_map.json`。窗口秒数相对已采集 WAV，不伪装成整张唱片的精确时间。不同窗口不自动证明不同歌词；保留伴奏，不称纯净分离人声。现有素材是早期 blues/jazz，不冒称1960年代 soul。

轻量高通/降噪后，用已有 RubberBand 切片链路变调拉伸；10片段拉伸比0.598–1.370，目标拍长误差0帧，边缘淡入淡出。手动选句与编排判断仍由主代理完成，不宣称自动发现最佳乐句。

## 实际验证

- 本轮46项测试通过，24.958秒；包括数字开头的真实来源ID、源身份冲突、过期切片/渲染绑定、MIDI遗漏/重复/力度、重触发与最终工程路径。
- 主混音44.1kHz、双声道、24-bit；17分轨求和相对RMS残差约−118.51dB。
- FFmpeg输入分析：−18.26 LUFS，−1.00 dBTP，LRA 6.10 LU。分析未改变混音，不作好听评分。
- 交付包63文件，其中62项在manifest内逐项复核 SHA；3个来源副本、10个pad、MIDI 76,800 ticks。
- 浏览器实际播放至16.45499秒，readyState=4；HTTP Range返回206、64字节；来源17/17匹配；未写用户笔记或Keep。
- 原15份主混音SHA及mtime保持，源录音及切片SHA保持。
- 通过原生Live文件对话框选中并打开隔离副本后，界面读取连续两次超时。实际打开成功、播放、保存、重开、回渲染及ChopRack加载均未核实。文件/数值验收不等于Live验收。

证据：[真实音频与工程核验](../../evidence/real-record-flip-2026-09-27.json)。本轮未修改核心闭环/导出器，未重跑先前400项全套回归；46项为新增工具的相关验收。

## Coding Plan 与恢复

同一波两路 Claude Code + Coding Plan（doubao-seed-evolving）：`beatlab-record-flip-compose-20260927-a1`、`beatlab-record-flip-delivery-20260927-a1`，均正常结束并采用。只外发必要公开代码与合成测试，真实媒体在本机处理；无按量兜底、无重发。

主代理核验输入/输出哈希、实际渲染和试听入口；修正半拍军鼓位置与五项交付一致性问题，并复用已有Rack构建器。只读复审确认没有剩余阻断项。保留的16项编曲测试与30项交付测试通过；没有用模型自报成功替代实测。

恢复顺序：读取 `.claude/autopilot/real-record-flip.json`；先核对既有作品和包，不覆盖v1。生成工具依次为 `examples.prepare_gold_from_dust` → `examples.real_record_flip` → `pipeline.song` → `examples.real_record_delivery`。新输出目录必须为空；现有v1无需重新生成。

下一步只有一个：等用户对此版本给出时间点反馈，再制作定向子版。自动化闭环的合成技术演示保留，专辑概念与其他未选方向不自动推进。
