# BeatLab 产品与开发路线图

## 1. 核心价值

BeatLab 当前最有价值的切入层不是“再做一个文本生成音乐模型”，而是采样制作漏斗中的中段：把大量真实音源变成少量值得继续制作的 Sample Flip，并把每个制作决策交付成可追溯、可修改的工程数据。

```text
素材供给 → 片段发现 → Sample Flip → Beat 编排 → DAW 精修 → 成品
             └──────── BeatLab 的核心价值区 ────────┘
```

产品北极星是 Kept Beat Rate：一次生成任务中，至少一个候选被保留并进入后续制作的比例。生成数量、自动评分或音频指标不能替代真实保留行为。

## 2. 2026-09-14 已完成

- 本地目录 Connector、增量摄入、内容去重、rights/provenance 记录。
- 拆轨缓存、Sample Moment 分析、Hero Sample 与最多两个 supporting samples。
- Loop、Chop、Stem 三类显式 Recipe 和 60–90 秒三候选。
- Sample-aware drums、同调 Bass、段落 mutation、dry stems 与 Review 反馈。
- 版本化 `arrangement.json`：试听渲染与工程导出共用同一事件时间线。
- 稳定 `track_id`、`clip_id`、`asset_id`；音频区间以 source frame 保存，时间线以 beat 保存。
- 每个鼓事件锁定实际使用的 one-shot；不在渲染阶段再次随机选 kit。
- 每个 Audio Clip 物化为独立 processed WAV，同时保留原始素材和操作链。
- 自包含 `project/`：Original、Processed、DrumKit、MIDI、reference、Recipe、Provenance 和哈希 manifest。
- 参考混音轨默认静音；工程包移动目录后可重新校验所有引用。
- `pipeline.py all` 已补齐 `moments` 阶段。
- Keep、Export 与 Review 镜像复制完整工程包，不再遗漏 chops、Recipe、Provenance 或 arrangement。
- 停用“只复制模板并改 BPM”的伪 `.als`；未完成 Live 12 实机验证时明确标记为未验证。

## 3. P0 剩余阻塞项

### 3.1 MBP Live 12 导入器

目标：把 `project/arrangement.json` 转换成用户打开即可编辑的 Live Set。

- 创建 Sample Chops、Vocal/Texture、Drums、Bass 和静音 Reference 五类轨道。
- 每个 sample event 生成独立 Arrangement Audio Clip，可移动、裁剪和替换。
- 按 `device_binding.mappings` 建立 Drum Rack 与 pad 音色映射。
- 为 Bass MIDI 绑定经过验证的 Live 原生音源或随工程打包的设备预设。
- 写入段落 Locator；只实现已经验证的 gain、pan、mute 和滤波自动化。
- 保存后重新打开，完整回放并检查 Missing Media、Clip 位置、MIDI 音符与音色。
- 将验证证据写回 manifest；禁止仅凭 XML 生成成功就标记为已验证。

依赖：一份由目标 Ableton Live 12 创建并保存的最小模板，以及 MBP 上的实机验收。Linux 无法关闭这一项。

### 3.2 局部重做

目标：保留满意部分，只重做指定轨道或小节，而不是重新随机生成整首。

建议命令契约：

```text
pipeline.py revise <run_id> <candidate>
  --bars 9:16
  --tracks bass
  --instruction "bass 更少"
```

首版只支持结构化操作：增减密度、静音、增益变化、重新生成指定轨道。新版本不得覆盖原候选，未选轨道的事件哈希必须保持一致。自然语言只负责映射到受支持操作，不承诺任意编辑。

### 3.3 真实素材 Pilot

- 使用 20–50 首来源明确的素材运行至少 20 个任务。
- 每次只展示三个候选并记录 Keep、Reject、Export、Opened in Ableton。
- 区分“素材不行、切法不行、鼓不行、Bass 不行、结构不行”。
- 达到至少 40% 的任务保留一个候选，才认为闭环 MVP 初步成立；该数值是内部实验门槛，不是行业基准。

## 4. 提升“好听”的待验证功能

以下方向有产品价值，但目前没有 BeatLab 盲听数据证明一定提升听感，必须通过同素材、同候选池的对照实验判断。

| 假设 | 最小实现 | 验证方法 |
|---|---|---|
| 多候选再筛选优于直接出三个 | 内部生成 8–16 个，只展示差异最大的三个 | 盲听比较 Keep Rate 与首选率 |
| Critic 能提高排序质量 | 只排序，不修改音频 | 与随机顺序和规则排序做同池对照 |
| Call-and-Response 更有制作感 | 识别采样重音与空隙，限制鼓/人声回答位置 | 对比结构、切法和整体评分 |
| Groove Extraction 比固定模板自然 | 从 Hero 或被保留工程提取微时序 | 比较“太规整”负反馈率 |
| 自动谐波对齐减少冲突 | 对 supporting、Bass 做 key confidence 门控 | 比较 Bass/整体评分与人工修正次数 |
| Negative Memory 减少审美疲劳 | 对重复来源、音色簇和切法模式降权 | 比较重复负反馈和连续使用次数 |

自动音频指标只能用于发现削波、静音、响度异常和明显结构错误；不能用来宣称音乐“更好听”。最终判断必须来自盲听和真实继续制作行为。

## 5. 双机分工

### mini：常驻控制面与资料面

- 定时运行 Connector、抓取元数据、rights 检查、去重和失败重试。
- 保存原始素材、分析缓存、Recipe、arrangement、反馈和版本历史。
- 维护任务队列、租约、幂等键、能力要求和产物哈希。
- 提供 Sample Inbox、Review、反馈和任务状态 API。
- 只由 mini 写主数据库；首版 SQLite 单写者足够，出现多个并发 worker 后再迁移 Postgres。

### MBP M3 Max：高算力执行面与创作面

- 主动领取符合本机能力的任务，不由 mini 直接控制桌面进程。
- 执行 stem separation、Moment 分析、Recipe/编排、候选渲染和工程打包。
- 运行 Ableton Live 12 导入器与实机验证。
- 上传产物、校验和、日志摘要与验证状态；素材大文件不写进数据库。

### 最小任务协议

每个任务至少包含 `job_id`、`run_id`、输入 asset hash、pipeline version、required_capabilities、lease、attempt 和输出 manifest。MBP 断线后任务租约可回收；相同幂等键不得重复发布两份最终产物。

## 6. 暂不做

- 全网无边界爬虫、绕过 DRM 或登录下载限制。
- 多租户 SaaS、Redis、Kubernetes 或提前拆微服务。
- 自研基础音乐生成模型、实时生成、自动母带和自动发行。
- 在没有真实听评数据时，把 Critic 分数或音频指标包装成“好听度”。
- 在没有 Live 12 实机证据时，宣称 Ableton 工程已经可用。

## 7. 下一迭代退出条件

1. MBP 能从 `arrangement.json` 稳定生成 Live 12 工程。
2. 三种候选都能看到独立采样 Clip、Drum Rack、Bass MIDI 和段落 Locator。
3. 工程移动后打开无 Missing Media，参考混音默认静音。
4. 可选择轨道与小节做一次局部重做，未选内容哈希不变。
5. 完成首批盲听记录，不以开发者主观试听代替结论。
