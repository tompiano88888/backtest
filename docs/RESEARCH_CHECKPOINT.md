# 当前套利研究节点

标记日期：**2026-10-19**。用户已指定最后完成的“5—8月分红调整＋重寻额外开仓下限”版为当前研究节点。后续从本节版本继续，旧基准及历次对照保留。

## 当前采用的版本

- 区间2020-01-01至2026-10-08，IF/IH/IC/IM共享初始资金1000万元，不加NAV。
- 此前3个完整年度的全收益指数与价格指数日收益差，按月汇总并取同月均值；仅累计(day,expiry]与5—8月的交集，月内按自然日分配，其余月份为零。
- 信号为`[(F-S)/S+预测到期前分红比例]×365/剩余自然日`。各历史训练日只使用其此前信息，调整后信号重新进入原35组过去一年滚动开平仓寻参、实际开平判断及排序；原代理评分目标保持，持仓退出阈值入场冻结。
- 调整后年化信号额外下限：**IF≥3.8%、IH≥2.3%、IC≥0.1%、IM≥3.6%**，还须严格大于当日滚动开仓阈值。
- 实际ETF多头＋当月期货空头，整数手及ETF100份取整；收盘信号最早次交易日开盘执行。实际ETF分红、残余现金回购、15%研究保证金预算、普通回款T+1及原事后撤计划/紧急退出口径保持。
- 期末权益**1360.2477万元**，PnL **360.2477万元**，CAGR **4.6457%**，最大回撤**1.9448%**，夏普**1.5373**。
- 1524份组合全部平仓，日终现金及预算缺口均为零。

## 固定成果及复现

- [当前参数](../configs/arbitrage_dividend_may_aug_entry_floors.json)。
- 当前组合：`outputs/arbitrage_dividend_may_aug_optimized_floors`，其`config.json`记录全部固定行情、合约、ETF、分红及全收益快照。
- [研究报告](../outputs/arbitrage_dividend_may_aug_research/report.md)及[核对记录](../outputs/arbitrage_dividend_may_aug_research/verification.json)。
- [按旧节点格式整理的PnL及年度收益](../outputs/arbitrage_dividend_may_aug_current_node/report.md)。
- 35组滚动选择来源：`outputs/arbitrage_dividend_may_aug_fixed_floors/selections.csv`及`all_trials.csv`；164组下限扫描：`outputs/arbitrage_dividend_may_aug_floor_scan`。
- 全收益快照`3f899b4d11604eef86a318e7496f9741`位于`data/index_total_return`；[方法说明](DIVIDEND_SEASON_RESEARCH.md)。

```powershell
.venv\Scripts\python.exe -m scripts.test_pooled_arbitrage --funding-exit --etf --cancel-new-openings --dividends --minimum-return-basis zero_convergence --entry-floors configs/arbitrage_dividend_may_aug_entry_floors.json --dividend-season-adjustment --dividend-season-months 5,6,7,8 --rolling-source outputs/arbitrage_dividend_may_aug_fixed_floors --output outputs/arbitrage_dividend_may_aug_checkpoint_replay
```

省略`--rolling-source`会重新计算滚动参数。**月份参数不可省略**，否则回到全年分红旧模型。既有云端复现ZIP封存的是307.3492万元旧节点，未随本次标记重新打包，不代表本节点。

## 当前验证及边界

此前实现验证238项测试通过；两组合各21份CSV复跑逐字节一致；当前组合1524份两腿盈亏及2473个自然日现金/权益/归因独立核对；164组扫描文件校验和核对。旧基准31份保护文件保持原样。

仍为全样本日频研究，额外下限由全样本事后选择。历史修订/PIT、原代理评分、同日事后撤计划、完整历史费用/税/份额调整等限制继续有效；不能当作实盘净收益或统一分钟框架验收。后续分钟策略继续遵循单一回调＋统一撮合/账务约束。

本次仅登记节点及文档指向，不修改策略、参数、数据或已有结果；不创建Git提交或标签。

不再使用的历史试验已永久删除，依赖保留范围见[清理记录](RESEARCH_MAINTENANCE.md)。

## 2026-10-22统一回测层回放（不改变节点）

沿用本节点信号与逐日参数，以真实日线OHLC接入统一Engine/FeeService/Ledger。净PnL554.3992万元、CAGR6.7344%、夏普1.675238、日终回撤2.0851%。移除事后撤单、同收盘紧急成交，改为收盘定数量／下一日开盘执行，并应用统一组合重试及T+1预留口径；本轮不等同原执行路径复刻，不自动替换上方节点，预算缺口须单列。[执行方法](CASH_CARRY_CALLBACK.md)及[重跑报告](../outputs/node_backtests_20261022/report.md)。
