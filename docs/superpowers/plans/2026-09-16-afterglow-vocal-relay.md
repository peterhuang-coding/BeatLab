# Afterglow v3：更换唱句与人声接力

用户反馈 v2「可以，人声或者采样可以再多来点，感觉这个里面大妈一直是这两句」。问题在主唱素材仍重复，而不仅是切法或器乐不够。本次明确配方修订保留 v1/v2；新版本单独待试听，不继承旧好评，也不自动填星级/Keep。

## 已完成的音乐改动

94 BPM、64 小节、165.404 秒，沿用 v2 的鼓、Bass、Rhodes、吉他和器乐短句编排。两段主歌分别安排不同领唱顺序与应答；桥段换成无歌词 Oh/Ah 旋律。原《After You've Gone》的 a/b 两片只在开场和两个副歌回归，另在结尾保留 resolve。

| 新来源 | 编排用途 | 可核验的来源性质 |
|---|---|---|
| [Esther Walker – Blues (My naughty sweetie gives to me)](https://www.loc.gov/item/jukebox-33694/) | 六个不同窗口，跨音区问答、结尾低音区长句 | 另一位女歌手的历史独唱加管弦伴奏录音，Citizen DJ Blues |
| [Marion Harris – A good man is hard to find](https://citizen-dj.labs.loc.gov/loc-jukebox-blues/remix/?itemId=jukebox-31525&itemStart=143000) | 六个不同窗口，第二组领唱与句尾回应 | 原歌手的另一首歌曲，不能算第三位歌手；该链接是作品元数据，实际所用区间以本地 slice-map 为准 |
| Vocal BNYX Oh Ab / Vocal BNYX Ah B | 主歌空位、副歌句尾、桥段的新旋律 | 已归档的 Ableton Core Library 无歌词单音采样，不是新增的两首老歌 |

现在有三条含女声的历史录音、两位已核验署名的历史女歌手；连同既有器乐录音与 Rhodes/吉他，一共六条音乐演奏来源，另有库内无歌词采样。新增 12 个不同源窗口，另有倒放、尾音变体和四个定音高无歌词切片。**不同窗口不等于已转写确认的 12 句不同歌词**；唱声仍连带原管弦伴奏，未执行人声分离。

原录音事件从 v2 的 108 次降到 v3 的 7 次，合计 15.1 拍，占整曲 5.898%。这是事件时间比例，不是声乐活动检测。新录音组的活动采样 RMS 分别约 -20.10 / -20.38 dBFS，承担前景唱句；此数值只核验实际声音能量，不能证明旋律协调或用户偏好。

## 交付入口

所有媒体仅存本机 `/Volumes/SanDisk2TB/BeatLab/`，不进入 Git / Notion。

- `beats/afterglow-2026-09-16-v3/full_mix.wav`：2:45 完整版。
- 同目录 `vocal-relay-preview.wav`：从整曲 0:10 开始的 30.638 秒主歌；`vocal-relay-solo.wav`：同段三个历史录音组和无歌词组单独播放，仍含录音原伴奏。
- `new-sources-to-relay.wav`：两个新录音的四个原始窗口，再接 v3 主歌。来源片段使用 RMS 匹配和峰值保护，成品段不改增益，非 LUFS 对照。
- `groups/`：旧钩句、Esther、新 Marion 歌曲、无歌词、键盘、吉他、器乐短句、鼓、Bass 九组完整音频。
- `slice-map.json`、`phrase-chops.mid`、`source-provenance.json`：29 个切片、150 次触发、原始/预处理/切片哈希、窗口和移调记录。
- `exports/afterglow-2026-09-16-v3/AbletonProject/AfterglowVocalRelay.als`：36 条对齐音频轨，效果已烘焙，附媒体。
- 同级 `ChopRack/Afterglow 29 Vocal Relay Pads.adg`：MIDI 48–76，历史录音同组互斥，其余角色分组；用于继续创作，不承诺与成品混音一样。

完整曲定位：0:00 新女声，0:10 新唱句主歌接力，0:51 熟悉钩句回来，1:11 无歌词桥段，1:22 第二组主歌重排，2:03 第二副歌，2:33 低音区尾声。

运行配方：`.venv/bin/python -m examples.afterglow_vocal_relay`。需要归档父版本与本地素材；目录存在则拒绝覆盖。新增来源试听整理在 `finish_delivery`，本次该小段在完整渲染后补入并独立执行核对；未为本次创作修改公共 DSP 或导出器，也未重跑整套历史回归测试。

## 本轮验证与边界

[真实证据](../../../evidence/afterglow-v3-2026-09-16.json)：36 非静音等长分轨，7,294,328 帧；分轨和相对成品 RMS 残差 -116.476 dB，九组相加峰值残差 7.153e-7。0 个削波采样点，完整曲 -16.7 LUFS、true peak -1.0 dBFS。29 个切片的源/处理/切片 SHA 与目标帧数、12 个新窗口非重叠、150 MIDI 触发和 256 拍终点、36 条 ALS 媒体副本和 29 鼓垫引用均核验。

v1/v2 混音 SHA 均未变化。v3 SHA256：`c6e882879e1453b753327598eff0973956eebb6a34a276404b56c3f8a38396ab`。

和声选择依赖局部 harmonic chroma 与编配判断；老录音噪声、滑音和管弦伴奏使部分窗口和弦候选接近，没有人工耳听/逐字歌词核验，不能声称所有新句都已验证好听。Live GUI 打开、发声与回渲染仍待验。旧 v2 商品草稿仍保留，本次优先音乐修订，未新建 v3 商品包或发布音乐；商业差距继续见 [v2 记录](2026-09-16-afterglow-collage.md)。

下一步：用户试听 v3，指出喜欢或不喜欢的具体唱句/时间点，决定保留哪些新素材。
