# KO II 浏览器练习台（sample-pad + 16-step sequencer）

纯 HTML/CSS/JS 原型，无任何库、CDN 或构建步骤。用来在浏览器里理解 KO II
的物理鼓垫布局并组合采样：**选鼓垫 → 点亮格子 → 播放循环**。

这是浏览器练习工具，不是硬件仿真，也不含合成器或内置音频；真实 WAV 由父级
提供。

## 文件

| 文件 | 职责 |
| --- | --- |
| `index.html` | 页面结构与文案（中文） |
| `style.css` | 浅灰机身 / 暖白桌面 / 橙色走带 / 蓝灰 LCD，桌面双栏、≤390px 堆叠 |
| `model.js` | 纯逻辑：物理 12 垫布局、每垫 16 步 pattern、内置示例、时序与增益换算。CommonJS + 浏览器全局 `SimModel` |
| `app.js` | DOM 渲染、`fetch('set.json')`、Web Audio 解码/试听/25 ms lookahead 调度、取消令牌、`window.simulator.snapshot()` |
| `test-model.js` | Node 离线单元测试 |

## 运行

`fetch('set.json')` 要求 http(s) 来源，直接双击 `file://` 打开会被拦截。
在本目录（或包含 `set.json` 与 `Samples/` 的目录）起一个本地静态服务器，
例如父级环境提供的服务器，然后打开 `index.html`。

## 素材清单 `set.json`

由父级提供，示例（合成）结构：

```json
{
  "set_id": "synthetic-demo",
  "title": "练习素材",
  "bpm": 88,
  "pads": [
    {
      "id": "kick",
      "group": "A",
      "slot": 0,
      "pad_label": ".",
      "label": "底鼓",
      "file": "Samples/A/00-kick.wav",
      "duration_seconds": 0.7,
      "gain_db": -5,
      "provenance": { "source_title": "示例录音", "note": "测试示例" }
    }
  ]
}
```

- `group`：`A` 鼓 / `B` 主句 / `C` 回答 / `D` 低音与键盘
- `slot`：物理位置 0–11。物理键标签按 3 列 × 4 行阅读顺序为
  `[7,8,9,4,5,6,1,2,3,.,0,ENTER]`，对应 slot
  `[9,10,11,6,7,8,3,4,5,0,1,2]`。空位置自动禁用。
- 本项目导出 WAV 已烘焙 `gain_db`；该字段仅记录来源处理，浏览器不重复施加。
- 所有界面文字均通过 `textContent` 写入。

## 行为要点

- 点击已分配的鼓垫：选中并试听；切换鼓垫/分组不会清掉其它鼓垫的 pattern。
- 16 步编辑器按 4 拍分块；下方“组合总览”显示跨所有分组的活跃节奏行，
  播放时四组一起响；点总览行可直接跳到对应鼓垫。
- 走带：播放 / 停止；BPM 60–160（默认取清单 bpm，兜底 88）；音量 0–1
  默认 0.65；清空当前鼓垫；载入示例会**替换**当前 pattern。
- 初始内置示例（只使用清单中存在的 id）：kick 0/8/10、snare 4/12、
  hat 偶数步、lead_a 0、answer_a 8、bass_30 0/8、可选 piano_42 4。
  初始不自动播放。
- 首次用户手势才创建并 `resume()` AudioContext；随后 fetch + decode
  全部采样，LCD 显示 `解码采样 x/n` 进度。
- 任何采样缺失/解码失败都会显示错误并禁用播放，不会静默跳过。
- 停止时：generation 令牌 +1，取消 25 ms 定时器、所有排队视觉回调，
  并 stop/断开所有已调度与正在发声的 BufferSource（含试听）。
- 页面切到后台标签时自动停止。
- 采样始终原速播放；BPM 只改变十六分音符触发间隔（`60/BPM/4`）。

## 诊断快照（只读）

```js
window.simulator.snapshot()
// =>
{
  ready, decodedCount, playing, currentStep, activeVoices,
  group, selectedId,
  patterns: { [padId]: [false, true, ...16] },
  scheduledEvents: [{ step, time, ids }]
}
```

每次调用返回全新拷贝，不提供任何修改入口。

## 测试

```
/opt/homebrew/bin/node tools/ko2-simulator/test-model.js
/opt/homebrew/bin/node --check tools/ko2-simulator/model.js
/opt/homebrew/bin/node --check tools/ko2-simulator/app.js
```

测试覆盖：物理标签/slot 顺序与 3×4 布局、默认 pattern 只含已有 id、
toggle 不可变与非法参数、清空、跨垫/跨组 pattern 独立、跨组排序、
内置示例存在性过滤、BPM 钳制与十六分音符时长、dB→线性增益等。
真实 24 个本地 WAV 的浏览器行为由父级验证。

原音频加载可继续完成，但停止会取消所有待播放意图；再次点击播放无需刷新。首次可直接播放，停止也适用于单垫试听。
