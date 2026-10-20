# 独立音源爬虫实施计划

**目标：** 用户确认现有音源来源后，将Citizen DJ爬虫做成独立Python CLI；BeatLab音乐与每日夜班暂停。
**架构：** 标准库CLI、显式隔离root、冻结目录快照、manifest/index持久化；复用既有公开解析逻辑，脱离音频转换/SQLite/KO II。
**范围：** discover/download/resume/status；Blues/Jazz固定来源、默认1最多4、32MiB、240秒；来源和内容去重，完整恢复零网络，权限/429停止，损坏拒覆盖。无新定时任务，无真实媒体执行。

- [x] 暂停原夜班并读取确认；复核既有爬虫和数据边界，用户确认来源。
- [x] 固定单包contract/输入/输出/测试，检查全机无活动CC。模块紧密依赖，采用一路。
- [ ] CC在暂存目录写tools/citizen_crate/crawler.py、README与tests/test_standalone_crawler.py。
- [ ] 主代理核输入SHA/白名单，复跑离线合成测试，另验CLI、恢复、失败门禁与来源校验后采用。
- [ ] 暂停记录/计划/代码/证据commit普通push，Notion与Hub同步，保持夜班PAUSED。

验收命令：`/Volumes/SanDisk2TB/BeatLab/.venv/bin/python -B -m unittest discover -s tests -p test_standalone_crawler.py -v`。
固定ID beatlab-standalone-crawler-20261020-a1，900秒、40回合、Seed Evolving原生high；质量修正最多一次，不重发unknown，不付费兜底。真实音频/数据库不交CC。结果以独立测试与实际文件为准。

## 本次结果（北京时间2026-10-21 00:06后核验）

固定a1运行900.124秒后timeout/143，原回执unknown、进程已结束。仅测试草稿落盘，crawler.py与README未生成；主代理独立测试exit1，原因缺crawler模块，未采用任何代码。原输入SHA保持，未真实下载或改音乐。保留原请求，不改ID重发或使用付费兜底。
完整契约见 `../specs/2026-10-20-standalone-crawler-contract.md`，证据 `evidence/standalone-crawler-2026-10-20.json`。上述实现与通过验收步骤仍未完成，不能将测试草稿称为可用爬虫。后续恢复需先检查原回执与草稿，收窄成小块实现；本轮不新增后台任务。
