# 锁定人声替换工作流（locked workflow）CLI 指南

本工作流是一个**本地、可恢复、有界**的命令行编排层，串联三个既有引擎：

- `locked_edit`：确定性的分轨拼接（stem-splice）锁定人声替换；
- `blind_review`：本地盲听评审页面与不可更改的人工决定；
- `ableton_export`：把最终选定版本收集为 Ableton Live 12 音频 Set。

编排层（`pipeline/creative_workflow.py`）只负责：提交校验、哈希绑定、有界尝试、
作业锁、人工评审闸门和交付校验。它**不会**自动生成保留决定、品味评分、歌词识别、
LUFS 响度结论或 Ableton Live 验证结论。

运行时不会调用任何额外模型、付费 API，不访问网络（评审服务仅绑定 `127.0.0.1`）。

---

## 1. 请求 schema（version 1）

提交文件是一个 JSON 对象，顶层**只允许**以下 6 个键：

| 键 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `version` | 整数 | 是 | 必须为 `1` |
| `request_id` | 字符串 | 是 | 安全 slug，匹配 `[a-z][a-z0-9_-]{0,63}` |
| `parent` | 字符串 | 是 | 父歌曲目录的**绝对路径**（不可变工程，含 score.json、run_manifest.json、full_mix.wav、stems 与 midi） |
| `edit` | 对象 | 是 | 锁定编辑描述，交给 `locked_edit.validate_edit` |
| `phrases` | 数组 | 是 | 候选乐句对象列表，非空，交给 `render_locked_edit` |
| `budget` | 对象 | 否 | 预算，省略时使用默认值（见下） |

### 1.1 `edit` 对象

| 字段 | 要求 |
|---|---|
| `track_id` | 匹配 `[a-z][a-z0-9_-]*`，必须是父歌曲中的人声轨（不可为鼓/贝斯） |
| `start_beat` / `end_beat` | 有限、非布尔数字；`0 <= start < end <= 小节数*4`（不含渲染尾巴） |
| `expected_parent_mix_sha256` | 64 位十六进制；提交时必须与当前父歌曲 `full_mix.wav` 的实际哈希一致 |
| `current_family_id` | 非空字符串，当前人声音频的家族标识 |
| `identity_verified` | 进入生成必须为显式 `true`；未确认的素材记失败，全部未确认则待确认 |
| `purpose` | 非空字符串，本次替换的用途 |

### 1.2 `phrases` 数组中每个对象

| 字段 | 要求 |
|---|---|
| `id` | 匹配 `[a-z][a-z0-9_-]*`，列表内不可重复 |
| `path` | 乐句 WAV 的**绝对路径** |
| `sha256` | 64 位十六进制；必须与实际字节一致；缺失/不匹配的来源独立失败，其他来源仍可执行 |
| `family_id` | 非空字符串；各乐句的家族标识**不可重复**；且不可等于 `edit.current_family_id`（由引擎在尝试时判定） |
| `identity_verified` | 进入生成必须为显式 `true`；未确认的素材记失败，全部未确认则待确认 |
| `allowed_uses` | 字符串数组；必须包含 `edit.purpose`，否则该乐句尝试失败 |
| `root_midi` | 可选，`[0,127]` 整数（布尔值拒绝），默认 60 |
| `role` | 必须为 `"answer"` |

身份信息**完全来自人工复核过的目录元数据**（`identity_verified`、`family_id`、
`allowed_uses`）。哈希只证明字节未变，不证明音频唱了什么词；系统不做任何歌词/ASR
推断，也不会把不同编辑窗口当作不同歌词的证据。

### 1.3 `budget` 对象与默认值

| 字段 | 范围 | 默认 |
|---|---|---|
| `max_candidates` | 整数 `1..2`（布尔、小数拒绝） | **2** |
| `max_attempts` | 整数 `1..6`（布尔、小数拒绝） | **6** |

只允许这两个键。跨多次恢复，尝试总数永不超过 `max_attempts`；候选达到
`max_candidates` 即停止，不会用重复版本凑数。一个真实有效的候选即可与父歌曲一起
进入评审，两个是默认目标。

### 1.4 完整示例

```json
{
  "version": 1,
  "request_id": "wf-demo-1",
  "parent": "/Users/me/beatlab/songs/parent-song",
  "edit": {
    "track_id": "voice",
    "start_beat": 4,
    "end_beat": 8,
    "expected_parent_mix_sha256": "64位十六进制哈希",
    "current_family_id": "fam-old",
    "identity_verified": true,
    "purpose": "vocal-replace"
  },
  "phrases": [
    {
      "id": "answer-a",
      "path": "/Users/me/beatlab/sources/phrase523.wav",
      "sha256": "64位十六进制哈希",
      "family_id": "fam-a",
      "identity_verified": true,
      "allowed_uses": ["vocal-replace"],
      "root_midi": 60,
      "role": "answer"
    },
    {
      "id": "answer-b",
      "path": "/Users/me/beatlab/sources/phrase659.wav",
      "sha256": "64位十六进制哈希",
      "family_id": "fam-b",
      "identity_verified": true,
      "allowed_uses": ["vocal-replace"],
      "root_midi": 60,
      "role": "answer"
    }
  ],
  "budget": {"max_candidates": 2, "max_attempts": 6}
}
```

---

## 2. 作业目录结构

提交成功后，`jobs-root` 下生成一个**独占预留**的新目录（`mkdir` 语义，绝不覆盖）：

```
<jobs-root>/<request_id>/
  request.json                # 不可变请求副本（权限 0444）
  state.json                  # 作业状态，原子重命名写入
  .job.lock                   # fcntl 排他锁文件（非阻塞，进程结束自动释放）
  cancel.json                 # 取消标记（独立原子写入，不持有渲染锁）
  attempts/
    attempt-001.json          # 每次尝试记录，渲染开始前就落盘
    attempt-002.json
  candidates/
    <phrase-id>/              # 成功的子歌曲（完整 song 目录）
  review/                     # 盲听评审产物
    public/                   # index.html 与 A/B/(C).wav（RMS 对齐试听副本）
    private/                  # mapping.json、decision.json、回调状态（不对外）
  delivery/                   # 仅在人工 Keep 后生成：导出的 Live 工程
```

位置安全约束：

- `jobs-root` 不允许位于受保护父歌曲目录内部；
- `jobs-root` 不允许位于任何乐句来源目录内部；
- 每个作业目录都是新的独占预留，任何情况下都不覆盖既有目录。

---

## 3. 状态机

| 状态 | 含义 |
|---|---|
| `queued` | 已提交、已绑定，尚未运行 |
| `running` | 正在校验/渲染（持作业锁） |
| `awaiting_confirmation` | 没有可行候选：缺少身份/用途、无真正不同的家族、或尝试预算耗尽；`reason` 给出可操作原因 |
| `awaiting_review` | 父歌曲 + 至少一个真实候选已生成，盲听页面已就绪，等待人工决定 |
| `completed` | 已有人工决定：Keep（`keep=true`，有 `delivery`）或平局/都不要（`keep=false`，无导出） |
| `failed` | 提交后输入被改动、候选或评审产物损坏等；停止而不是静默复用 |
| `cancelled` | 存在取消标记；不会自动恢复，不再派发生成或交付；取消瞬间已在运行的文件操作可能完成并保留为未交付证据 |

不可变与一致性：

- `request.json` 一经写入不再改变；相同 `request_id` **且规范化内容完全相同**的提交返回原作业；
  内容不同则拒绝。
- 提交时绑定：父歌曲 manifest/score/mix、每个分轨/MIDI/来源哈希，以及每个有效乐句文件的实际字节；提交时无效的来源保存失败原因，不静默换成新字节。
- 提交后有效输入改变 → 作业停止（`failed`），不会静默复用或新建。
- `status` 只读，绝不写作业目录。

---

## 4. CLI 命令

解释器使用：`/Volumes/SanDisk2TB/BeatLab/.venv/bin/python`
工作目录：`/Volumes/SanDisk2TB/BeatLab/.cache/album-execution-20260919`。以下命令在该目录执行；也可经 `pipeline/pipeline.py creative_workflow` 转调。当前输入为 BeatLab score 工程，不直接导入任意 `.als`。

### 4.1 提交

```bash
/Volumes/SanDisk2TB/BeatLab/.venv/bin/python pipeline/creative_workflow.py submit --request ./request.json --jobs-root ./jobs
```

- `--request`：请求 JSON 文件路径（若指向的文件不存在，则按内联 JSON 字符串解析）；
- `--jobs-root`：作业根目录，不存在会创建；
- 输出包含 `job`（作业目录路径）与 `status`（初始为 `queued`）的 JSON。

### 4.2 运行（渲染候选并构建评审）

```bash
/Volumes/SanDisk2TB/BeatLab/.venv/bin/python pipeline/creative_workflow.py run --job "./jobs/wf-demo-1"
```

- 先校验每个乐句，再由真实 `locked_edit` 引擎渲染到 `candidates/<phrase-id>`；
- 接受每个子歌曲前重新校验父歌曲与保护记录；
- 正常结果：`awaiting_review`；无可行候选：`awaiting_confirmation`（含可操作原因）；
- 可安全重复进入：已持久化的候选会按身份/哈希/保护记录核验后复用，不增加重复尝试。

### 4.3 查看状态

```bash
/Volumes/SanDisk2TB/BeatLab/.venv/bin/python pipeline/creative_workflow.py status --job "./jobs/wf-demo-1"
```

### 4.4 取消

```bash
/Volumes/SanDisk2TB/BeatLab/.venv/bin/python pipeline/creative_workflow.py cancel --job "./jobs/wf-demo-1"
```

- 原子写入 `cancel.json`，不等待长时间渲染锁；空闲时同步状态；
- 运行中的进程会在：每次尝试之前、渲染器返回之后、候选/评审发布之前检查标记；
- `completed` / `failed` / `cancelled` 状态不会被取消操作覆盖。

### 4.5 完成（应用人工决定并按需导出）

```bash
/Volumes/SanDisk2TB/BeatLab/.venv/bin/python pipeline/creative_workflow.py finish --job "./jobs/wf-demo-1"
```

### 4.6 启动本地盲听服务

```bash
/Volumes/SanDisk2TB/BeatLab/.venv/bin/python pipeline/creative_workflow.py review --job "./jobs/wf-demo-1" --port 8798
```

- 仅绑定 `127.0.0.1`，`--port` 默认 `8798`；
- 页面为中文，选项只显示随机标签 A/B/(C)，不暴露来源身份；
- 页面提示试听文件仅做 **RMS 电平匹配（非 LUFS）**，原文件不改动。

---

## 5. 人工评审与导出语义

1. 评审选项 = 父歌曲 + 成功候选（共 2–3 项），标签随机；各试听 WAV 为 32-bit float、
   RMS 等电平、共同 -1 dBFS 峰值上限。
2. 人工只能凭听感选择：`保留 A/B/C`、`平局`、`都不要`。系统**永不自动记录保留或品味评分**。
3. 投票通过 `POST /api/decision` 写入**唯一且不可更改**的 `decision.json`；随后回调安全调用
   `finish`。回调失败会被明确报告，可用同一请求在命令行重试恢复，无需重复投票。
4. `finish` 必须读到已持久化的真实决定；没有决定则保持 `awaiting_review`。
5. `平局` / `都不要` → `completed`、`keep=false`，**不导出**。
6. 选择保留父歌曲或某候选 → 先验证选定的冻结版本和决定映射，再由 `export_song` 导出到固定的
   `delivery/` 目录（不做可变覆盖）。音频来源与 Source 配方包（含
   `locked_edit_recipe.json`、`locked-edit.json`、`protection.json`、`sources/`）由既有导出器
   一并收集。
7. 已完成作业重复执行是幂等的：只有当 `export_manifest` 的来源身份、ALS/媒体 SHA 与记录一致、
   且记录绑定当前决定时才复用既有交付，否则显式失败。
8. 取消后仍可打开评审页面，投票决定可以保存，但 `finish` 在任何副作用之前先服从取消状态；
   取消的作业不会发布新交付；已经在执行的渲染/导出可能完成写盘，保留用于核查，不标记成功。

---

## 6. 中断与恢复语义

- 尝试记录在**渲染之前**落盘。若进程中断且最终候选不存在，该尝试标记为
  `interrupted`，**绝不会自动重复同一个未知尝试**。
- 已原子完成的子歌曲，可通过匹配 locked-edit 配方（父歌曲哈希、edit、乐句哈希）并执行
  `validate_song` 来恢复并重新登记。
- 跨恢复的尝试总数不超过 `max_attempts`；候选选择要求不同的候选 ID/家族；
  一个来源无效只计一次失败，不会阻止其他独立材料。

---

## 7. 明确未验证 / 不声称的事项

以下均不在本工作流的验证或声称范围内：

- **不是 LUFS**：评审仅做 RMS 等电平匹配，不做 LUFS 响度测量；
- **无 ASR / 歌词识别**：身份是人工复核过的元数据，不是自动识别的歌词；
- **不做品味判断**：保留与否完全由人工听感决定；
- **Ableton Live 未验证**：导出的音频 Set 未经过 Live 实际打开、重开、工程迁移打开或 Live
  渲染比对；导出器验证的是媒体与 XML 结构，不是 Live 音频引擎；
- 导出的是音频轨（warp off、无新增效果、unity 增益），**不是**可播放的 MIDI 乐器重建。


## 8. 操作与恢复补充

- Keep 成功后页面显示实际工程目录，刷新保持。导出暂时失败会保留决定，修复环境后对同一 job 执行 `finish`，无需再投票。
- 已完整写出的 delivery 但状态未保存时，核对 ALS、试听参考、分轨、MIDI、score、manifest 与配方来源后才能恢复；损坏或半成品不覆盖。
- 人声内容家族、用途与保护范围需明确审核；自动化不自动授权来源。素材不足时补全目录并使用新请求 ID，原失败记录仍保留。
- `request.json` 的内容指纹、候选配方与盲听 WAV 哈希都会复核；同 ID 不允许修改任务。
