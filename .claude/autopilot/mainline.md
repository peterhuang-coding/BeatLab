# BeatLab 主线

## 目标
当前先做一张复杂、有趣、偏 newschool 的叙事型 beats 专辑；以乐句翻采、原创旋律、人声角色和段落发展形成整体，支持 Ableton 继续创作。商业化后置。
验收：有原句/solo/成品可对照；反馈对应明确版本；技术结果有证据、音乐偏好由用户确认。

## 已决策
- D1 2026-09-12：音乐质量 → Ableton 还原 → 素材/商品。
- D2 2026-09-13：完整乐句变调、重排、重复与回应是重点，鼓机练习为辅助；保留认可的 Windowlight / Dust Letters。
- D3 2026-09-15：建立产品调研待办，接入 Notion 并随 Git 推送；白天用户定方向，夜班执行已选的一项。
- D4 2026-09-16：v2 女声仍重复两句；新版本要增加真实不同源唱句并轮换领唱，不能只增加器乐来源或同句变调。
- D5 2026-09-18：用户直接要求查询昨日产出和卖 beats 的闭环；执行 R06 调研，发布、价格、付费和联系买家不在本轮范围。

- D6 2026-09-18：用户明确先做专辑叙事规划，参考 Kanye West / Tyler；商业化晚点讨论。

## 当前任务
FEATURE-F03环境阻塞，Notion仍执行中但未交付：2026-09-20夜班完成最新卡片/依赖核验与两路cc-plan派发。两个本地客户端均在凭据helper退出51时结束，未进入模型执行、未改文件、未跑新测试。直接只读凭据检查同样退出51且不可用；具体原因未确认，不推断套餐过期或服务端401。状态与固定请求ID见feature-refinement.json的comparison；恢复计划docs/superpowers/plans/2026-09-20-version-comparison.md。

上一批：
FEATURE-20260920 已完成技术验证并推送，待用户验收：F01时间点笔记、F02实际声部来源展示、必要的媒体Range与路径边界修复。完整272项测试通过；浏览器保存/刷新/跳回1.25秒与2.5秒、真实增益子版均验；16份旧主混音SHA/mtime保持。报告 [feature结果](../../docs/research/2026-09-20-feature-refinement-result.md)，精确恢复状态 [feature-refinement.json](feature-refinement.json)。
Notion本轮 https://app.notion.com/p/3e13285284df81b3acdede2802cddd4f；下一项 FEATURE-F03 https://app.notion.com/p/3e13285284df814a888bcf97283d00b5 已按用户持续细化授权默认排队，前置技术验证和代码推送已满足，执行时仍读取最新状态。
正确工作区 /Volumes/SanDisk2TB/BeatLab/.cache/album-execution-20260919；分支 codex/album-execution-20260919，主根仍旧分支。已把原每日22点beatlab调度改为此恢复入口，没有新增定时器或延长旧4小时窗口。
cc-plan三逻辑包四请求，初波并发2，无按量；笔记包unknown时本地进程已结束，从稳定产物独立验收后采用、未重发；另两报告自列handoff-result导致invalid_output但实际范围干净、独立验证后采用。无活动请求。原ALGO-4H已结束，旧四个unknown不重发。

代码提交5668e9c289e649724dec0bf6913b174cc2ab2890已普通push并核验远端相同；Notion本轮卡已回填报告/验证并读回「待你验收」，F03保持「今晚执行」。Hub恢复入口为 /Volumes/SanDisk2TB/claude-pm-hub/projects/beatlab/latest.md。

F03本地计划提交1946dd96e3d173aa9c1b8e7a52770b618b87838e普通push失败（GitHub凭据不可读取），远端仍760ad831fdaf28dc7d9e7ca17f5d70f2829ebede；前面的已推送结论仅指F01/F02历史成果，不指本轮。

## 当前算法进度
三个opt-in接入已验证，完整184项测试通过。22条素材125→67片段，同源超阈值重叠56→0，全部素材仍有候选、0异常、源SHA/mtime保持；原循环性/综合评分略降，不宣布好听。未改默认选句/编排。证据 `evidence/algorithm-integration-2026-09-20.json`。wave-04已完成：194测试通过；Crazy Blues两版68.326秒实际A/B，v1门长/尾休止/MIDI一致，旧24音频保持。证据 `evidence/algorithm-audio-midi-2026-09-20.json`。

## 前轮创作结果
- E01–E04 技术验收通过：清单含有效人声处理、浮点 premaster、包完整性与恢复校验、两线整合。
- score 声部增益反馈实际生成不可变子版本，固定父版 master gain，保持其他声部；重复请求复用。并非自然语言重切 worker。
- 三段《借来的光》小样：Pocket Sun 80.37s / Applause Machine 82s / No Curtain Call 72.24s，带来源、原句、solo、MIDI、分轨、ALS、ChopRack。整张故事与九曲仍是提案，听感待用户。
- 79 项 unittest、94 条工程断言、真实 SQLite 合成烟测通过；旧音频哈希保持。
- Coding Plan 实际 7 次请求/5 子任务，代码、编曲、审查、报告有采用及拒收记录；没有按量兜底。

## 未完成与边界
Live 界面访问超时，真实打开/发声/重开/回渲染未验。R01–R05 仍待定，R06 保留研究且商业后置；完整自动选好句、自然语言反馈重编与全链路恢复仍未完成。Afterglow v3 听感仍待验。原素材/音乐/DB 不入 Git，不合 main，不发布音乐，不为推进工作自动付费。

## 下一步（唯一）
先恢复本机Coding Plan和GitHub凭据读取，正常push保存的本地提交并核验远端，再在原F03逻辑任务限次内继续；阻塞未变不重派，旧unknown仍不重发。

最新 feature 轮次：[2026-09-20 试听反馈与来源](rounds/2026-09-20-feature-refinement.md)。

最近一轮：[2026-09-19 工程与三段小样](rounds/2026-09-19-album-execution.md)。

下载链路最近一轮：[2026-09-19 固定批次采样入库](rounds/2026-09-19-crate-harvest.md)。

算法最近一轮：[2026-09-20 纯算法与只读基线](rounds/2026-09-20-algorithm-pure-modules.md)。

接入最近一轮：[2026-09-20 接入与真实素材对照](rounds/2026-09-20-algorithm-integration.md)。

最新收尾：[算法窗口结束与恢复点](rounds/2026-09-20-algorithm-sprint-close.md)。
