# BeatLab feature 细化执行计划 · 2026-09-20

用户本轮直接要求「用 plan 持续细化这里的各种 feature」。本轮从已有音乐反馈和来源追溯需求开始，cc-plan 两路实现、主代理验收，之后按已验证依赖继续完善路线图。不是恢复已到期的 ALGO-4H，旧 unknown 请求不重发。

## 本轮设计与范围

- F01 时间点反馈：在 score 试听页记录当前秒数、问题类别和原话，绑定 run_id + 实际音频 SHA256；SQLite 追加保留多条意见、请求重试幂等、音频改变则拒绝旧版本反馈。历史条目可跳回该秒。暂不将自然语言直接翻译成编曲动作。
- F02 来源追溯：只展示当前 manifest 实际使用的声部，根据 source-provenance.json 匹配已记录来源、SHA、许可说明；缺失或有歧义明确显示。只读、无网络，不由出处推断商用授权。
- 两项共用现有本地 Review 服务，保留声部增益反馈与已认可作品；测试使用临时 WAV/DB。
- F03–F06 在路线图中细化版本比较、手动选句、编排迭代、Live 验证的接口、依赖与验收，未实现不得写成已完成。商业后置。

## 委派

固定波次 `.cache/feature-refinement-20260920/wave-01`；两个独立 cc-plan 进程，各自复制白名单输入到独立暂存目录，套餐模型 doubao-seed-evolving。每包最多1200秒/50轮，非按量兜底。

1. `beatlab-feature-notes-20260920-a1`：listening_notes、feedback HTTP、song_review、测试与使用文档。
2. `beatlab-feature-provenance-20260920-a1`：song_provenance、测试与后续 feature 路线图；不改 song_review，由主代理串行接入。

原项目输入哈希一致后才能采用；verifying 只是待验收。先收齐本波，不开重叠批次。unknown 核查不重发，每逻辑任务质量修正最多一次。

## 验收

- [x] 两个真实 CC 结果、工具日志、允许文件范围与源输入哈希核验。
- [x] 时间点保存/刷新/多条意见/重试/冲突/旧版本/异常时间/路径逃逸的真实 HTTP 测试。
- [x] 来源匹配/歧义/缺失/恶意 HTML 与链接/不读取外部来源文件的测试。
- [x] 合并接入后完整 unittest、浏览器实际填写保存刷新和跳转。
- [x] 已有正式音乐 SHA 与 mtime 保持；新功能不改变旧歌。
- [x] 更新完整 MVP、路线图、主线与轮次，代码5668e9c普通push并核验远端SHA；Notion结果和状态回填已读回。
- Hub收尾入口：`/Volumes/SanDisk2TB/claude-pm-hub/projects/beatlab/latest.md`，由wrapup保存最终提交与方法，恢复时用Git现状校准。

工作区 `/Volumes/SanDisk2TB/BeatLab/.cache/album-execution-20260919`，分支 `codex/album-execution-20260919`，起点3993403。原每日22点调度保持；本次不新增定时器、不延长旧4小时窗口。

## 浏览器验收发现与有界修正

F01 19项真实测试通过但 worker 在交接报告超时（1200秒，unknown）。主代理确认本地进程结束、源输入哈希一致、5份输出稳定后独立重跑19项通过，采用产物并局部修正超大整数时间400及表单位置；没有重发F01请求。
F02 首轮25项通过，但同ID/不同哈希被误计为匹配；已交同一逻辑任务唯一一次定向修正 a2，并纠正未唯一匹配≠未使用、路线图对F01存储的猜测。
浏览器实际发现：4秒音频已buffer完，但seekable范围[0,0]，点1.25秒笔记回0；HTTP Range请求始终200返回全文件。另以隔离合成文件复现 /media/%2e%2e/ 可越出beats。新增独立媒体传输任务 `beatlab-media-range-20260920-a1`，修改反馈媒体GET的分段响应、流式读取和路径边界；先收齐当前修正再派发，不与原unknown笔记任务重发混淆。

## 持续执行

沿用原每天22点的 beatlab 夜班，已把正确执行工作区、cc-plan入口与旧算法窗口结束边界写入调度。下一项 FEATURE-F03 父版/反馈版对照试听已按用户持续细化授权默认排队，必须等F01/F02与实际音频跳转技术通过并推送后开始。其余F04–F06保留路线图，不自动批准商业实施或复制旧unknown算法请求。

技术验收：新增媒体包已结束并采用，29项HTTP；完整272项通过，浏览器跳转、笔记持久化和旧增益反馈通过，16份主混音SHA/mtime保持。交接报告状态与实际采用区别见 [结果](../../research/2026-09-20-feature-refinement-result.md)。
