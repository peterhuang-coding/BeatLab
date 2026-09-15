# Afterglow v2：四来源拼采与售卖差距

用户要求「再复杂一点，多几个音乐一起采样放进去」，并询问现有音频距离可售 beats 的差距。基于 v1 制作新版，原音频不覆盖。本轮已完成音乐和本地商品草稿；听感、试唱、Live 实际回放和发布条件仍待验。

## 实际改动

94 BPM，64 小节，165.404 秒。两个主歌各扩为 16 小节，器乐回应接替部分老歌人声，副歌保留较密集的切片主题。24 切片、171 次 MIDI 触发、31 条音频轨；整理成老歌主采样、Rhodes、吉他、器乐短句、鼓、Bass 六组，方便继续混音。

| 音乐来源 | 实际用法 | 来源性质 |
|---|---|---|
| [Marion Harris – After You've Gone](https://www.loc.gov/item/jukebox-313413/) | 主旋律、人声切片、倒放、音区变化 | 1918 年女声加管弦伴奏的完整录音片段，未做人声分离 |
| [All Star Trio – 12th Street Rag](https://www.loc.gov/item/jukebox-38191/) | Bb / Eb 器乐短句与反转过门 | 1920 年器乐合奏片段，不能标为分离出来的独奏萨克斯或木琴 |
| Rhodes Dust | Gm 上方音配 Eb/Gm Bass，另移调出 Cm 与 Bb 的和声颜色 | Live Core Library 的音乐演奏 loop，非第三首已发行老歌 |
| Electric Guitar Muffled | 移调后的 Gm / Cm 主歌回应与短句 | Live Core Library 的吉他音乐 loop，非第四首已发行老歌 |

以上四个来源均实际写入非静音音频。乐器库的全局调名不等于每个窗口的和弦；本次 Rhodes Dust 文件虽标 Bb major，所用窗口的 CQT 更符合 G minor。这里采用上方和弦音的重新配低音解释，仍是待耳听验证的音乐判断。

含女声的主录音事件覆盖从 v1 的 **94.1%** 降至 v2 的 **68.4%**；这是事件时长占编排的比例，不是自动检测出的歌声活动比例。新增主歌人声轻版只在两段主歌把整个主录音组降低 9 dB，含其中原有管弦伴奏，不能称为纯伴奏或人声分离。

## 文件入口

运行配方：`examples/afterglow_collage.py`，命令 `.venv/bin/python -m examples.afterglow_collage`。依赖已保留的 v1 与已归档的四来源；新目录存在则拒绝重做。

- `beats/afterglow-2026-09-16-v2/full_mix.wav`：2:45 完整拼采版。
- 同目录 `verse-vocal-light.wav`：主歌人声轻版。
- `v1-v2-verse-compare.wav`：相同的首八小节主歌对比，按实际 EBU R128 测量值衰减匹配，详见 `v1-v2-compare.json`。
- `sources-to-beat.wav`：四来源原片段依次试听，再接完整副歌；采用 RMS 匹配及峰值保护，非 LUFS 匹配。
- `groups/` 六组整曲音频，`slice-map.json`、`phrase-chops.mid` 和来源记录。
- `exports/afterglow-2026-09-16-v2/AbletonProject/AfterglowCollage.als`：31 轨音频工程。
- 同目录 `ChopRack/`：24 切片 MIDI 鼓垫，48–71，对应四个独立互斥组；私人继续制作使用。
- `exports/afterglow-2026-09-16-v2/ReleaseDraft/`：WAV master、320 kbps MP3、31 轨详细 ZIP、六组 trackouts ZIP、MIDI、人声轻版、保留约 6 dB 峰值余量的 mix-headroom.wav。后者仅把同一混音衰减 5 dB，未宣称重新完成母带处理。封面仍为占位草图，metadata 标记 ready_to_publish=false。

试听定位：0:18 吉他回应、0:26 另一首老录音短句、0:51 第一副歌、1:11 桥段、2:03 第二副歌。

## 本轮验证

证据：[afterglow-v2-2026-09-16.json](../../../evidence/afterglow-v2-2026-09-16.json)。真实渲染 7,294,328 帧、0 个削波采样点；31 分轨求和相对成品 RMS 残差 -117.654 dB。完整混音 -15.8 LUFS / true peak -1.0 dBFS；人声轻版 -17.1 LUFS / true peak -1.0 dBFS。24 切片 SHA/源范围/目标帧数、171 MIDI 触发、256 拍终点、31 轨 ALS 媒体副本与 24 鼓垫引用已核验；六组相加一致，人声轻版实际增益变化已数值验证，v1 SHA 未改变。

首次完整试听拼接失败，原因是吉他原片段为 mono，另外三段为 stereo。主曲和轻版渲染已成功，不受此问题影响。保留吉他原片段单声道，只在比较音频中复制为双声道；对已校验渲染调用 finish_delivery，完成对照和导出，没有重写 v1 或把失败当作整曲通过。本轮未修改公共 DSP/导出器，不引用历史测试次数；Live GUI 和音乐审美均未验证。

混音 SHA256：`942c607d79457a0bf7f824c3f74388c8ff96d3c639eb034ec271ccace5166321`。

## 距离能卖出去还差什么

以下按「卖给需要自己录主唱/说唱的人」评估。若目标是出售器乐作品或流媒体发行，受众和验收会不同。当前可以交付制作 demo，尚无依据把它称作有市场验证的成熟商品；来源数量和技术指标不代表购买意愿。

| 优先级 | 当前事实 / 差距 | 最小验收 |
|---|---|---|
| 1：买家能写歌 | v1 含主唱的音乐几乎铺满；v2 已扩 16 小节主歌并减覆盖，提供轻版，但无人试唱 | 在所选版本录一段 16 小节主唱，检查落句、呼吸与主唱辨识；再决定哪些切片保留 |
| 2：主题、律动和混音 | 新版有明确重复主题与四源回应，但未在同响度下用目标风格参考曲做人工耳听，也未验证耳机/手机/音箱兼容 | 锁定一首参考，听主旋律记忆点、Kick/Bass 分工、采样中频拥挤、切点、单声道；按听到的具体问题修改 |
| 3：可授予买家的使用权 | 来源已有证据，但商业上线与 beat 买家条款尚未逐项完成 | 见下方授权说明，落实持有许可、目标平台第三方采样声明、非独占/独占范围和交付内容 |
| 4：商品交付 | WAV、MP3、详细/分组 trackouts、MIDI 与版本记录已实际整理；ALS 实际加载仍待验 | 在 Live 打开、保存、重开并核对音频；客户交付选六组 stems，私人 ChopRack 不作为商品采样库 |
| 5：销售呈现与需求 | 未确定制作人署名、最终封面、授权档位、价格、上架渠道；没有目标买家试听/购买证据 | 完成一个商品页草稿和对应文件，用目标歌手的写歌/购买反馈验证定位；本轮未联系他人或上架 |

### 授权与平台核验（2026-09-16）

- **两个老录音**来自同一 Citizen DJ Jazz 合集。其 [Rights & access](https://citizen-dj.labs.loc.gov/loc-jukebox-jazz/use/) 明确该合集作品可修改、分发及商业使用，并说明其美国公共领域依据；两条作品与下载/转码哈希已留档。该页面是本轮的具体来源依据，不等同替全世界的每种使用方式做了清权承诺。
- **Rhodes、吉他和鼓/Bass 音色**属于 Core Library。Ableton 的 [商业使用说明](https://help.ableton.com/hc/en-us/articles/209768885-Commercial-Use-rights-for-Live-content) 允许 Live 许可持有者在添加其它材料、显著改编后的原创音乐中使用；限制另售为采样产品，且 Demo Songs 不可用。本轮使用归档 sample/loop，没有使用 Demo Song。上线前仍需确认持有有效 Live 使用许可及给买家的交付范围；本地安装不等于许可证凭证。
- BeatStars [上传说明](https://help.beatstars.com/hc/en-us/articles/1260802609630-How-Do-I-Upload-Tracks) 要求声明第三方 loop/sample，并禁止未经授权素材；还需要选择出售的许可和价格。素材可下载或注明作者，不能代替这些步骤。该说明同时指出 stems 上传取决于 Growth/Professional 档，本轮未开通或付费。
- BeatStars [文件规格](https://help.beatstars.com/hc/en-us/articles/205641058-What-Type-Of-Audio-Files-Can-I-Upload) 推荐 stems 为 44.1 kHz、24-bit 或更高；本轮 WAV 已满足该基本格式，MP3 为 320 kbps。文档没有提供「做到某个 LUFS 就会卖出去」的门槛，响度不能替代混音和购买意愿验收。

当前判断：先用人声轻版完成一次真实试唱，比继续增加采样数量更能验证作为 beat 商品的价值。需要「值得买」的实际使用反馈，再完成授权、商品页面和发布；不把上架按钮可用视为已有销量。

下一步：用户选完整拼采版或主歌人声轻版，以 16 小节试唱或明确时间点反馈决定下一次音乐修改。
