# BeatLab

BeatLab 是一条面向采样型 hip-hop / boom-bap / lo-fi 的本地优先制作流水线：输入真实音源，自动发现 Sample Moments，选择 Hero Sample，完成切片、变形、鼓与 Bass 编排，并输出三个 60–90 秒候选及可继续制作的工程包。

它的目标不是用文本直接生成整首歌，而是把真实采样发展成一份可解释、可复现、可继续编辑的 producer draft。

## 当前闭环

```text
音源摄入 → 去重/权利记录 → 拆轨与分析 → Sample Moments
→ Hero Sample → Loop/Chop/Stem 三种 Recipe → 事件级编排
→ 试听渲染 → Review/反馈 → 自包含 DAW 工程包
```

```bash
python pipeline/pipeline.py ingest --path <音源目录>
python pipeline/pipeline.py separate --all
python pipeline/pipeline.py score --all
python pipeline/pipeline.py moments --all
python pipeline/pipeline.py compose <run_id>
python pipeline/pipeline.py render <run_id>
python pipeline/pipeline.py report <run_id>

# 或一次运行完整链路
python pipeline/pipeline.py all --path <音源目录> --run-id <run_id>
```

## 每个候选的输出

```text
beats/<run_id>/<kind>/
├── arrangement.json          # 试听与工程导出的唯一事件时间线
├── full_mix.wav              # 限幅后的试听混音
├── premaster_mix.wav         # 未做 master 限幅的参考混音
├── stems/                    # chops / drums / bass / vocal dry stems
├── processed/                # 实际使用的处理后 Audio Clip
├── chops/                    # 兼容旧拖入流程的 pad 切片
├── midi/                     # drums / bass / chops MIDI
├── recipe.json
├── provenance.json
└── project/                  # 可搬移的自包含工程包
    ├── arrangement.json
    ├── manifest.json
    ├── Samples/
    │   ├── Original/
    │   ├── Processed/
    │   └── DrumKit/
    ├── MIDI/
    └── reference/
```

`arrangement.json` 为每个轨道、Clip 和素材提供稳定 `track_id`、`clip_id`、`asset_id`，记录 beat 时间线、源采样帧、微时序、增益、声像与操作链。鼓事件锁定实际使用的 one-shot，试听渲染和工程包不再各自随机选音色。

## Ableton 边界

旧版 `.als` 只复制模板并修改 BPM，打开后看不到真实剪辑，因此已经停用。当前仓库输出完整、自包含、可校验的 DAW 中间工程包，但还没有声称完成真实 Live Set。

下一步需要在 MBP 上实现并验收 Live 12 导入器：将 `project/arrangement.json` 转为独立 Audio Clip、Drum Rack、Bass Instrument、段落标记与支持的自动化。只有实际在目标 Ableton Live 12 中打开、保存并完整回放后，才能把 `daw_opened` 和 `daw_playback_verified` 标记为 `true`。

## 依赖

Python 3.11+。核心库：`librosa soundfile mido scipy numpy`。Stem separation 另需 `audio-separator` 与对应运行时。

## 产品与开发文档

- `docs/PRD.md`：产品定位、闭环、MVP 与双机架构。
- `docs/ROADMAP.md`：已完成能力、剩余待办、验收门槛与研究假设。

## 验收

```bash
python tests/accept_d3.py
```

验收使用隔离目录，不触碰真实 BeatLab 素材库；覆盖三 Recipe、确定性、音频渲染、稳定事件 ID、自包含工程包、移动后媒体引用和诚实的 DAW 验证状态。
