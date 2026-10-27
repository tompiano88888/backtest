# AKShare 标准绩效报告

完整 CLI 回测 `python -m backtest run ... --output outputs/run.json` 默认追加 `outputs/run/standard_report/`，原 JSON 保留。目录含 performance.png、metrics.json、annual.csv、daily.csv。请每次运行使用独立输出目录。

图中包含累计 PnL、回撤面积、年度收益与回撤、夏普、卡尔玛、CAGR及波动率。正收益红色、负收益绿色、零值和非涨跌指标黑色；PnL曲线蓝色，回撤面积维持红色。绘图依赖 matplotlib 和系统中文字体（Windows 使用 Microsoft YaHei）。

Python 完整运行使用 `backtest.akshare_daily.run(config, root, version, on_bar=None, report_output="outputs/new_run")`；不提供 report_output 保持原纯内存调用行为。其他完整研究入口须在结束后调用 `backtest.performance.export_performance(rows, initial, start, output)`，rows 为按显式日期递增的 day/equity 数据，可选 is_market_day 布尔值。无需重新撮合即可生成图表，不修改策略、费用、成交或账务。

CAGR按自然日差/365.25；夏普按交易日收益的样本标准差、252日年化、无风险率0；卡尔玛为CAGR/全区间最大回撤。年度收益按期初权益，年度回撤重置高点；不完整年份标“区间”。休市收益并入下一交易日的收益样本。仅支持单一初始资金，无外部出入金调整。

场景日期来自明确交易日，按当日最后可见观察估值，图中保留非完整日终结算验收提示。样本不足、零波动或非正权益时不适用的指标显示“—”；实际PnL和亏损保留。当前AKShare股票及实际期货合约日线范围不变。

同步验证使用本地模拟AKShare响应发布的不可变快照；不代表再次联网验收供应商接口。
