# BeatLab 项目约定

- 2026-09-27 用户随后明确“多让 cc 跑，让它跑跑试试”，批准持续试运行的工程准备：任务队列/恢复与音频正确性基准，验收通过再按原建议启用7天22/00/02执行、09复审。见 `docs/superpowers/plans/2026-09-27-continuous-lab-pilot.md`；范围不自动扩展到全部任务池，音乐Keep与Live另验。状态 `continuous-lab.json` 优先于下方历史提案。

- 2026-09-27 最新用户偏好：后续水下CC质量优先，在已授权且验证的套餐能力内选择适合任务的最高能力模型，显式开启原生最高思考档（当前Seed Evolving为high，传参需实查）。交接白名单内读写与测试预授权；不扩大权限/预算/并发，不为升档重发在途或已结束请求。

- 2026-09-27 用户提出Coding Plan长期水下执行、定时更新与主代理定期review。已形成持续任务池与7天试运行提案，见 `docs/superpowers/specs/2026-09-27-continuous-lab-design.md`；本轮仅方案，未启用新频率或自动批准池内方向。原每日22点夜班与音乐Keep边界保持；不得把提案当执行指令。

- 2026-09-27 用户对《尘里有金》继续要求“更复杂，整体不用都lofi”。v2已生成待试听：保留真实翻采主题，增加清晰钢琴/颤音琴与鼓组、段落反差；不把所有采样作品预设为lofi，不把复杂度等同轨数或噪声。计划 `docs/superpowers/plans/2026-09-27-prism-cut-v2.md`；v1保留。

- 2026-09-27 用户在闭环演示后反馈“有点那个味儿”，但明确下一首要由真实录音自行裁切，想要 Kanye 风格 beats，可将水下工作交 Coding Plan。当前创作任务 MUSIC-20260927 / 尘里有金，计划 `docs/superpowers/plans/2026-09-27-real-record-flip.md`，状态 `real-record-flip.json`。主旋律和人声用可追溯录音翻采；合成声音仍可用于技术测试，但不能把技术演示作为新音乐交付。风格参考不等于使用 Kanye 歌曲或人声；新作品仍待用户试听。

- 2026-09-27 用户直接授权按新版PRD修改为自动化闭环，可调用Claude Code。当前 WORKFLOW-P0-20260927 优先于旧F03独立排队，范围见 `docs/superpowers/plans/2026-09-27-locked-workflow.md` 与 `.claude/autopilot/locked-workflow.json`。首条闭环是指定区间换不同内容家族的回答句，冻结鼓/Bass及非目标区域，有限候选、保护检查、RMS匹配盲听、明确Keep后导出。身份/用途记录不足待确认；不把分数当好听或代填Keep。已有音频分轨ALS不等于可发声MIDI采样器，Live另验。凭据已恢复，旧文档cfa4437已推送；旧unknown不重发。

- 2026-09-20 用户新授权「用 plan 持续细化这里的各种 feature」。恢复执行工作区 `.cache/album-execution-20260919` 的 FEATURE-20260920，状态见 `.claude/autopilot/feature-refinement.json`。当前 cc-plan（claude_plan.py）替代旧的纯文本草稿入口作为默认代码委派；同波独立包最多两路，真实验收后采用。F01时间点反馈/F02来源展示后，默认夜班排队FEATURE-F03版本比较，依赖见Notion卡；F04–F06仍在路线图。旧ALGO-4H窗口保持结束，4个unknown修正不重发，新授权不延长旧期限。


- 2026-09-19 随后用户直接授权 Coding Plan 完成音乐下载/爬虫链路：新增 `crate` 固定批次发现、限量下载、来源校验、去重入库与恢复。命令见 `docs/product/crate-harvest.md`，实测证据 `evidence/crate-harvest-2026-09-19.json`。同一任务独立下游工作默认 `coding_plan_batch.py` 两路套餐并行，重叠文件顺序应用；本轮最后审阅已采用该批次入口。音乐好句选择与整张专辑仍待推进。

- 2026-09-19 用户直接授权 Coding Plan 优先推进创作工程并生成音乐。本轮在 `codex/album-execution-20260919` 整合两线、修复 E01–E03，补 score 声部增益反馈，生成三段《借来的光》试验小样。结果见 `docs/research/2026-09-19-creative-delivery.md`；小样待试听，九曲概念未定稿，不合 main，商业后置。后续恢复先检查该执行分支；主工作区仍停在旧采样分支。
- 成块代码、测试草稿和文档先按 task-tiering 技能交 Coding Plan，主代理负责工具执行与验收。不急的已授权任务以套餐承担主要可委派工作为默认，不因暂时限流或赶交付擅自换高额度执行。模型结果的 verifying 状态不是通过；保留请求、结果、review 和真实测试证据。

- 2026-09-18 最新方向：用户明确先做专辑，商业化晚点讨论；当前请求为复杂、有趣、newschool 的 beats 专辑叙事规划，参考 Kanye West / Tyler, The Creator 的制作方向。当前提案 `docs/superpowers/specs/2026-09-18-borrowed-light-album-proposal.md` 的标题、故事、九曲和时长均待用户讨论，不能记成已批准。创作验收以整张听感、主题发展与段落关系为先，不继续要求先选市场/买家或制作销售两版；R06 保留研究，销售实施后置。

- 用户 2026-09-15 要求建立产品方向调研待办、接入 Notion、随 Git 推送，并采用白天定方向/夜间执行。夜班启动先读 `.claude/autopilot/mainline.md` 与 `docs/product/day-night-workflow.md`，再获取 Notion 最新选择；只执行一项已确认任务，结果、验证与最终 commit 回填。入口与任务编号见 `docs/product/research-backlog.md`、`docs/product/notion-links.json`。
- 完整改进计划：`docs/superpowers/plans/2026-09-12-beatlab-mvp.md`。开始工作先读其中最新状态，进度与验收据实更新。
- 用户 2026-09-12 确认优先顺序：音乐质量 → 能还原作品的 Ableton 工程 → 素材发现与商品包（对话表格 2 → 4 → 5）。先交付一首可试听的完整作品；不能把技术测试通过等同用户喜欢。
- 2026-09-13 用户评价 Windowlight v1“很好”，并要求加入人声采样、编排复杂一些。保留这版认可的和声/律动，增加乐句呼应与段落变化；不要继续把“越少声部越好”当作固定目标。v2 的听感需单独记录。
- 2026-09-13 用户对 v2 继续回复“很好”，询问来源后要求加强 MIDI，让采样成为鼓机演奏练习。后续交付应包含可触发的采样器/Drum Rack、可编辑的 MIDI 乐句和鼓垫映射；仅附带 MIDI 文件不等于可演奏工程。本轮独立练习包见 `docs/superpowers/plans/2026-09-13-midi-drum-practice.md`，Live 实际加载/发声仍待验。
- 2026-09-13 随后用户明确纠正：重点是学习 Kanye 等制作人的乐句翻采，从音乐片段取句、变调、前后切、重复、重排，形成更有趣的主题；采样库应有可改编的音乐乐句。上条“鼓机练习”只是辅助交付，不能再用单音堆叠或练习包替代此目标。Dust Letters 得到用户“很好很好”的反馈；见 `docs/superpowers/plans/2026-09-13-phrase-sample-flip.md`，不自动填写星级或 Keep。
- 2026-09-13 要求：多探索老歌采样、运行爬虫、git push 并沉淀方法。首个真实网络来源为 Citizen DJ 的 Blues/Jazz 官方 WAV 乐句目录；命令与验证见 `docs/superpowers/plans/2026-09-13-old-record-crate.md`。制作方法见 `docs/sampling-playbook.md`。当时未包含定时任务或音乐上架；定时夜班现按上方 2026-09-15 的新增协作约定执行，仍不自动授权音乐平台发布。
- 后续 commit / git push 必须带上完整计划及本轮进度文档。推送前核对 `git status` 与提交文件，避免计划仍留作未跟踪文件。
- 2026-09-16 用户要求 Afterglow「再复杂一点，多几个音乐一起采样」，并询问可售差距。四来源 v2 已保存完整/主歌人声轻版，来源区分两条历史录音与两条 Core Library 音乐 loop，不能统称四首老歌。多源需有主题、回应与和声作用；出售给歌手的验收重点是实际试唱、混音与可授予买家的使用范围，不能以采样数、轨数或 LUFS 代替。见 `docs/superpowers/plans/2026-09-16-afterglow-collage.md`。
- 2026-09-16 用户反馈 v2「大妈一直是这两句」，要求更多人声/采样。此为选句重复的纠正，不能只加器乐或给原句换调/倒放。v3 改为三条含女声录音接力，新增两条录音的 12 个不同窗口，缩减旧主句，增加无歌词回应；听感仍需新版本反馈。见 `docs/superpowers/plans/2026-09-16-afterglow-vocal-relay.md`。
- `library/`、`beats/`、`kit/`、`exports/`、模型、数据库与第三方采样不进 Git。可以提交原创配方、工具代码和来源说明；任何商业素材授权范围必须按实际凭证核验。
- 项目记忆沿用 `/Volumes/SanDisk2TB/claude-pm-hub/projects/beatlab/latest.md` 与 wrapup skill；只恢复本项目相关信息，记忆不产生新授权。
