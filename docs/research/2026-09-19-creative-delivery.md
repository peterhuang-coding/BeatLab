# BeatLab 创意交付报告

## 变更
- 分支 `codex/album-execution-20260919` 在隔离 worktree 中整合采样分支 `72fc1f5` 与 main `3d333fe`，未合入 main。
- E01：将实际人声 gain×1.5、有效增益上限和非拉伸唱句的 8 秒截断写入清单；拉伸乐句保持声明时长，处理片段保存浮点 WAV。
- E02：FLOAT stems 与 premaster 保留高于 1 的峰值。
- E03：元数据/manifest 覆盖大小、哈希、解码后有限音频/MIDI、安全路径、迁移包校验与发布前 gate。
- 歌曲反馈：按指定声部的 dB 改变量生成独立子版本；固定父版总线增益，避免自动归一化抵消“轻一点”；其他声部和父版保留，重复请求复用结果。无变化、来源变更、轨道清单不一致明确失败。浏览器和 CLI 均可使用。

## 证据
- 音频 stems 求和残差低于 -117dB；既有音频 SHA 保持稳定。
- 最终通过：79 项 unittest、94 条工程验收断言、真实 SQLite 合成音频烟测。损坏包恢复检查也已覆盖；拆轨模型未重跑。
- Coding Plan 共 7 次调用、5 个子任务：2 次完整性草案未直接采用代码，保留检查清单；2 次编曲草案部分采用；1 次反馈函数修正后采用；1 次审阅抓到清单轨道遗漏；本报告草稿也由套餐整理后校正。没有按量 API 兜底，不宣称节省百分比。记录见 [委派证据](../../evidence/coding-plan-2026-09-19.json)。

## 音乐
- Pocket Sun：98 BPM，32 bars，80.37 秒，25 stems，14 slices。
- Applause Machine：144 BPM，48 bars，82 秒，26 stems，16 slices。
- No Curtain Call：82 BPM，24 bars，72.24 秒，22 stems，15 slices。
- 先用已归档 Ableton 音色写出原创 8 小节器乐乐句，再剪切、重排、倒放与变八度；不是从三首现成歌曲直接拼接。
- Oh/Ah/choir 使用无词采样库；未新录歌词，也未下载老唱片素材。
- 每项含来源乐句、solo、stem 音频、MIDI、归集 ALS、ChopRack。

## 限制
- Live 界面访问超时；实际打开、发声、保存重开与回渲染未验。浏览器页面、音频文件与生成反馈版 HTTP 流程已验；主观音乐听感等待用户。
- 未发布、未采购、未上线商业流程。
- 不是完整自动作曲或自然语言重切 worker；自动挖好句、全局下拍校准、模型比较和 DJ 软件导入仍待推进。没有把这次技术验证当作整张专辑完成。

## 本机入口与复现

三首媒体均在 `/Volumes/SanDisk2TB/BeatLab/beats/borrowed-light-<slug>-20260919-v1/`，slug 为 `pocket-sun`、`applause-machine`、`no-curtain-call`。
同名 `exports/` 目录含 `AbletonProject/<slug>.als` 和 `ChopRack/Phrase Pads.adg`。每个 `beats/` 目录还有 `original-phrase.wav`、`sample-flip-solo.wav`、`slice-map.json`、`source-provenance.json` 和 `validation.json`。

```bash
# 从执行工作区使用共享 Python 环境，数据保留在主项目目录。
BEATLAB_ROOT=/Volumes/SanDisk2TB/BeatLab /Volumes/SanDisk2TB/BeatLab/.venv/bin/python pipeline/feedback.py serve --port 8796
# 打开 http://127.0.0.1:8796/，选择新作品；按声部调低后生成子版本。
# 配方使用新目录，拒绝覆盖已生成版本。
/Volumes/SanDisk2TB/BeatLab/.venv/bin/python -m examples.borrowed_light_demos --root /Volumes/SanDisk2TB/BeatLab
```

反馈版本会落盘 score、音频、分轨和 MIDI；ALS 仍需再运行 `ableton_export`，不会把父版 ALS 冒充为新混音工程。
完整音频证据见 [三段小样验证](../../evidence/borrowed-light-demos-2026-09-19.json)。下一步：试听三段，选择一段扩为完整曲。
