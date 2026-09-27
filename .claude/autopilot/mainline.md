# BeatLab 主线

## 目标
最终做用户认可、方便修改且可商业化变现的beats；当前先交约10首有主题关系的真实翻采草稿，用户整体试听给风格量表，再锁定式细化。支持Ableton继续创作；实际出售与发布另验。
验收：有原句/solo/成品可对照；反馈对应明确版本；技术结果有证据、音乐偏好由用户确认。

## 已决策
- D1 2026-09-12：音乐质量 → Ableton 还原 → 素材/商品。
- D2 2026-09-13：完整乐句变调、重排、重复与回应是重点，鼓机练习为辅助；保留认可的 Windowlight / Dust Letters。
- D3 2026-09-15：建立产品调研待办，接入 Notion 并随 Git 推送；白天用户定方向，夜班执行已选的一项。
- D4 2026-09-16：v2 女声仍重复两句；新版本要增加真实不同源唱句并轮换领唱，不能只增加器乐来源或同句变调。
- D5 2026-09-18：用户直接要求查询昨日产出和卖 beats 的闭环；执行 R06 调研，发布、价格、付费和联系买家不在本轮范围。

- D6 2026-09-18：用户明确先做专辑叙事规划，参考 Kanye West / Tyler；商业化晚点讨论。

## 当前任务

最新直接授权 MUSIC-TEN-20260927：约10首工作主题《城市余像》，当前仅01–06已交付50–56秒真实翻采草稿、分轨/ALS/ChopRack和评分页 http://127.0.0.1:8800/；07–10仍失败。两包同波并发2，实际3次CC（含唯一qfix1），试听采用、编排部分采用。qfix1因套餐429退出，恢复时间2026-09-27 21:58:10但不得自动重试；已询问一次额外20分钟续修，尚待用户明确答复。状态ten-beats.json，完整计划2026-09-27-ten-themed-beats.md，结果docs/research/2026-09-27-city-afterimages-result.md。用户Keep为空、Live未验，原17份混音不变；LAB-PILOT暂排后、截止不延长，旧unknown不重发。

以下为此前工程任务历史：

2026-09-27 用户随后直接要求“多让 cc 跑，让它跑跑试试”。LAB-PILOT-20260927首波已实际运行两路CC（996.136秒/675.035秒、最高本项目并发2，实际Seed Evolving/thinking/high），独立原测试43+22通过、真实v2的10检查通过；但两包各4个独立反例失败，均未采用。唯一质量修正包已准备、未提交，等待外部CC空位。原beatlab仍每天22点，prompt已更新并读回；高频阶段未启用。窗口截止2026-10-04 20:24:56北京时间，包含准备、不延长，原配置备份和恢复标志已保存。恢复 `continuous-lab.json`，完整报告 `docs/research/2026-09-27-continuous-lab-pilot-result.md`。

最新2026-09-27：用户要求思考Coding Plan可长期推进的工作与定时复审方式。两路套餐完成文档草稿，经主代理校正，形成持续任务池和7天运行提案；`docs/superpowers/specs/2026-09-27-continuous-lab-design.md`。本轮只规划，试运行未启用，原每日22点调度不变；任务池不是批量实施授权。状态 `continuous-lab.json`。

用户随后反馈“可以更复杂，整体不必都lofi”：v2 Prism Cut已交付，54.174秒/20轨/38次切片触发。保留真实翻采主题，清晰钢琴/颤音琴/拍手与五段发展；原16份混音保持。6项新测试+46项复用工具测试、真实音频/HTTP/浏览器及两版-19.77LUFS匹配对照通过。CC固定a1超时退出143；核验停止与稳定草稿后本地修正采用，保留unknown、没有重发。Live未验，Keep为空。报告 docs/research/2026-09-27-prism-cut-v2-result.md；状态real-record-flip.json的revision。代码/计划/证据e7cc394已普通push并核对远端；Notion已回填待你验收并读回，原每日22点夜班保持、v2恢复入口已同步。

MUSIC-20260927：真实录音翻采《尘里有金》v1已交付，92 BPM / 54.174秒，待用户试听；状态 `real-record-flip.json`。三份录音裁出10片段，17分轨、ALS、ChopRack与原录音对照已生成。两路cc-plan正常结束并采用，46项相关测试及真实音频/HTTP/浏览器通过，旧15份主混音保持。Live打开操作后的界面读取超时，发声/保存/回渲染未验。报告 `docs/research/2026-09-27-real-record-flip-result.md`。Notion https://app.notion.com/p/3e83285284df81408241ff0d6b11bf13 。本轮不代填Keep、不自动扩曲。代码/计划/证据a95d859已普通push并核对远端；Notion已回填待你验收并读回；原每天22点夜班恢复入口已同步且时刻/状态保持。

## 上一阶段：闭环技术交付

WORKFLOW-P0-20260927：用户2026-09-27提供新版PRD并要求按闭环改成自动化工作流。目标为指定段落替换不同内容家族回答句，鼓/Bass/非目标区域锁定；有限候选、真实保护检查、RMS匹配盲听、人选Keep后导出。现有设计已在对话明确，按用户直接实施授权推进；详见docs/superpowers/specs/2026-09-27-locked-workflow-design.md和实施计划。

状态 `.claude/autopilot/locked-workflow.json`：首条闭环技术验收完成，待用户试听。Notion https://app.notion.com/p/3e83285284df81778473f751527bf88d 。两波 4 个 cc-plan 请求均已结束并采用，最大并发 2、无按量、旧 unknown 未重发。400 项回归通过；真实合成音频/CLI/浏览器 Keep 自动导出与刷新通过。原有 15 份主混音保持。正式演示 http://127.0.0.1:8798/ 未代填用户意见；为本地合成人声技术测试，非新专辑成品。

第一波8d9b1f1及完整交付e6b3f9f2f37fcdf38b7545bc022637b46053eda4均已普通push并核对远端一致；Notion任务及项目回填并读回待验收。最终状态见 locked-workflow.json。独立F03维持待定方向。完整结果与边界：docs/research/2026-09-27-locked-workflow-result.md。

## 历史：F03独立对比与F01/F02交付
以下为2026-09-20时间点记录，凭据与F03状态已被上方2026-09-27新事实更新，不作为当前排队或恢复指令。
FEATURE-F03环境阻塞，Notion仍执行中但未交付：2026-09-20夜班完成最新卡片/依赖核验与两路cc-plan派发。两个本地客户端均在凭据helper退出51时结束，未进入模型执行、未改文件、未跑新测试。直接只读凭据检查同样退出51且不可用；具体原因未确认，不推断套餐过期或服务端401。状态与固定请求ID见feature-refinement.json的comparison；恢复计划docs/superpowers/plans/2026-09-20-version-comparison.md。

上一批：
FEATURE-20260920 已完成技术验证并推送，待用户验收：F01时间点笔记、F02实际声部来源展示、必要的媒体Range与路径边界修复。完整272项测试通过；浏览器保存/刷新/跳回1.25秒与2.5秒、真实增益子版均验；16份旧主混音SHA/mtime保持。报告 [feature结果](../../docs/research/2026-09-20-feature-refinement-result.md)，精确恢复状态 [feature-refinement.json](feature-refinement.json)。
Notion本轮 https://app.notion.com/p/3e13285284df81b3acdede2802cddd4f；下一项 FEATURE-F03 https://app.notion.com/p/3e13285284df814a888bcf97283d00b5 已按用户持续细化授权默认排队，前置技术验证和代码推送已满足，执行时仍读取最新状态。
正确工作区 /Volumes/SanDisk2TB/BeatLab/.cache/album-execution-20260919；分支 codex/album-execution-20260919，主根仍旧分支。已把原每日22点beatlab调度改为此恢复入口，没有新增定时器或延长旧4小时窗口。
cc-plan三逻辑包四请求，初波并发2，无按量；笔记包unknown时本地进程已结束，从稳定产物独立验收后采用、未重发；另两报告自列handoff-result导致invalid_output但实际范围干净、独立验证后采用。无活动请求。原ALGO-4H已结束，旧四个unknown不重发。

代码提交5668e9c289e649724dec0bf6913b174cc2ab2890已普通push并核验远端相同；Notion本轮卡已回填报告/验证并读回「待你验收」，F03保持「今晚执行」。Hub恢复入口为 /Volumes/SanDisk2TB/claude-pm-hub/projects/beatlab/latest.md。

F03本地计划提交1946dd96e3d173aa9c1b8e7a52770b618b87838e普通push失败（GitHub凭据不可读取），远端仍760ad831fdaf28dc7d9e7ca17f5d70f2829ebede；前面的已推送结论仅指F01/F02历史成果，不指本轮。

## 历史算法进度（原窗口已结束）
三个opt-in接入已验证，完整184项测试通过。22条素材125→67片段，同源超阈值重叠56→0，全部素材仍有候选、0异常、源SHA/mtime保持；原循环性/综合评分略降，不宣布好听。未改默认选句/编排。证据 `evidence/algorithm-integration-2026-09-20.json`。wave-04已完成：194测试通过；Crazy Blues两版68.326秒实际A/B，v1门长/尾休止/MIDI一致，旧24音频保持。证据 `evidence/algorithm-audio-midi-2026-09-20.json`。

## 前轮创作结果
- E01–E04 技术验收通过：清单含有效人声处理、浮点 premaster、包完整性与恢复校验、两线整合。
- score 声部增益反馈实际生成不可变子版本，固定父版 master gain，保持其他声部；重复请求复用。并非自然语言重切 worker。
- 三段《借来的光》小样：Pocket Sun 80.37s / Applause Machine 82s / No Curtain Call 72.24s，带来源、原句、solo、MIDI、分轨、ALS、ChopRack。整张故事与九曲仍是提案，听感待用户。
- 79 项 unittest、94 条工程断言、真实 SQLite 合成烟测通过；旧音频哈希保持。
- Coding Plan 实际 7 次请求/5 子任务，代码、编曲、审查、报告有采用及拒收记录；没有按量兜底。

## 未完成与边界
Live 界面访问超时，真实打开/发声/重开/回渲染未验。R01–R05 仍待定，R06 保留研究；长期商业目标已按最新用户要求更新，实际出售与发布尚待选择成品；完整自动选好句、自然语言反馈重编与整个PRD范围的恢复仍未完成。Afterglow v3 听感仍待验。原素材/音乐/DB 不入 Git，不合 main，不发布音乐，不为推进工作自动付费。

## 下一步（唯一）
等待用户对剩余四首的一次额外20分钟CC续修决定；获准后冻结前六首输出并补齐，否则保留六首供试听。具体交接和计数见ten-beats.json；不得因额度恢复擅自增加修正次数。

最新 feature 轮次：[2026-09-20 试听反馈与来源](rounds/2026-09-20-feature-refinement.md)。

最近一轮：[2026-09-19 工程与三段小样](rounds/2026-09-19-album-execution.md)。

下载链路最近一轮：[2026-09-19 固定批次采样入库](rounds/2026-09-19-crate-harvest.md)。

算法最近一轮：[2026-09-20 纯算法与只读基线](rounds/2026-09-20-algorithm-pure-modules.md)。

接入最近一轮：[2026-09-20 接入与真实素材对照](rounds/2026-09-20-algorithm-integration.md)。

最新收尾：[算法窗口结束与恢复点](rounds/2026-09-20-algorithm-sprint-close.md)。
