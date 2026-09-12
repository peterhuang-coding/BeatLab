# Windowlight MIDI Drum Practice Implementation Plan

**Goal:** 把用户已认可的 v2 鼓与人声变成可演奏的 16-pad Drum Rack，交付 88 BPM 的 MIDI 练习乐句。

**后续范围纠正（2026-09-13）：** 用户澄清真正重点是从完整音乐乐句进行翻采、变调、重排与重复。此练习包保留为辅助工具；当前音乐主线改为 [Phrase Sample Flip](2026-09-13-phrase-sample-flip.md)，不因练习包存在即认为采样创作目标已满足。

**Architecture:** 独立私人练习包保留原歌。6 个鼓垫和 10 个已调音的人声鼓垫引用逐项可追溯的 WAV。MIDI 控制落点、时值和力度；闭/开镲互斥、人声切片互斥。使用本机 Core Library 的真实 ADG 结构，最终由 Live 实际加载、保存工程。

**Tech Stack:** 当前 Python / mido / soundfile / scipy；Ableton Live 12 Drum Rack + Simpler。

用户 2026-09-13：“完了是不是再加一些 midi 的使用，感觉采样也可以更像是一种那种 鼓机的实践”。按已有制作授权在当前功能分支继续，保留现有未提交修改。没有新采购、立即 push 或素材发布。

方案取舍：只有 MIDI 文件不能直接发声；16-pad 可演奏采样包是本次范围；整首所有乐器逐项还原成 MIDI 音源留在后续阶段。

- [x] 编写 `tests/test_drum_practice.py`，验证固定 16-pad 映射、4 段 MIDI 的完整 8 小节长度、力度变化、未知音符拒绝。
- [x] 编写 `examples/windowlight_drum_practice.py`，生成可追溯音色、真实 Drum Rack 预设、4 个独立循环和 32 小节练习 MIDI、鼓垫表和使用说明。输出到 `exports/windowlight-midi-practice`；音色不进 Git。
- [ ] 在 Live 打开独立练习工程，装载 Drum Rack 与 MIDI，验证鼓垫映射、媒体装载和发声，保存工程；不能用 XML 结构检查代替实际播放验证。
- [x] 更新总计划、用户偏好和 Hub，保存本轮可复验证据，后续随代码提交计划。

验证命令：`.venv/bin/python -m unittest discover -s tests -p 'test_drum_practice.py' -v`；生成包后用 mido 重读每个 MIDI，实际解析 ADG 的 16 个接收音符和 WAV 文件引用，核验 v2 母版哈希未变。Live 发声和原歌音色相等是不同验收；本次不承诺逐样本一致。

实际进度：3 个新增测试先红后绿。5 个 MIDI 共包含 4 × 8 小节乐句和 32 小节串联，分别 96/136/156/172 个事件，串联 560 个事件。ALS 含 1 MidiTrack、16 Simplers、4 MidiClip、560 个音符和 16 个实际媒体引用；预设与工程只引用本包媒体，16 个 WAV 哈希匹配。试听 88.273 秒，由实际 MIDI 和本包采样经 Python 合成，峰值 -11.70 dBFS；不是 Live 回渲染。

GUI 限制：本轮首次能读取当前 v2 音频工程的 12 轨、88 BPM。随后打开新练习工程的 Go to Folder 对话框没有响应多次 Return/打开/关闭动作，重连后仍停在弹层；因此练习工程的 Live 加载/发声未验收，未强制退出用户应用或修改音频设置。已按本机原生模板写入实际采样器和 MIDI，未保留空模板冒充完成工程。lesson 仅提供 XML 容器结构，原教学音符、采样、音源及效果全部替换。

最终验证：全套 54/54 测试通过（4.392 秒）；首次全套暴露平铺 pipeline 导入模式与包导入模式冲突，修复后重跑通过。失败/成功日志分别保存于 `.cache/windowlight-midi-tests-before-import-fix.log` 与 `.cache/windowlight-midi-tests.log`。交付证据：`evidence/windowlight-midi-practice.json`。GUI 验收项保持未勾选。
