# 框架接口

行情 `Bar` 可保留 `settlement_price`（当日结算价）和 `previous_settlement_price`（供应商前结算价），均使用 Decimal。前结算价不从相邻记录推算，也不替代当日结算基准；旧 Parquet 缺此前结算字段时读取为 None。

股指期货全历史日期型落地使用 `backtest.ifind_futures_daily.publish_daily/publish_cache`，版本保存在独立 DuckDB 表 futures_daily_snapshots。它不属于 Store.bars 的带时间戳清单，历史时段 Provider 尚未装配；读取步骤见 [股指期货日线](INDEX_FUTURES.md)。

合约资料使用 `backtest.ifind_contracts.publish_contracts/contract_from_snapshot`，版本表为 futures_contract_snapshots，固定关联的日线版本和 metadata revisions。资料有效时间按生命周期，可知时间按实际下载时刻；历史公告和时段未验证时不回填 known_at。详见 [股指期货元数据](INDEX_FUTURES_METADATA.md)。

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
回调可见 `time/bars/marks/account/positions`，提交方法为 `submit/target/target_weight`。
研究代码是可信本地代码；只读上下文避免意外修改，不是恶意代码安全沙箱。

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

## 2026-10-20 费用、竞价与执行观察接口

ExecutionConfig新增fee_mode（默认fixed，可选historical）、fee_matching（默认fifo，可选today_first）、close_today_fee_rate、commission_rate及commission_per_unit。基础费用与佣金分别计算，平今减免不减免佣金；按实际成交价和数量计算。today_first跨group按同合约同方向先抵当日开仓量，不修改FIFO成本；仅先开后平。完整字段及研究输入示例见[手续费配置](EXECUTION_FEES.md)。

runner.engine_from_config和run_scenario指令/回调共用execution、execution_by_instrument及historical_fees。合约execution完整替换全局，不逐字段继承。HistoricalFees(root,version,as_of)读取已关闭写连接的DuckDB标准fee_rule，按交易日匹配历史区间；缺失或冲突报错。publish_fee_snapshot可发布费用元数据快照，无需虚构行情Bar。下载和独立审计见[历史手续费](INDEX_FUTURES_FEES.md)，Git不携带实际数据库或原始缓存。

runner.result新增fee_details与fee_provenance，逐笔费用拆分合计与Fill.fee、Ledger手续费现金流一致。slippage_ticks作用于连续Bar，离散Auction按结果价零滑点；竞价费用和成交约束照常执行，仅此前提交指令可参与。Frame.auctions与ctx.auctions提供标准竞价事件，竞价不计组合三次连续Bar重试时钟。

ctx.fills_since(index=0)返回不可变成交元组。ctx.target_group(instrument,quantity,group)为独立单合约group设置持续目标，禁止操作组合group。Engine可选execution_guard允许按成交前只读开盘/竞价账户状态缩量或暂缓；ExecutionContext.fill_for_quantity提供无副作用的同源费用报价。execution_observer在open/after_fill/minute_close/settlement提供只读观察，默认关闭。详见[分钟层验收](MINUTE_ENGINE_ACCEPTANCE.md)。
