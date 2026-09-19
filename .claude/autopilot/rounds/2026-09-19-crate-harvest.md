# CRATE-20260919：下载、入库与固定批次恢复

用户直接授权用 Coding Plan 完成下载/爬虫链路。起点 ed60482，执行分支 codex/album-execution-20260919；仍为隔离工作区，未合 main。

交付：`pipeline/crate.py` + `crate` CLI；Citizen DJ 官方 Blues/Jazz 目录发现、固定全局限量清单、3次有限网络尝试、原子文件/来源快照、完整有限音频解码、既有事务摄入、MD5去重、来源/播放索引。`--discover-only` 不下音频，`--resume` 保持选曲；完成项用原件与规范化各自SHA+DB核查，损坏要求修复，批次运行锁退出释放。

验证：原79项基线通过，最终99项unittest通过（6.806s）。隔离2段与正式8段真实下载入库，正式资产14→22；新8段来自8条录音、183.763秒，原48kHz/库44.1kHz。309/339为目录链接数，未全量下载。两个完成批次在禁止requests.get条件下恢复成功，音频SHA/mtime不变；此前24份音频保持。证据 `evidence/crate-harvest-2026-09-19.json`。

委派：6子任务/9次Coding Plan请求（connector2、crate2、tests2、docs1、reviews2），请求ark-code-latest返回auto，不推断底层模型。4份主体草稿采用并按实际失败修正；2路并行审阅的误报经函数guard/来源身份字段/真实测试反证。最后审阅使用coding_plan_batch，peak_in_flight=2。记录 `evidence/coding-plan-crate-2026-09-19.json`，本地完整材料 `.cache/coding-plan-crawler/`。无按量兜底。

命令/边界见 `docs/product/crate-harvest.md`。只完成官方片段获取与入库，不代表完整歌曲/全平台爬取或好句选择；未改变现有音乐。新的用户指令授权接着运行4小时算法迭代，高并发Coding Plan；需另外保存截止时间与任务范围，不由本下载功能自动产生长期运行授权。
