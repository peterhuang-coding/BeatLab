# WORKFLOW-P0-20260927 实施计划

对应设计：../specs/2026-09-27-locked-workflow-design.md。直接授权来自用户“按这个闭环修改…变成自动化工作流…调用claude code”。执行分支codex/album-execution-20260919，起点cfa4437；主媒体根BeatLab，隔离测试与候选目录。

1. 基线与恢复：核对真实song/revision/validate_song契约、原请求与Git；安全只读检查凭据。历史两份文档提交正常push并核验。新请求不重发旧F03/ALGO unknown。
2. 第一波两路cc-plan（doubao-seed-evolving）：beatlab-locked-edit-20260927-a1负责locked_edit.py及真实音频测试；beatlab-blind-review-20260927-a1负责blind_review.py及HTTP/音量匹配/投票测试。各任务独立暂存白名单，先红后绿，父代理检查原输入hash与实际工具结果后顺序采用。
3. 第二波单路cc-plan：以第一波实际API为依据实现creative_workflow.py、CLI任务生命周期、取消/恢复/有限预算和说明/示例；不与第一波并行修改。使用真实合成音频集成测试，必要时只针对失败点一次质量修正，每逻辑任务累计最多3尝试。
4. 主代理接入旧CLI，拒绝用旧gain重渲染stem-splice配方；独立复验，负面场景、原作品保护、浏览器同秒切换/投票，临时Keep导出回读。正式演示不写用户意见。
5. 更新README和PRD：保留原命令，撤销固定单Hero/固定三模板等与新版不符的未来约束；清楚区分已交付、技术检查、听感和Live未验。保存证据、轮次、主线、状态、MVP计划、Notion和Hub。commit普通push执行分支并核验，不合main。

## 当前进度

- 凭据读取已恢复；历史HEAD cfa4437已普通push，远端一致。
- 第一波已派发，并发2，暂存记录~/.local/state/taskrouter/claude-plan/对应任务ID。
- 基线回归272项通过（16.612秒）；新功能仍待独立验收。旧gain重渲染保护新增测试先失败后通过；导出收集换句配方与sources的2项测试先失败后通过，既有13项导出检查保持。

## 停止与验收

失败/超时/unknown先查原结果/日志/进程，不为换入口改ID；无按量兜底。预算在候选提交前检查，取消不自动恢复。缺少不同唱句进入待确认。未验证产物不采用；用户主观结论保持待试听。

## 第一波验收与第二波

- 两个原请求均已结束；换句原23项、盲听原42项由主代理独立重跑通过。盲听runner的blocked来自报告同时记录预期red退出1和最终green退出0，并非套餐故障；未重发。
- 一次本地质量修正后，换句29项、盲听43项通过；修复低通采样率、目标轨区间外MIDI、路径缓存旧音频、来源哈希、输出嵌套、既有pending保护和score误重渲染。盲听短ID误拦与快速切换播放意图丢失已复现修正；真实浏览器5秒快速切换保持播放。
- 两个20.4秒合成人声演示候选实际完成：五条非目标stem/MIDI字节一致，目标区间外最大差0，原文件SHA/mtime不变；RMS编码后差约0.000000003dB。此为技术演示，不是用户好听或Keep结论。
- 第二波并发2：beatlab-workflow-worker-20260927-a1串联任务生命周期；beatlab-workflow-docs-20260927-a1更新README/PRD并保存历史方案。均已提交，先查原请求，不能重复派发。

- 第一波整体347项回归通过（28.243秒）。已生成证据 `evidence/locked-workflow-2026-09-27.json`；第二波工作仍在执行，尚未宣称整条工作流交付。
