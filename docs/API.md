# 框架接口

通用双边自锁解除：`ctx.close_locked(instrument, long_group, short_group, quantity=None)`。
`OpeningUnlockPolicy` 接收外部开仓信号及显式第一分钟时钟，无信号时最早第二分钟执行。
均价加双边合计不利一跳、共享分钟额度、部分成交重试和归属约束见
[自锁解除](SELF_LOCK_RELEASE.md)。框架不生成研究信号或默认自动清仓。

显式竞价容量仿真：`Engine(..., auction_volume_exemption=predicate)`可按不可变指令选择竞价容量豁免，默认关闭；T+1、可平数量、限价、费用和账务仍生效，成交记录保留原始量与豁免标记。见[竞价容量仿真](AUCTION_CAPACITY_SIMULATION.md)。

详细业务约定见[规格](../SPEC.md)，外部策略接入见[接入指引](RESEARCHER_GUIDE.md)。

## 回调与显式交易

```python
from backtest.models import Instruction

def on_bar(ctx):
    # ctx.account 是冻结的账户快照；positions 为只读持仓记录。
    # 仅演示：真实策略需避免每个 Bar 都重复下单。
    if not ctx.positions:
        ctx.submit([
            Instruction("A", +1, 2, "open", "pair-001"),
            Instruction("B", -1, 1, "open", "pair-001"),
        ], combination=True)

engine.step(frame, on_bar=on_bar)
```

`side` 始终表示持仓方向，`effect` 为 open 或 close：`side=-1,effect=close` 为买入平空。
显式均价、冻结委托价与开盘择优、当前帧取消剩余订单的接口见
[均价与委托价执行](AVERAGE_AND_LIMIT_EXECUTION.md)。
可选前收盘触价、下一 Bar 开盘市价升级，以及持续完成组合的配置见
[参考价执行](REFERENCE_EXECUTION.md)；默认撮合与三 Bar 超时规则保持。
普通市价与积极报价统一定价：买入`min(Bar.open + 滑点tick, Bar.high)`，
卖出`max(Bar.open - 滑点tick, Bar.low)`；`slippage_ticks`默认1，可显式配置。
`Instruction(..., limit_price=...)`可选字段表达委托报价，不是成交价格。
买价高于Bar.high、卖价低于Bar.low时按上述市价规则在Bar.start成交；
报价在闭区间[low,high]内按报价触价成交并保守记在Bar.end；
买价低于low、卖价高于high时不成交。参考价模式产生的报价也使用相同判断。
后续市价补单也加不利滑点，不再裸开盘成交。集合竞价仍按独立竞价价格。
`ExecutionConfig.participation`默认None（JSON为null或省略）：一分钟Bar为50%，
日线及其他时长Bar为10%，集合竞价维持10%。一分钟由Bar.end-start恰为60秒判定，
不根据回调频率推断；其他粒度可显式设置参与率。非空participation是跨粒度固定覆盖值。
同标的同Bar所有订单共享向下取整后的数量预算，开仓还按交易单位取整。
成交量预算、涨跌停和可平数量仍生效。显式报价无法触及时记LIMIT_PRICE_NOT_TOUCHED。

订单级价格模型：回调可在每笔`Instruction`上设置`price_model`（`limit_open_then_market`须带`limit_price`=信号价±加点的冻结报价；`bar_average`；`next_open`；`previous_close_then_open`），
`ctx.target/target_group/target_weight`同样有`price_model`、`limit_price`参数；未设置的报单使用`ExecutionConfig.price_model`。
`ctx.execution_details_since(cursor)`返回新成交的定价依据（模型、来源order/config、方法、尝试次数）。
运行结果`execution_modes`（`Engine.execution_report()`）及`standard_report/execution_modes.json`披露默认模型、逐单选择、参与率及各模型成交/未成交统计。详见[ORDER_LEVEL_EXECUTION.md](ORDER_LEVEL_EXECUTION.md)。
回调可见 `time/bars/marks/account/positions/warnings`，提交方法为 `submit/target/target_weight`。
研究代码是可信本地代码；只读上下文避免意外修改，不是恶意代码安全沙箱。

### 警告与诊断（`ctx.warnings`）

引擎产生的**所有**警告（`engine.warnings` 中的每一条，包括组合重试、交割中断、资金风险等原有代码）按产生顺序交给回调，每条只交付一次：`ctx.warnings` 是自上次回调以来新增警告的只读元组，每条为只读映射（含 `time`、`code` 及各代码的附加字段）。回调可据此重新下单、停止开仓、记录，或抛异常终止回测，研究和策略因此可以提前设计应对。回调之后才产生的警告（结算、过期、资金风险）在下一次回调交付；最后一次回调之后产生的只出现在 `engine.warnings` 和结果里。警告只增加可见性，不改变撮合、过期规则或账务结果。

| 代码 | 产生时点 | 含义 |
|---|---|---|
| `ORDER_UNFILLED` | 撮合后、回调前 | 非组合指令本 Bar 未（完全）成交。`reason`：`SUSPENDED`、`NO_VOLUME`、`AFTER_EXPIRY`、`UPPER_LIMIT`、`LOWER_LIMIT`、`NO_CLOSEABLE_POSITION`、`NO_VOLUME_BUDGET`、`BELOW_LOT_SIZE`、`PRICE_OUTSIDE_BAR`（保留的防御性检查；普通市价滑点已截取到Bar区间）、`PRICE_OUTSIDE_LIMITS`、`NO_QUOTE`、`NOT_OPEN`、`GUARD_DEFERRED`、`PARTIAL_FILL`、`NO_REFERENCE_CLOSE`、`REFERENCE_PRICE_NOT_TOUCHED`；同一指令的原因或剩余数量变化时才再次提示 |
| `ORDER_EXPIRED` | 帧结束后 | 非组合指令按 `settle_instruments` 规则到期仍未成交；带最后一次 `reason` 和未成交腿 |
| `POSITION_PAST_DELIVERY` | 帧开始 | 持仓合约已过交割日仍没有结算；引擎不自行结算，持仓保持末次估值 |
| `STALE_QUOTE` | 帧开始 | 持仓标的连续 `stale_quote_days`（默认 3）个交易日没有行情或 `valuation_marks`；`config["stale_quote_days"]` 或 `Engine(stale_quote_days=...)` 配置，`None` 关闭；每段缺失只提示一次 |
| `PENDING_OPERATION_AT_END`、`POSITION_PAST_DELIVERY_AT_END`、`STALE_QUOTE_AT_END` | 运行结束 | `run_scenario` 结束时由 `engine.finalize()` 汇总；最后一次回调提交的指令从未有机会撮合，只在 residual 中标记 `after_last_frame` |

`engine.residual()`（同时写入 `result()["residual"]`）给出期末仍在的持仓、未完结指令、过期指令数、过交割日持仓和陈旧行情。直接驱动 `engine.step` 的研究代码结束时应自行调用 `engine.finalize()`。

### 运行身份与只读存储

`run_scenario(config, on_bar, strategy=None)` 把回调身份写入 `provenance.strategy`：`name`（默认函数限定名，或 `callback.strategy_name`）、`module`、`params`（`callback.strategy_params`，或 `strategy=dict(name=..., params=...)`；必须可 JSON 序列化，否则在任何帧运行前报错）、`source_sha256`（回调源码哈希）。闭包源码不包含参数，所以参数要显式暴露，同一数据同一配置下不同参数的结果才能区分。

`Store(root, read_only=True)` 只读打开 DuckDB，不创建目录和表，`publish`/`put_metadata` 抛 `PermissionError`。多个回放进程可同时只读同一快照；只有 update 类任务应读写打开（DuckDB 在读写打开期间拒绝其他任何进程）。

标准报告的 matplotlib 字体缓存写入系统临时目录（或调用方设置的 `MPLCONFIGDIR`），不再写进 `standard_report` 目录。

离线输入：`engine.submit(legs, at=decision_time, combination=True)`。必须在对应的 `step` 前按时间提交；不得在已执行的时间点回填指令。
`engine.target(instrument, quantity, at)` 表达持续净目标，若需要同合约多空同时持仓请使用显式指令。
`engine.target_weight` 仅支持证券／基金／期货，使用账本的上次日终权益和当前已知价格，期权调用会报错。

## 时钟

`Frame.time` 是当前 Bar 结束时间；bars 中全部 Bar.end 必须相等。
`open_instruments` 来自交易日历，不得根据 bars 的键猜测。某腿在交易时段内缺行情仍应出现在 open_instruments，这会消耗一次组合尝试；休市腿不应出现。
`settle_instruments` 列出该时点结束交易日的标的（证券也可以列入，用于普通订单到期）。
`settlements` 为 `(instrument_id, trading_date, Decimal_price)` 元组，来自最终日线结算记录。
可选`valuation_marks`是没有可撮合Bar时的显式原始参考估值，不产生任何成交。
必须为待结算的衍生品持仓提供结算价，否则运行失败。
日终回调是 `engine.step(frame, on_day_end=...)`；其中新指令不会在刚结束的 Bar 成交。
启用accounting时仅在`accounting_day_end`全账户屏障调用；未启用时保持旧结算Frame回调行为。

组合任一原始腿发生交割时，仍以组合形式执行的未完成操作变为`expired`并产生
`DELIVERY_INTERRUPTED_OPERATION`；该腿已经全部成交、因而不在剩余`legs`中时也适用。
已成交记录、剩余数量和其他腿实际持仓保留；其他腿不自动平仓。外部研究可在日终回调
看到中断及残余持仓，再显式提交退出，最早下一允许Bar成交。
超时后已经转为非组合的`unwinding`清理只受其剩余腿的交割约束；历史原始腿交割不会
中断仍可在自身时段完成的残余退出。剩余退出腿本身交割时仍按到期中断与现金结清处理。

## 账本

统一分红和闲置收益配置见[账户事件](ACCOUNT_EVENTS.md)。`engine_from_config`读取可选
`accounting`；`Frame.accounting_day_start/end`显式提供连续自然日及全市场日终屏障。
`CashDividend`支持股票与基金；`AccountingEvents`调度登记、除息应收及派息。
`IdleCashIncome`按历史配置日利率对正闲置现金记账，默认不启用。
`DividendTaxPolicy`和`AccountEventHandler`是后续股票公司行为的框架扩展接口。
新增`Snapshot.dividend_receivable`，已计入权益与组PnL；应收不作现金或计息本金。

`Ledger.apply(Fill)`：由撮合引擎调用，写现金和持仓；不能用它绕过引擎执行研究指令。
`Ledger.settle(id, trading_date, price)`：重复结算报错；交割日自动现金结清。期权价是期权单位价值。
`Ledger.snapshot(margin_rules)`：不修改账本，返回现金、权益、保证金、风险度、资金缺口和 PnL。
`Ledger.group_pnl()`：现金流和期末价值按 group 汇总，其总和等于账户 PnL。

`MarginRule` 参数：future 使用 rate；index_option 使用 rate 和 floor；etf_option 的 floor 为独立最低比率；future_option 使用 underlying_rate、虚值扣减系数 rate 和 floor。必须按历史规则提供，公式名不意味着已验证全部品种。

## 展期

`engine.roll(group, mapping, at, enabled=True)`，mapping 将品种映射为实际合约 ID。调用方必须通过 PIT 查询确保当时已知、有效。
每个独立策略持仓应使用专有 group，不要用一个 group 混装彼此无关的头寸。正在执行该 group 的组合操作时禁止再提交展期。

## 持久化

固定股指期货、ETF、指数分钟落地区可通过 `backtest.ifind_minute.MinuteSnapshot`
只读加载，再用 `normalize_day` 和显式 SessionCalendar 生成分钟 Bar。
边界观察、指数参考量、缺分钟预期时钟与版本核验见[分钟接入](MINUTE_FEED.md)。

```python
from backtest.storage import Store

with Store("data") as store:
    version = store.publish(bars, source="normalized", frequency="1m", parent=store.latest())
    history = store.bars(version, ["A"], start, end)
    mapping = store.metadata("main", "TEST.P", effective_at=now, as_of=now, version=version)
```

`put_metadata(kind,key,valid_from,known_at,payload,valid_to=None)` 是当前通用结构，payload 是 JSON 对象；业务 Schema 由后续标准化层校验。旧数据版本的 metadata 查询排除该版本发布之后录入的修订。
`store.bars` 当前是分区读取的正确性实现；尚未优化为完整列裁剪／谓词下推查询器，不用于宣称性能目标。

## iFinD 配置

`request` 指定 SDK 函数与参数；frequency 选择 THS_HQ（日线）或 THS_HF（分钟），另支持 THS_BD、THS_DS、THS_DP 和 THS_DateQuery。IFinDSource 是上下文管理器，管理登录／登出并解析官方 JSON 响应。
CLI update 改用 `normalization`（代码、列、单位和分钟时间戳边界）及 `calendar_file`（带时区实际交易时段）生成标准 Bar，旧 column_mapping 任务需迁移。
新增 ifind-check、ifind-query 和 update --validate-only；实际 SDK 返回字段和权限须通过小样本核验。配置、命令、错误行为及兼容性见 [iFinD 指南](IFIND.md)。
接口参考：https://quantapi.51ifind.com/gwstatic/static/ds_web/quantapi-web/help-center/manual.html

ETF 分红元数据：`backtest.ifind_etf_dividends.read_events(store, version, code, as_of=None)` 返回固定快照的 Decimal 每份现金及独立事件日期。as_of 为策略 PIT 过滤；未传用于事后账务资料。详见 [ETF 分红](ETF_DIVIDENDS.md)。
# 标准绩效输出

`run_scenario(config, on_bar=None, *, output=None)`传output时保存结果并默认生成标准报告；`save_result(engine, config, path)`可用于完成后的统一落盘。`backtest.performance.export_performance(daily_rows, initial, start, output, ...)`供外部完整研究调用，输出PNG/指标JSON/年度和逐日CSV。日终和交易日语义、缺样本/负权益处理及默认入口见[标准报告](STANDARD_PERFORMANCE.md)。
