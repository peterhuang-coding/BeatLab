# BeatLab：从素材发现到闭环生成的产品改进文档

BeatLab 应升级为一套**本地优先的 Sample Discovery & Flip System**：持续从合规来源发现和更新素材，把整首音频理解为可采样片段，为一个 Hero Sample 生成 Loop、Chop、Stem 三类可解释 Recipe，产出三个可编辑 beat 候选，再利用保留、淘汰和 Ableton 修改结果优化下一轮选材与编排。**产品北极星不是生成数量，而是候选被保留并继续制作的比例。**

## 1. 产品定位调整

- 当前定位：一条能够从本地音频生成完整 beat 的技术流水线。
- 目标定位：从素材发现、权利记录、片段理解、Sample Flip、Beat 生成、试听反馈到 DAW 交付的个人制作闭环。
- 核心用户：使用 Ableton、偏好采样型 hip-hop、boom-bap、lo-fi、soul sampling 等制作方式的个人制作人。
- 核心任务：用户不需要手工浏览大量素材、逐首拆轨和反复搭建第一版编排；BeatLab 应持续提供少量、来源清晰、制作逻辑明确、值得继续完成的候选。

| 不是 | 而是 |
|---|---|
| 全网音频下载器 | 合规素材 Connector 与可追溯资料库 |
| 随机切片拼接器 | 围绕 Hero Sample 的显式 Sample Flip |
| 文本生成整首歌曲 | 把真实素材发展为可编辑 producer draft |
| 生成一次即结束 | 通过用户反馈持续提高命中率 |
| 替代 Ableton | 把高质量制作决策交给 Ableton 继续完成 |

## 2. 产品闭环

完整链路：**发现素材 → 权利检查 → 下载与去重 → 音频理解 → Sample Moment → Hero Sample → Flip Recipe → Beat 候选 → 试听反馈 → Taste 更新 → 下一轮选材与生成 → Ableton 交付**

**北极星指标：Kept Beat Rate** —— 有至少一个候选被保留并进入 Ableton 的生成任务占比。

首轮建议实验门槛：连续 20 次生成任务中，至少 40% 的任务产生一个被保留候选。该数值是产品实验目标，不是行业基准。

产品闭环必须满足的三个条件：

1. **可解释**：用户知道为什么推荐这段、怎样切、怎样变调和编排。
2. **可复现**：同一 Recipe 可以重建，代码或模型升级后仍能追溯。
3. **可学习**：系统能区分用户不喜欢素材、切法、鼓、Bass 还是结构。

## 3. Goal 1：建立安全、持续更新的素材供给层

目标：mini 持续发现和更新素材，但只把来源、许可和使用边界清楚的资产送入生成链路。

**P0 Feature**

- **Connector Framework**：每个来源使用独立适配器，统一输出 source manifest。首批来源：用户本地目录、明确授权素材目录、Public Domain、CC 或允许下载/API 使用的来源。
- **Incremental Crawl**：基于游标、更新时间或内容哈希只获取新增与变化内容。
- **Crawl Queue**：支持调度、限速、超时、重试、熔断和失败原因。
- **Raw Asset Store**：原始文件只保存一份，以内容哈希寻址。
- **Metadata Normalization**：统一 title、artist、year、genre、source URL、license、duration 等字段。
- **Rights State**：allowed、private_only、needs_review、blocked。
- **Duplicate Detection**：MD5/SHA + Chromaprint 或音频指纹去重。

**关键约束**

- 不绕过 DRM、登录限制或平台下载保护。
- needs_review 素材只能进入私人实验区，不能进入可发布候选。
- crawler 只负责供给，不直接决定素材质量。
- 所有素材必须保留原始来源、采集时间、许可快照和文件哈希。

**验收条件**

- Connector 失败不会阻断其他来源。
- 同一素材来自多个来源时不会重复下载和分析。
- 100% 进入生成池的素材具有明确 rights state。

## 4. Goal 2：把整首素材变成可搜索的 Sample Moments

目标：从"整首歌曲有多少分"升级为"哪 2–16 秒值得采，以及为什么"。

**P0 Feature**

- **Preflight**：格式、响度、静音、损坏文件和时长检查。
- **Global Analysis**：BPM、Key、拍点、downbeat、结构段和动态变化。
- **Stem Cache**：按需生成 vocal、drums、bass、other，避免重复拆轨。
- **Moment Windows**：生成 1/2/4/8 小节以及 phrase-based 候选。
- **Moment Types**：旋律、无鼓旋律、vocal phrase、drum break、bass phrase、texture、transition。
- **Moment Score**：可循环性、记忆点代理、鼓/人声状态、调性稳定、结构位置、音色独特和空间。
- **Diversity Filter**：避免 Top-N 都来自同一首歌或同一种音色。
- **Explainable Recommendation**：为每个 Moment 输出推荐原因和风险。

**P1 Feature**

- 自动识别更完整的起音、尾音和呼吸边界。
- 用历史反馈训练个性化 moment reranker。
- 对相似片段聚类，建立"用户已经听腻"的惩罚机制。
- 建立采样候选 Inbox，支持试听、收藏、屏蔽来源和批量进入生成。

**验收条件**

- 每首合格歌曲至少输出 3 个不同类型的候选 Moment，无法输出时给出原因。
- 用户能够在生成 beat 前先试听并否决片段。
- 能区分"素材本身不好"与"后续生成不好"。

## 5. Goal 3：建立 Hero Sample 与显式 Flip Recipe

目标：每个 beat 围绕一个主采样展开，不再让多个高分素材随机争夺主旋律。

**Hero Sample 规则**

- 一个 beat 只有一个 Hero Sample。
- Supporting Samples 最多 0–2 个，仅用于 texture、vocal accent、transition 或鼓层。
- Hero Sample 的来源、时间区间、stem 和所有变换必须完整记录。

**三个基础 Recipe**

| Recipe | 核心逻辑 | 输出差异 |
|---|---|---|
| Loop | 保持原句，做 BPM/Key 对齐、滤波、dropout 和段落变化 | 最保留原素材情绪 |
| Chop | 按瞬态和 phrase 切分，重新设计重音、呼吸与 syncopation | 变化最大、最体现制作决策 |
| Stem | 只保留目标 stem，再围绕它重新配鼓、Bass 和 texture | 更干净、更容易混音 |

**Recipe Manifest**

- source asset 与精确时间区间。
- stem、切点、pad、MIDI note 和排列。
- BPM、pitch、stretch、reverse、filter、gain、pan。
- 使用的鼓组、Bass 根音和 groove profile。
- pipeline、模型、代码和参数版本。
- deterministic seed。

**验收条件**

- 同一 Hero Sample 的三个 Recipe 具有明显不同的制作逻辑。
- 同一 Recipe 重跑可复现核心排列。
- Recipe 可在 Ableton 中逐轨重建。

## 6. Goal 4：生成围绕采样工作的 Beat，而不是套模板

目标：鼓、Bass 与结构响应 Hero Sample 的重音、空隙、和声与情绪。

**P0 Feature**

- 一次生成三个 60–90 秒候选。
- 基础结构：Intro、Verse、Hook、Verse Variation、Outro。
- Groove Profiles：boom-bap、loose/Dilla、straight、halftime。
- Harmonic Alignment：统一 Key、根音和必要的 pitch shift。
- Sample-aware Drums：根据采样 onset、空隙和 phrase ending 安排 kick/snare。
- Bass from Sample：优先使用 bass stem 或调性结果建立 Bass，不跨调随机生成。
- Section Mutation：通过滤波、mute、chop 密度和辅助素材形成段落变化。

**P1 Feature**

- Best-of-N：后台生成更多内部候选，只展示差异最大的三个。
- Call-and-Response：采样与 snare、vocal、texture 的问答式编排。
- Groove Extraction：从原素材或高评价历史 beat 提取微时序模板。
- Reference Profile：允许用户选择"更松""更碎""更留白"等制作倾向。
- Full Arrangement：用户保留短候选后，再扩写为 3–4 分钟完整结构。

**验收条件**

- 三个候选的差异不是换随机种子，而是 Recipe 与结构逻辑不同。
- 主采样在整段音乐中保持明确主题。
- 鼓重音和 Bass 不与采样发生明显节奏或调性冲突。

## 7. Goal 5：建立试听、反馈和 Taste Loop

目标：用户反馈直接改变下一轮素材排序、Recipe 选择与编排参数。

**Review Feature**

- A/B/C 同屏或连续对比试听。
- Waveform、使用片段、切点和 Recipe 参数展示。
- Keep、Reject、Regenerate、Extend、Export。
- 分维度评分：素材、切法、鼓、Bass、结构、整体。
- 快捷原因：素材没感觉、切得太碎、太像原曲、鼓不对、太规整、没有空间、值得继续。

**Learning Feature**

- Rule Reweighting：第一阶段根据反馈调整现有 rubric 权重。
- Personalized Reranker：对 Sample Moment 和 Recipe 分别排序。
- Negative Memory：记录被屏蔽的来源、音色和重复模式。
- Exploration Budget：保留少量与历史偏好不同的候选，避免口味固化。
- Ableton Outcome：记录候选是否打开、哪些轨道被删除或保留、是否最终归档。

**验收条件**

- 反馈能明确归因到 Moment、Recipe 或 Beat Composer。
- 个性化排序在固定回放集上优于默认排序。
- Kept Beat Rate 随累计反馈提升，而不是只增加生成量。

## 8. Goal 6：完成专业交付、可追溯与可恢复

**DAW 交付**

- preview.wav、full_mix.wav、dry stems。
- 所有使用过的 chop WAV。
- drums、bass、chops、vocal/texture MIDI。
- Ableton Live Set 或稳定拖入目录。
- recipe.json、provenance.json、run_manifest.json。

**可靠性**

- 每个节点可恢复、可重试、可跳过已完成结果。
- 任务状态持久化，进程退出或 MBP 休眠不丢失任务。
- 模型、代码和参数升级不会覆盖旧产物。
- 失败分类：来源失败、权利失败、文件失败、分析失败、生成失败、渲染失败。

**任务状态机**

`discovered → rights_checked → fetched → ingested → analyzed → moments_ready → selected → recipes_ready → generated → reviewed → kept/rejected → exported`

## 9. 双机产品架构

- **mini：常驻控制面和资料面** — Connector 与定时爬取；原始素材、元数据、rights/provenance 与分析缓存；任务队列、状态机、重试和版本；Sample Inbox、候选试听和反馈历史；后续提供 Web UI 与 API。
- **MBP M3 Max：高算力执行面和创作面** — 主动领取 capability 匹配的任务；Stem separation、音频特征和 Moment 分析；Recipe、best-of-N、Beat Composer 与渲染；Ableton 打包、试听和人工精修；完成后把产物和摘要上传到 mini。

**初期技术复杂度控制**

- mini 初期使用单进程 API + SQLite + 内容寻址文件目录。
- 只有 mini 服务写 SQLite，MBP 通过 API 操作。
- 大文件通过对象目录或 HTTP 上传下载，不放进数据库。
- worker 超过一个后再升级 Postgres；不提前引入 Redis、Kubernetes 或微服务。

## 10. 产品页面与操作入口

1. **Sources** — 管理 Connector、抓取状态、rights state、失败原因和来源屏蔽。
2. **Library** — 浏览素材、stem、BPM/Key、标签、相似资产和 provenance。
3. **Sample Inbox** — 试听 Sample Moments，收藏、屏蔽、批量生成或加入未来任务。
4. **Generate** — 选择 Hero Sample、风格与约束；查看 Loop/Chop/Stem Recipe；发起生成。
5. **Review** — 对比三个候选，查看 waveform、来源、Recipe 与分维度反馈。
6. **Projects** — 管理已保留 beat、Ableton 包、版本、修改记录与发布状态。

## 11. 路线图与优先级

| 阶段 | 范围 | 退出条件 |
|---|---|---|
| P0：闭环 MVP | 一个本地目录 Connector、rights/去重、Moment、Hero Sample、三 Recipe、三候选、Review、Ableton 包 | 20 次任务中 ≥40% 至少保留一个候选 |
| P1：质量提升 | 片段个性化排序、sample-aware drums、Bass 对齐、best-of-N、Full Arrangement | 个性化排序和保留率明显优于 P0 |
| P2：双机异步 | mini API/queue/store、MBP worker、任务恢复、远程 Review | MBP 离线不丢任务，上线后自动完成计算 |
| P3：扩大供给 | 多个安全 Connector、定时增量更新、crate 推荐、来源质量模型 | 新增素材提高候选命中率，而非只扩大库存 |

**MoSCoW**

- **Must**：配置化、一个 Connector、权利状态、去重、片段评分、Hero Sample、三 Recipe、三候选、反馈、Recipe/Provenance、Ableton 交付、任务恢复。
- **Should**：stem cache、best-of-N、调性对齐、sample-aware drums、Sample Inbox、分维度反馈、个性化 reranker。
- **Could**：多 Connector、自动 crate、语义搜索、参考曲风、完整 3–4 分钟扩写、移动端 Review。
- **Won't Now**：全网爬虫、多租户 SaaS、自研基础模型、实时生成、自动母带与发行、自动版权承诺。

## 12. 产品指标

| 层级 | 指标 |
|---|---|
| 供给 | Connector 成功率、rights 完整率、去重率、有效新增素材数 |
| 理解 | 每首 Moment 数、Moment 收藏率、Moment 屏蔽率、类型多样性 |
| 生成 | 任务成功率、首次试听时间、三个候选差异度、渲染耗时 |
| 价值 | Kept Beat Rate、Export Rate、Opened in Ableton Rate |
| 学习 | 个性化排序提升、重复负反馈下降、连续使用次数 |
| 可靠性 | 失败恢复率、缓存命中率、重复计算率、产物可重建率 |

## 13. 对现有代码的改造

| 现有模块 | 改进方向 |
|---|---|
| common.py | 移除 Desktop 硬编码；增加 sources、assets、rights、moments、recipes、feedback、jobs、runs 数据模型 |
| ingest.py | 改为 Connector 统一入口；写 provenance、rights 与内容哈希；支持增量摄入 |
| separate.py | 建立 stem cache；按 Moment/Cue 范围处理；输出 phrase 与结构边界 |
| score.py | 拆成 asset quality、moment ranking、recipe prior 三层评分 |
| compose.py | Hero Sample 单主角；Loop/Chop/Stem Recipe；sample-aware drums；best-of-N |
| render.py | 批量渲染三候选与 dry stems；输出完整 manifests；任务可恢复 |
| report.py | 升级为 Review：Moment 与候选试听、A/B/C、分维度反馈、Keep/Reject/Export |

**新增模块**：`connectors/`、`crawler.py`、`library.py`、`moments.py`、`recipes.py`、`feedback.py`；P2 再加 `api.py`、`worker.py`

## 14. 第一开发里程碑

只实现一个本地目录 Connector，使用 20–50 首来源明确的素材完成闭环：

1. 增量扫描、rights 标记和去重。
2. 分析歌曲并输出 Sample Moments。
3. 自动选择一个 Hero Sample。
4. 生成 Loop、Chop、Stem 三个 Recipe。
5. 生成三个 60–90 秒候选。
6. Review 页面完成试听、分维度反馈和 Keep/Reject。
7. 被保留候选导出 WAV、stems、MIDI、recipe、provenance 和 Ableton handoff。
8. 下一次任务使用历史反馈调整 Moment 与 Recipe 排序。

这个里程碑完成前，不增加外部爬虫数量，不建设复杂服务器集群，也不扩展完整歌曲自动化。
