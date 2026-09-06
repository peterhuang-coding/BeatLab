# BeatLab

端到端自动生成采样型 beats 的个人流水线：把一堆音频丢进来，自动产出 3-4 分钟的 beat（音频 + Ableton Live .als + MIDI）。

Kanye / Drake / Tyler 式的"老歌采样混合 + 人声垫底"路线：采样切片 + 鼓 + bass + 编排，而非直接 AI 生成整段音频。

## 链路（每节点可单独手动跑）

```
pipeline.py ingest   <路径...>   # A/B 摄入+整合分类（44.1k 规整、md5 去重、粗分类、BPM/key）
pipeline.py separate --all       # D   拆轨（htdemucs 4-stem）+ 16 片瞬态切片 + 人声 phrase
pipeline.py score    --all       # C   选品评分（100 分 rubric，三闸门：鼓/无vocal/结构）
pipeline.py compose  --best 3    # E   3-4 分钟编排（结构伸缩 + MPC swing 逐 tick + 切片摆放）
pipeline.py render   <beat_id>   # F   渲染 beat.wav + 鼓 one-shot kit + .als 伴生 + MIDI
pipeline.py report   <beat_id>   # G   暗色试听页 + 桌面日期目录镜像
pipeline.py all [路径...]        # 全程正向循环
```

## 依赖

Python 3.11+（Apple Silicon 实测）。核心库：`librosa soundfile mido essentia audio-separator onnxruntime scipy numpy`

```bash
python3 -m venv .venv && .venv/bin/pip install librosa soundfile mido essentia audio-separator onnxruntime
```

拆轨模型（htdemucs ONNX）首次运行自动下载到 `.models/`（~321MB）。

## 目录

- `pipeline/` — 全部代码（`common.py` 是共享契约：路径/SQLite schema/评分权重/MIDI 映射）
- `library/<分类>/<id>/` — 整合分类后的音源（source.wav + stems/ + slices/ + slice_map.json）
- `beats/<beat_id>/` — 每次编排的产出（spec.json + MIDI + beat.wav + .als）
- `db.sqlite` — samples/scores/beats 三表
- `kit/` — 从鼓轨自动提取并经类别滤波隔离的 one-shot 鼓组

## 关键设计决策（2026-08 grill-me 拍板）

1. 用途：自用/创作/练手；发布时灰色采样单独清权（Tracklib 通道）
2. 目标形态：3-4 分钟完整 beat（intro/verse/chorus/bridge/outro），非 loop 拖长
3. 素材：混合全放开（老录音/对白/电影剪辑）；自动爬取只走安全池
4. 部署：本机跑全链路，重活后台
5. Phase 1 = 正向循环；Phase 2 = 反馈闭环（critic/best-of-N/点赞重加权）

## 已知边界

- 鼓 one-shot 来自 demucs 鼓轨切片 + 类别滤波隔离（kick 低通 150Hz / snare 带通 / hat·oh 高通 5.5kHz），非专业采样包品质
- 切片不跨调对齐：多音源切片同段堆叠可能打架（Phase 2 做主音源聚焦+调性对齐）
- .als 为 Live 12 模板 + BPM patch；MIDI 拖入对应轨道使用
