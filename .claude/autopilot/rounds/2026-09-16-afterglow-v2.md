# MUSIC-20260916-V2

- 用户反馈：再复杂一点，多音乐共同采样；评估可售差距。未自动填评分/Keep。
- 结果：四来源拼采，94 BPM / 64 小节 / 165.404秒，31轨、24切片、171 MIDI；主歌人声轻版、原来源试听、同响度v1/v2主歌对照、ALS/ChopRack、本地ReleaseDraft。
- 验证：实际渲染、分轨相加、六组合并、人声轻版-9dB增益、来源/切片哈希、MIDI时长、ALS媒体与鼓垫引用；商品包保持draft。音乐听感、试唱、Live回放和销量均未验证。
- 纠错：来源拼接需显式处理mono/stereo，保留原样本声道，仅在比较片中upmix；完成已校验渲染的后续导出，无需重写旧曲。
- 起始commit：aca70d2，执行分支codex/sandisk-migration。最终Git与Notion回填以真实日志和页面为准。
- 报告：[四来源与售卖差距](../../../docs/superpowers/plans/2026-09-16-afterglow-collage.md)。
- 下一步：用户选完整/人声轻版，以16小节试唱或具体时间点反馈决定下一次修改。
