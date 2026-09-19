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
ALGO-4H-20260919授权窗口已结束，已验证成果待用户试听；没有待自动恢复的算法执行项。固定截止北京时间9月20日03:56:20，最后4个修正请求unknown，未重发。恢复时已超过截止，原beatlab每日22点ACTIVE调度现已逐项恢复核验，state.automation_restore_required=false。不能将窗口结束等同全部算法完成，也不延长窗口。
执行分支codex/album-execution-20260919，工作区/Volumes/SanDisk2TB/BeatLab/.cache/album-execution-20260919；主根仍旧音乐分支。见[收尾报告](../../docs/research/2026-09-20-algorithm-sprint-result.md)和[精确状态](algorithm-sprint.json)。

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
等待用户试听并选择新的明确范围。原每日夜班不得自动继续已到期ALGO-4H；第五波草稿和4个unknown请求保留，不重新提交。

最近一轮：[2026-09-19 工程与三段小样](rounds/2026-09-19-album-execution.md)。

下载链路最近一轮：[2026-09-19 固定批次采样入库](rounds/2026-09-19-crate-harvest.md)。

算法最近一轮：[2026-09-20 纯算法与只读基线](rounds/2026-09-20-algorithm-pure-modules.md)。

接入最近一轮：[2026-09-20 接入与真实素材对照](rounds/2026-09-20-algorithm-integration.md)。

最新收尾：[算法窗口结束与恢复点](rounds/2026-09-20-algorithm-sprint-close.md)。
