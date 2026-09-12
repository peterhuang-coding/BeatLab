# Old Record Crate Implementation Plan

**Goal:** 按用户要求抓取一批有出处的老歌乐句，接入现有入库流程，保存制作方法并提交推送当前项目。

**Architecture:** 首个来源采用 Library of Congress Citizen DJ 的 National Jukebox Blues / Jazz 官方下载目录。抓取公开目录发现 WAV 乐句，按作品轮换取材，限速、限量、缓存并以内容哈希去重；复用现有 SQLite / ingest，保留来源 URL、原曲片段时间标签、许可页面快照及下载哈希。原音频仍保留在本地忽略目录。

**Tech Stack:** Python 标准库 HTMLParser / urllib.parse、当前已安装 requests / soundfile、现有 crawler / ingest / library。

2026-09-13 用户对 Dust Letters 回复“很好很好”，要求多考虑老歌采样、运行爬虫、git push 和方法沉淀。记录为本作品的正向反馈，不编造评分或商业验收。该要求授权本轮实现、实际抓取及推送，不创建定时后台任务。

取舍：Citizen DJ 提供公开的老录音乐句和官方再创作说明，适合立即跑通；Tracklib Songs 更适合后续 60–70 年代 Soul/Gospel 挖歌，但账号/清样流程需单独接入；Freesound 并非本轮完整老歌首选。当前不把 1900–1922 年的录音统称为 70 年代 Soul。

- [x] 新增 `pipeline/connectors/citizen_dj.py`：只接 Blues/Jazz 两个目录，解析作品/片段/媒体链接及许可说明；每次默认最多 10 个新文件，最多 100，网络超时与大小上限，成功文件原子落盘；目录变化或失败记录可检查。
- [x] 新增 `tests/test_citizen_dj.py`：公开目录解析/重复链接/许可缺失、固定域名、真实 WAV 缓存及增量去重、下载失败与限制、实际 SQLite 入库的来源和许可快照。
- [x] 修改 `crawler.py`：把 limit/timeout 传入 connector，避免下载完成后才截断；修改 `ingest.py` 和 CLI 接入来源、保留元数据；本地来源继续 needs_review。
- [x] 实际 Blues/Jazz 抓取并入库，重跑检查内容无重复；生成本地可读曲目索引、来源及验证结果，供下一版选句。
- [x] 更新用户反馈、总计划、README 和制作方法；完成完整源码/依赖/计划检查、测试与烟测。用户已授权推送当前分支；实际 commit/push 的 SHA 与结果由 Git 和本轮 Hub 复盘记录。

来源核验（2026-09-13）：[Blues 下载与 Rights & access](https://citizen-dj.labs.loc.gov/loc-jukebox-blues/use/)、[Jazz 下载](https://citizen-dj.labs.loc.gov/loc-jukebox-jazz/use/)、[项目说明](https://citizen-dj.labs.loc.gov/about/)。官网 robots.txt 实际返回 200，允许抓取；目录明确提供分段 WAV 下载。Rights 状态依据该具体合集声明，不从“歌曲很老”自动推断。

验证入口：`.venv/bin/python -m unittest discover -s tests -p 'test_citizen_dj.py' -v`；真实抓取 `./beatlab ingest --source citizen_dj --path blues --limit 6` 和 jazz；全套 `unittest discover`、`tests/smoke_pipeline.py`、实际暂存文件 `git diff --cached --check`。推送目标为 `origin/codex/sandisk-migration`；不强推、不覆盖远端 main。


## 实际运行与交付

官方目录实际解析出 Blues 309 个 WAV 片段、Jazz 339 个 WAV 片段。这是公开目录可见链接数量，不是已下载数量。实际入库 14 段、383.165 秒，8 Blues + 6 Jazz，来源、目录 SHA、原件 SHA/MD5、标准化 WAV 均验证。索引 `library/sources/citizen_dj/老歌采样库.md`，机器可读 `catalog.json`，播放清单 `old-record-crate.m3u8`。

四次网络运行：Blues 首批新增6；Jazz 首批新增5/失败1（S3连接或读取超时）；Jazz 重跑新增1/跳过6/失败0，先前超时文件成功获取后与Blues已有内容匹配，跨合集去重；Blues 重跑新增2/跳过6/失败0。重跑前已有11份原件的SHA与修改时间未变；SQLite integrity_check为ok，14个入库MD5唯一。实际证据 `evidence/citizen-dj-crate-2026-09-13.json`。本次没有假称下载了完整老歌目录或完成了自动听感筛选。

验证：6项来源测试通过，包含真实WAV、SQLite、缓存损坏、内容去重、失败计数。审查发现主CLI丢失位置参数用法的source且不接受timeout，新增回归测试先红后绿并修复；独立复查确认两项已解决，无剩余严重发现。最终全套64/64、4.336秒通过，日志 `.cache/pre-push-tests-final.log`。60个安装包依赖兼容，锁文件与实际版本完全一致。合成音频的入库→分析→编排→渲染→Review烟测通过；它不包含真实拆轨模型或Live回渲染。

制作经验已整理到 `docs/sampling-playbook.md`：完整乐句优先、围绕主题做变化、原句/solo/成品分层反馈，以及目录发现与音乐质量筛选的区别。首版音色来源是早期录音；60–70年代Soul/Gospel等路线保留为后续选材探索。
