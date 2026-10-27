# 研究维护与清理记录

2026-10-22按用户要求永久删除此前归档的旧试验、重复结果、旧文档原文与测试临时产物；不再保留可恢复归档。

## 当前入口

- 套利：scripts.test_pooled_arbitrage；scripts.scan_arbitrage_entry_floor；scripts.verify_pooled_outputs；scripts.summarize_current_arbitrage_node；scripts.summarize_dividend_season_research --may-aug。
- R-Breaker：scripts.run_rbreaker_portfolio；scripts.verify_rbreaker_fee_avoidance；scripts.summarize_rbreaker_stops；scripts.plot_rbreaker_portfolio；信号与年度图入口见对应文档。
- 下载、框架演示及全部测试依赖保留。backtest内旧研究模块与共享运行器仍由测试或条件分支引用，此次保持原样，未改框架行为。

## 必须留在原路径的历史依赖

- outputs/rbreaker_checkpoint_20261020_auction_atr20：当前独立核验检查其72份封存校验；当前196文件封存及当前结果完整保留。
- outputs/rbreaker_portfolio_stop_1pct_auction及历史费直接平仓结果：封存关联和当前对照来源。
- outputs/arbitrage_dividend_may_aug_*：当前结果、重跑对照、滚动选择、扫描、研究汇总及报告。
- outputs/arbitrage_pooled_optimized_floors、outputs/arbitrage_nav_floor_research：当前套利汇总读取旧基准及baseline_preserved.json；其保护清单指向的文件全部保留。
- outputs/rbreaker_signal_diagnostics_20261022和outputs/rbreaker_yearly_multidim_20261022：当前只读归因成果。


## 永久删除与核验

共删除此前归档209个条目、32410个文件，归档内容约6.27GB，另清理残留pytest临时目录。原路径仍被框架文档或封存报告引用的历史短页仅保留当前节点跳转。

框架代码、测试、配置、数据及当前节点依赖保持原样。4172份保护文件的校验依据保留在outputs/research_cleanup_protected_20261022.json，最终结果及删除条目见[清理记录](../outputs/research_cleanup_record_20261022.json)。
