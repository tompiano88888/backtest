# 默认标准绩效报告

2026-10-27用户确认的框架基本功能。每次完整回测默认输出标准报告，计算与绘图只读结果，不改撮合、费用、持仓或账务。

## 默认输出

结果目录的`standard_report/`包含：

- `performance.png`：累计PnL、日终回撤、年度表、8项汇总指标。
- `metrics.json`：定义、指标、适用性提示、逐日与年度数据。
- `annual.csv`、`daily.csv`：可核对的年度及逐日明细。

收益/PnL/CAGR按正红负绿零黑；回撤幅度绿色；夏普、卡尔玛、波动率、权益等非涨跌值黑色。PnL曲线统一蓝色，回撤面积保留原红色。标题、日期、初始资金、年度表均取实际运行数据，不绑定两组节点或2026年。图表需要matplotlib，已列入基础依赖；部署机器需要中文字体（Windows默认使用Microsoft YaHei）。

## 接入方式

CLI `python -m backtest run ... --output outputs/run/result.json`与`demo`默认生成`outputs/run/result/standard_report/`，避免同一父目录下多个JSON互相覆盖报表。

```python
from backtest.runner import run_scenario, save_result

engine = run_scenario(config, on_bar=callback, output="outputs/new_run/result.json")
# 或在已有运行结束后调用一次：
# save_result(engine, config, "outputs/new_run/result.json")
```

不传output的`run_scenario`和`result`保持纯内存用途，供测试/嵌入式组合使用；持久化完整回测必须走上面的finalizer。直接逐Frame驱动Engine的外部策略应在全账户日终收集唯一日期的权益：

```python
from backtest.performance import export_performance

export_performance(
    daily_rows, initial_cash, start_date, "outputs/new_run",
    title="策略名称", subtitle="本次配置概要",
    minute_drawdown=optional_minute_drawdown,
)
```

每行必须有`day`（显式日期）、`equity`，可选`is_market_day`布尔值，默认True。必须按日期严格递增、无重复、金额有限。自然日计息行应传False，收益并入下一交易日的夏普样本。不提供外部入金/出金调整；输入应为单一初始资金的策略权益曲线。

已默认接入通用CLI、run_scenario(output=...)、现金套利回调CLI、R-Breaker资金池完整运行、期现套利原日频完整运行及统一Engine日频套利入口。老的因子扫描/已退役研究输出器没有批量改写；后续新增或恢复完整回测入口必须调用本模块，不能复用旧的单独图表作为替代。

通用显式Frame场景按bar/auction/settlement的交易日或accounting_day_start/end确定日期，取最后一次可见估值；不按time的自然日猜夜盘。其图中注明“非完整日终结算验收”。空或混合交易日Frame缺乏明确accounting日期时拒绝生成歧义报表。专业完整研究使用显式全账户日终daily_rows，不能把单市场先收盘当全账户日终。

## 指标

CAGR使用开始至期末日期差/365.25；夏普为交易日收益算术均值/样本标准差×sqrt(252)，无风险率0；卡尔玛=CAGR/全区间日终最大回撤；波动率为交易日样本标准差×sqrt(252)。年度PnL为自然年权益增量，收益按该年期初权益，年内回撤逐年重置高点。年度夏普按收益所属交易日分组；跨年休市收入可计入次年首个交易日夏普样本，但年度PnL按自然年划分。不完整年份标“区间”，年度收益不年化。

单日无CAGR；不足两个交易日无夏普/波动率；零波动夏普不定义；零回撤卡尔玛不定义。非正权益保留真实PnL与超过100%的回撤，CAGR/夏普/波动率/卡尔玛显示“—”并提示，不将异常曲线修正为可行策略。金额及年度PnL核对使用Decimal，绘图和统计比率用浮点。

原套利节点CAGR的365/含首尾天数口径与标准口径略不同；本模块不修改旧报表或节点。详细节点对照仍见2026-10-27已保存报告。

## 验证

测试覆盖年度边界、休市收益、布尔/CSV标志、夜盘交易日、重复日期、单日/负权益，以及完整场景自动输出四项报告且PnL不变。当前两组节点入口保持原成交与账务，报表作为末尾附加输出。新运行应始终使用独立目录。
