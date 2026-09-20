# FEATURE-F03 父版／反馈版对照试听执行计划

**目标**：在真实 gain 子版页面展示父版关系、声部 dB 变化和同秒 A/B 切换，保留当前版本笔记语义。
**授权**：用户持续用plan细化，Notion FEATURE-F03今晚执行；夜班2026-09-20新鲜读取已确认，现标执行中。起点760ad831fdaf28dc7d9e7ca17f5d70f2829ebede；前置F01/F02已推送。
**技术**：现有Python/stdlib、soundfile/numpy/mido、原生浏览器audio。沿用现有隔离工作区，不新增框架/服务。

## 设计与边界
不从目录名猜父版。依据revision.py真实字段parent绝对路径、request_id、parent_mix_sha256、gains_db、status=rendered；父/子均须在所传beats_root内直接子目录。读取与验证不修改作品。复用validate_song验证实际主混音/分轨，比较父hash和真实gain契约；manifest没有mix_sha256时不可假设存在。
接口pipeline/song_comparison.py load_comparison(song: Path, beats_root: Path)返回state=none/ready/unavailable；ready含parent/child的run_id/title/review_url/media_url/mix_sha256/bpm/bars/duration_seconds/master_gain，changes每项track_id/name/delta_db/parent_gain_db/child_gain_db。无revision为none；损坏/逃逸/hash不匹配为unavailable并给无私人路径中文原因。
pipeline/song_comparison_view.py render_comparison_section(model: dict)->str只消费此契约，生成独立对照区和闭包JS。独立播放器与原主播放器区分，笔记仍属于本页版本；切换保留同秒及暂停/播放状态、先暂停另一版。所有audio播放互斥（含原主播放器、solo、新生成反馈版），异步metadata/快速切换不能叠播。无音量匹配或好听结论。

## 同波两路cc-plan
- [ ] 关系/验证包：song_comparison.py、tests/test_song_comparison.py；实际合成render_score→revise_song及负向损坏/越界/来源不必存在、重复复用、只读SHA/mtime。
- [ ] 对照展示包：song_comparison_view.py、tests/test_song_comparison_view.py、docs/product/version-comparison.md；独立合成model测试escaping、状态、语义链接，主代理实际浏览器补验。
- [ ] 主代理输入hash检查后顺序接入song_review.py；不让两包改重叠文件。
- [ ] 隔离真实HTTP与浏览器：同秒暂停/播放切换、快速切换、原播放器互斥、刷新父链接、来源/笔记旧功能；缺失/损坏状态不影响主播放器。
- [ ] 完整unittest、新旧16份主混音SHA/mtime，报告/证据/MVP/路线图/主线/Notion/Hub；普通push核验远端。

测试命令统一为 /Volumes/SanDisk2TB/BeatLab/.venv/bin/python -B -m unittest discover -s tests，单包添加-p明确文件-v；完整合跑用BEATLAB_ROOT隔离。下游仅暂存白名单，不读真实素材/DB/Hub/凭据、不联网、不改父目录、不git；报告changed_files只列允许输出，不列handoff-result.json。未知先核查不重发，质量修正最多一次，无按量。
本轮只完成F03，不推进F04–F06、旧算法冲刺或音乐发布。主观听感待用户。

## 本轮执行结果
两路已派发但凭据helper退出51，均本地blocked，未进入模型业务执行。上方实现/验收checkbox保持未完成。输入hash和16份主混音保持；无代码产物或新测试，不采用、不重发。先恢复本机凭据读取，再按原逻辑任务累计限次继续；详见../../research/2026-09-20-version-comparison-blocked.md。
