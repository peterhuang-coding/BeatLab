# Windowlight 人声与编排修订

**Goal:** 按用户“很好，还需要一些人声采样啥的，复杂点”的反馈，在保留 v1 音乐方向的基础上交付人声版 v2。

**Architecture:** 复用现有 song → ableton_export → package。只新增谱面与素材归档，不修改渲染器、数据库或自动反馈 worker。原版音频不变，新版另存并记录父版本哈希与用户原话。

**Tech Stack:** 本机 Ableton Core Library one-shots、现有 Python/NumPy/soundfile、ffmpeg 与 Live 音频工程导出器。

用户已明确授权本次修订。异步人声偏好问题为可选；默认 Soul 风格无词人声切片。比较过只加喊声、整段主唱、旋律人声切片三种做法，本次选旋律切片与呼应，兼顾原曲旋律辨识度和新增变化。

## 编排设计

- 保留 88 BPM、52 小节、原和声、Bass 和主鼓律动。
- 选 Vocal BNYX Oh Ab、Vocal BNYX Ah B 作为切片声源，音区以 G3–E4 为主；少量 Choir Pure C4 放在桥段/尾声作和声。
- 副歌人声在前半句，钢琴在后半句回应；第二次副歌增加回应和节奏变化。
- 主歌只在句尾出现短人声；前奏预告主题，桥段形成较长的人声和声。
- Shaker、Rim、开放 Hat 只在指定段落补节奏；四/八小节边界补鼓花，段落交接允许短留白。
- 用同响度的 v1/v2 副歌做 A/B，避免更响造成误判。完整新版、独立人声试听与变化记录一起交付。

## 执行与验收

- [x] 新增 examples/windowlight_vocal_score.py：读取 v1 已落盘 score.collected.json；核验父音频 SHA-256；复制新增原料到 library/instruments/windowlight-v2；输出 windowlight-vocal.json 与来源 catalog。它是原创编排脚本，不是通用自动作曲模型。
- [x] `./beatlab song --score examples/windowlight-vocal.json --out beats/windowlight-v2`，保存父版本、反馈与实际修改到新版 manifest。
- [x] 运行现有 validate_song，检查新声部非静音、起点、总时长、MIDI 与分轨相加；ffmpeg 测 LUFS/真峰值。验证 v1 哈希不变。
- [x] 输出副歌同响度 A/B 和独立人声试听，再将 v2 用已有命令导出到 exports/windowlight-v2/AbletonProject 与 ReleaseDraft。
- [x] 更新总计划与执行记录；Hub 同步见对应项目 latest.md。本次不提交/推送，不把新增正面反馈计为新的数值评分或自动 Keep；v2 听感仍由用户评审。

Live 载入后界面连接和回渲染是前轮已知待验项，本轮导出结果不自动提升这一状态。网络来源自动发现仍在后续总计划内。

## 实际交付与证据

12 轨、1,202 个事件，143.818 秒。除原来的 6 轨外新增 Oh 切片、Ah 回应、Choir 和声、Shaker、Rim、开放 Hat。副歌前半句钢琴让给人声、后半句保留回答；主歌只加句尾点缀，桥段/尾声增加和声。新增采样取自本机 Core Library 非 Demo one-shot，已归档 2TB 并保留来源哈希；商业状态仍 needs_review。

- 完整版：`beats/windowlight-v2/full_mix.wav`。
- 独立人声：`beats/windowlight-v2/vocals_only.wav`，保持实际混音中的增益。
- 音量匹配副歌：同目录 `windowlight-v1-hook-matched.wav`、`windowlight-v2-hook-matched.wav`，实测均 -14.9 LUFS。
- 新版 mix SHA-256：`f339d0743e5f8f0f5d76920fdc0339babd546e9cbc1b0146e096a1562a1e80b7`。
- 整曲综合响度 -15.0 LUFS、真峰值约 -1.0 dBFS。12 轨求和与试听峰值残差 `9.5367431640625e-7`、相对 RMS `-118.772 dB`，通过交付校验。
- ALS 实查 12 AudioClips，全部 0 起点、Warp Off；项目内 12 音频文件哈希匹配。未在 Live 打开/回渲染 v2。
- 商品包 20 文件哈希通过、ZIP 12 个成员匹配原分轨、master 与试听一致；v1 音频哈希保持不变。
- 本轮没有修改生产渲染/导出模块，也没有新跑前轮 51 项单元测试；使用实际 v2 音乐执行了渲染、完整性校验和打包。
- 证据：`evidence/windowlight-feedback-2026-09-13.json`、`windowlight-v2-validation.json`、`windowlight-v2-listening.json`、`windowlight-v2-delivery.json`。

用户 v1 正面反馈已记录；v2 的人声选择和丰富程度尚待用户实际反馈，未自行填写星级、Keep 或“更好听”的结论。
