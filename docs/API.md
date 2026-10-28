# 第一批接口

R-Breaker ATR风险节点新增外部执行路由选项`--avoid-close-today`，需同时显式启用历史手续费、stop-risk和auction。所有平仓及解锁检查当日计费余额，以真实反向开仓代替不安全平仓；框架手续费不改。方法、实际资金及独立审计见[避免平今费路由](RBREAKER_FEE_AVOIDANCE.md)。

R-Breaker外部研究入口新增可选`--historical-fees configs/ifind.index-futures-fees.json`，只启用通用FeeService历史实际计费，策略设计及默认fixed运行保持。历史结果另存，逐笔fee_details与逐日PnL/结算后资金使用率可核对，方法及输出见[历史手续费重跑](RBREAKER_HISTORICAL_FEES.md)。

历史手续费已导入固定日线537个股指期货实际合约，2010-04-16至2026-10-08共42388条，费用库data/execution_fees，版本`bf211dd35fc143b6a7d62bf4a483aa98`。统一历史费用配置片段configs/ifind.index-futures-fees.json，新增断点下载/离线发布和独立核对入口；早期供应商差异与知识时间限制见[导入说明](INDEX_FUTURES_FEES.md)。不自动改变既有研究费用模式。


## 2026-10-20 统一手续费配置

Engine费用统一由FeeService计算。ExecutionConfig新增fee_mode（fixed/historical）、fee_matching（fifo/today_first）、commission_rate、commission_per_unit；runner指令和回调入口共用，合约配置完整替换全局配置。today_first跨group按同合约同方向先抵今开量，昨多10/今开3/平5为3手平今；仅先开后平，实际FIFO成本不变。历史规则由固定版本/as_of的HistoricalFees读取，缺失禁止回退；本地已导入供应商历史序列，仍保留早期差异及历史公告PIT限制。JSON平今比例费率转换与费用有限/非负校验已补齐。明细由fee_details及fee_provenance输出，完整字段、导入和示例见[统一手续费](EXECUTION_FEES.md)。


`Engine(..., execution_guard=None, execution_observer=None)`新增可选执行期扩展，默认全部关闭，不改变统一引擎原来的资金准入约定。`execution_guard(context, candidate_fill)`返回0至候选数量之间的整数，仅可缩量或暂缓，不能指定成交价格；开仓须满足最小交易单位。未成交余量沿用原重试机制，组合三Bar时钟和超时退出保持。

`ExecutionContext`为只读的`time/trading_date/phase/marks/positions/account`观察：准入时仅含该执行时刻已知的竞价价、连续开盘参考及此前估值，不含当前Bar的high/low/close或日终结算输入。每笔实际成交后下一笔重新观察。`fill_for_quantity(q)`返回相同候选价格、按当时FIFO开仓日冻结计算的精确费用，不修改账本。`execution_observer(context, stage)`观察`open/after_fill/minute_close/settlement`；分钟末及结算观察仅用于事后审计，不用于提前成交。

`ExecutionConfig.close_today_fee_rate`可选指定平今名义金额费率，默认None沿用`fee_rate`，与已有按手费用配置分别计算。默认FIFO识别平今数量；可选today_first计费见[统一手续费](EXECUTION_FEES.md)；直接平仓对照试验设平今`.00023`、其余`.000023`。净敞口分配、90%/50%阈值和资金可行性报告由外部研究策略及审计器负责，未内置到撮合核心。新研究入口`python -m scripts.run_rbreaker_exposure`，加`--direct-close-control`运行同规则直接平仓组，详见[净敞口试验](RBREAKER_NET_EXPOSURE.md)。

`ctx.target_group(instrument, quantity, group)`给独立单合约批次设置持续净目标，开平仅影响该group，已有多腿组合group禁止用此接口操作；组合继续用显式legs。完成的独立零目标会清理。研究入口`--batch-margin .05 --batch-stop .005 --include-auction`试验以前日结算权益5%保证金分批、0.5%固定金额止损，具体规则见[分批试验](RBREAKER_BATCHES.md)，不替代已登记R-Breaker节点。

用户2026-10-20确认回测层验收范围：策略作为外部回调/指令来源，无需加入框架或内置。集合竞价使用`models.Auction`及`Frame.auctions`，回调只读`ctx.auctions`；竞价之前提交的指令按竞价结果零滑点成交，其余费用和实际成交约束保持。`runner.run_scenario`支持每Frame的`auctions`记录（instrument/time/trading_date/price/volume及可选状态/限价），与普通bars分开。同标的同Frame禁止重复来源。竞价不消耗三个共同连续Bar的组合重试时钟。规范和范围见[回测层验收](MINUTE_ENGINE_ACCEPTANCE.md)。

5—8月限定分红研究：`--dividend-season-adjustment --dividend-season-months 5,6,7,8`（API `dividend_season_months=[5,6,7,8]`）仅累计(day,expiry]与这些月份的交集，其他月份预测为零；季节内缺历史不填零。仍重新滚动训练开平仓参数，不复用全年模型选择表。缓存检查新增月份集合，禁止异口径复用。省略月份保持上一版全年模型预测，以便复现；新结果使用arbitrage_dividend_may_aug前缀。

分红季研究：历史`pooled_rolling_arbitrage.run`/`scripts.test_pooled_arbitrage`新增`dividend_season_adjustment=False`/`--dividend-season-adjustment`和可选`total_return_version`/`--total-return-version`，默认固定3f899b4d11604eef86a318e7496f9741。要求实际ETF且不与NAV同时启用；此前3个完整年度月度TR-minus-price贡献按自然日预测，信号`[(F-S)/S+q_hat]*365/d`进入历史35组选参及实际开平/排序。缺历史或负预测无信号，不改变现金账务。冻结选择缓存检查季节开关与全收益版本；扫描入口自动读取源的季节信号。原基准与默认行为保留。详见[分红季研究](DIVIDEND_SEASON_RESEARCH.md)。

NAV门槛扫描：`scripts.scan_arbitrage_entry_floor --source <NAV研究目录>`读取源固定NAV版本和原滚动评分类型（代理/实际ETF），用同一NAV信号构建历史开平选择、候选/排序/退出及额外开仓下限；缺NAV无信号，源净值known_at不冒充历史可知时间。门槛0%—4%步长0.1个百分点，结果写新目录；当源为实际ETF评分时，评分口径亦保留。NAV公式及信号明细写根目录nav_signals.csv，独立扫描核对脚本新增NAV公式核对。

历史`run(..., rolling_source=...)`/CLI `--rolling-source`为可选选择表缓存，仅当行情元数据快照、NAV版本/开关、评分类型、分红版本、15%预算及退出处理等完全匹配时读取同品种同日期条目，无附近/未来回填；复制源完整35组试验表并记录三份源文件SHA256，不能将输出指向源目录。它只复用研究选择，不替代撮合/账务。226测试及真实NAV实际ETF研究1077份/2473日、19份CSV一致回归核对；统一Engine/Ledger未变。

旧日频研究`Portfolio`及`pooled_rolling_arbitrage.run`新增可选`min_expected_annual_return_by_product`字典，必须完整且恰好覆盖参与品种，值有限且非负，不得与公共`min_expected_annual_return`同时设置。CLI `--entry-floors configs/arbitrage_entry_floor_best.json --minimum-return-basis zero_convergence`应用IF4%、IH2.4%、IC1.5%、IM3.5%，等于允许；门槛只影响该品种新计划，原滚动评分、平仓阈值及按预期退出差排序均不变。配置记录实际字典；不是相加四个独立账户。统一Engine/Ledger未变。只读报告入口`scripts.summarize_optimized_pooled_arbitrage`输出组合和年度分品种归因，并核对门槛源及复跑记录。

`scripts.scan_arbitrage_entry_floor`编排原`Portfolio`及`replay_history`扫描，默认0%—4%、Decimal步长0.001（0.1个百分点），四品种各1000万独立账户，评分为含分红/回购全区间收益。仅缓存并复用源selections.csv同品种同日的原滚动选择，缺日期报错，不取未来或附近记录；不重新选择原35组。输出164组全部账务、年度/参数表和图；同候选日期集合可复用完全一致执行路径，登记来源及CSV校验和。属于旧日频研究编排，不是新的统一框架策略或样本外寻优。`replay_history`由原run循环原样抽取，保留原顺序和研究处理。

批量扫描使用`replay_history(..., single_product_reporting=True)`只简化单品种的只读每日归因（权益减本金和回购），必须只有一个实际ETF品种；默认False保持历史完整分组报表。该选项不改变任何资金、指令或Ledger，期末save_trial仍调用原group_pnl并独立核对两腿收益。与旧报表的Decimal零/尾随零文本可能不同，数值必须精确相等。完整复跑与逐笔独立校验用`python -m scripts.verify_entry_floor_scan`，允许仅表示格式差异，其他差异报错。

历史统一1.5%基准：`min_expected_annual_return='.015', minimum_return_basis='zero_convergence'`，只检查信号日年化升水率≥1.5%，不减退出阈值；缺信号仍不交易，已有退出、排序及历史35组评分不变。CLI追加`--minimum-return-basis zero_convergence`。`minimum_return_basis='exit_target'`为兼容原历史命令默认值，保留原年化升水减退出阈值口径，不代表当前方案。均仅适用于既有独立日期型研究，不修改统一Engine。

历史日频研究 `pooled_rolling_arbitrage.run` 新增可选 `min_expected_annual_return`（Decimal小数，默认None保持原行为）。CLI `--min-expected-annual-return .013` 表示信号收盘年化升水减所选冻结退出阈值不低于1.3%才生成开仓计划，等于下限允许；不改变历史35组评分、排序、已有持仓退出或统一Engine。未满足的候选记录原因 `expected_annual_return_below_minimum`。这是年化信号差，不是实际持有期收益或扣成本保证收益。

`pooled_rolling_arbitrage.run(output, initial, margin, funding_exit=False, etf=False, cancel_new_openings=False, dividends=False, dividend_version=..., products=None, nav_adjustment=False, etf_scoring=False)`：
资金退出默认关闭；显式开启使用用户指定同收盘流动性研究例外，详见
[统一资金池研究](POOLED_ROLLING_ARBITRAGE.md)。生产撮合时序及Ledger规则不变。
etf=True用实际ETF原价、100份向下取整数量和T1卖出约束，指数仍只用于信号及名义参考。
cancel_new_openings=True（须同时funding_exit=True）在研究分支优先撤销当日预期收益最低开仓计划，用保留计划重新执行账本；含同日收盘信息回看偏差，单列原提议和撤销审计。
dividends=True需要ETF腿，固定事后元数据，通过research_dividends.DividendBook分别处理登记日、应收及派息；只有派息增加Ledger可用现金，研究应收增加权益。未改生产Engine公司行为调度。
products为非空、无重复的品种子集，None默认四品种；CLI使用--products IF,IH,IM排除IC后重新分配统一资金。
nav_adjustment使用nav_arbitrage_signal的加法年化信号，并通过ProductMarket可选rate_provider重新生成训练入退索引；实际入退和风险退出同源。固定NAV读取只读且验SHA256，缺值无信号，历史发布时间仍未验证。
etf_scoring=True需要ETF腿，独立于nav_adjustment；不传NAV调整时保留原基差信号。使用ETFCandidateMarket评分实际ETF单组合账户，不再使用指数代理PnL。evaluate(trace=True)返回收益与完整Portfolio/Ledger逐日追踪供核对，默认Decimal快速递推与追踪完全等值。

指数／ETF日期型观察使用 `backtest.ifind_index_etf.publish_daily`；版本表 index_etf_daily_snapshots。source_volume／source_amount 保留供应商原生值，ohlcv_complete 标明缺成交数据的记录。该观察表尚不是 Engine Bar 输入，详见 [指数与ETF日线](INDEX_ETF_DAILY.md)。

`include_etf_nav=True` 保存 ETF 每日单位净值 unit_nav 和保守的查询可知时间 unit_nav_known_at；净值独有日期单独保存。`backtest.ifind_index_etf.nav_from_snapshot(store, version, code, as_of=None)` 读取完整固定净值序列并校验 SHA256，as_of 需要带时区。旧快照无净值时明确报错。净值不替代市场成交价。

`backtest.expiry_spread_research.run(overrides=None, progress=print)` 运行到期阶段的
条件分位数价差研究，返回四个仓位/成本情景指标。`HoldingPolicy` 为有状态目标生成器，
中性信号保持数量，反向切换，到期关闭组合；`factor_simulation.simulate` 的新增
target_policy 参数接收它，默认 None 时原研究行为不变。详见
[到期条件价差策略](EXPIRY_SPREAD_RESEARCH.md)。

研究层 `backtest.factor_research.run_research(overrides=None, progress=print)` 运行登记的
跨期因子试验，返回 selected/stress/config；配置字段见 DEFAULTS，默认输出
outputs/index_futures_factor_research。`factor_data.make_features` 返回因子/标签、
价格索引和观察日历；`factor_simulation.simulate` 使用统一 Ledger 做日期型研究回放。
Fill.time 现在接受 datetime 或 date，后者明确表示研究交易日期，不是时段时间戳。
详细限制见 [因子研究](INDEX_FUTURES_FACTOR_RESEARCH.md)。

研究层 `backtest.futures_spread.analyze(root, output, delivery_path=None, version=None, metadata_version=None)`
读取并校验日期型快照，返回 provenance 字典。缺交割日期时 status 为 blocked，
输出缺失清单；完整运行 status 为 complete。交割 CSV 字段为 instrument、delivery、source。
默认读取关联的合约快照，用 --metadata-version 和 --version 固定输入；--deliveries 可提供 CSV 替代。
CLI：`python -m scripts.analyze_index_futures_spread`。元数据用于事后研究，不回填历史 known_at。

行情 `Bar` 可保留 `settlement_price`（当日结算价）和 `previous_settlement_price`（供应商前结算价），均使用 Decimal。前结算价不从相邻记录推算，也不替代当日结算基准；旧 Parquet 缺此前结算字段时读取为 None。

股指期货全历史日期型落地使用 `backtest.ifind_futures_daily.publish_daily/publish_cache`，版本保存在独立 DuckDB 表 futures_daily_snapshots。它不属于 Store.bars 的带时间戳清单，历史时段 Provider 尚未装配；读取步骤见 [股指期货日线](INDEX_FUTURES.md)。

合约资料使用 `backtest.ifind_contracts.publish_contracts/contract_from_snapshot`，版本表为 futures_contract_snapshots，固定关联的日线版本和 metadata revisions。资料有效时间按生命周期，可知时间按实际下载时刻；历史公告和时段未验证时不回填 known_at。详见 [股指期货元数据](INDEX_FUTURES_METADATA.md)。

## 回调与显式交易

期现套利接入：`cash_carry_callback.export_parameters()`导出已记录最佳可比版本的
逐日滚动阈值；`CashCarryCallback(parameters,instruments,decision_times,front_contracts,...)`
作为唯一`on_bar`提交实际组合数量，`runner.run_scenario(config,on_bar=...)`共用原Engine。
`cash_carry_callback.run(config,parameter_path)`返回`runner.result`标准结构及参数哈希、决策审计。
时钟、PIT映射、真实分钟资料和原研究差异见[回调说明](CASH_CARRY_CALLBACK.md)。

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
回调可见 `time/bars/marks/account/positions/warnings`，提交方法为 `submit/target/target_weight`。
研究代码是可信本地代码；只读上下文避免意外修改，不是恶意代码安全沙箱。

### 警告与诊断（`ctx.warnings`）

引擎产生的**所有**警告（`engine.warnings` 中的每一条，包括组合重试、交割中断、资金风险等原有代码）按产生顺序交给回调，每条只交付一次：`ctx.warnings` 是自上次回调以来新增警告的只读元组，每条为只读映射（含 `time`、`code` 及各代码的附加字段）。回调可据此重新下单、停止开仓、记录，或抛异常终止回测，研究和策略因此可以提前设计应对。回调之后才产生的警告（结算、过期、资金风险）在下一次回调交付；最后一次回调之后产生的只出现在 `engine.warnings` 和结果里。警告只增加可见性，不改变撮合、过期规则或账务结果。

| 代码 | 产生时点 | 含义 |
|---|---|---|
| `ORDER_UNFILLED` | 撮合后、回调前 | 非组合指令本 Bar 未（完全）成交。`reason`：`SUSPENDED`、`NO_VOLUME`、`AFTER_EXPIRY`、`UPPER_LIMIT`、`LOWER_LIMIT`、`NO_CLOSEABLE_POSITION`、`NO_VOLUME_BUDGET`、`BELOW_LOT_SIZE`、`PRICE_OUTSIDE_BAR`（含滑点把价格推出区间）、`PRICE_OUTSIDE_LIMITS`、`NO_QUOTE`、`NOT_OPEN`、`GUARD_DEFERRED`、`PARTIAL_FILL`；同一指令的原因或剩余数量变化时才再次提示 |
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

独立日频研究 `backtest.continuous_spread.transform` 返回因果到期标准化偏离、
同合约对衔接指标和实际合约前瞻标签；`run(output=...)` 导出预登记入场比较。
复用Ledger的日期型研究模拟不等于生产Engine时钟或撮合装配。
详见 [连续价差研究](CONTINUOUS_SPREAD_RESEARCH.md)。

`request` 指定 SDK 函数与参数；frequency 选择 THS_HQ（日线）或 THS_HF（分钟），另支持 THS_BD、THS_DS、THS_DP 和 THS_DateQuery。IFinDSource 是上下文管理器，管理登录／登出并解析官方 JSON 响应。
CLI update 改用 `normalization`（代码、列、单位和分钟时间戳边界）及 `calendar_file`（带时区实际交易时段）生成标准 Bar，旧 column_mapping 任务需迁移。
新增 ifind-check、ifind-query 和 update --validate-only；实际 SDK 返回字段和权限须通过小样本核验。配置、命令、错误行为及兼容性见 [iFinD 指南](IFIND.md)。
接口参考：https://quantapi.51ifind.com/gwstatic/static/ds_web/quantapi-web/help-center/manual.html

ETF 分红元数据：`backtest.ifind_etf_dividends.read_events(store, version, code, as_of=None)` 返回固定快照的 Decimal 每份现金及独立事件日期。as_of 为策略 PIT 过滤；未传用于事后账务资料。详见 [ETF 分红](ETF_DIVIDENDS.md)。
# 标准绩效输出

`run_scenario(config, on_bar=None, *, output=None)`传output时保存结果并默认生成标准报告；`save_result(engine, config, path)`可用于完成后的统一落盘。`backtest.performance.export_performance(daily_rows, initial, start, output, ...)`供外部完整研究调用，输出PNG/指标JSON/年度和逐日CSV。日终和交易日语义、缺样本/负权益处理及默认入口见[标准报告](STANDARD_PERFORMANCE.md)。

# R-Breaker 分钟实验入口

资金池`run(..., stop_risk=Decimal('.01'), atr_window=20, atr_multiple=Decimal(1))`可选启用风险定仓和ATR止损；默认None保持原行为，CLI用`--stop-risk .01`写独立结果。`StrategyContext.fills_since(index=0)`返回不可变实际Fill元组，游标越界报错，供跨结算交易生命周期跟踪。止损只由回调提交目标，原Engine执行下一Bar撮合；规则、冷却及未成交限制见[R-Breaker止损](RBREAKER_STOPS.md)。

`backtest.rbreaker_portfolio.run(output='outputs/rbreaker_portfolio', start=..., end=..., rate=Decimal('.15'))` 为后续1000万元单资金池版本：`PortfolioRBreaker`同刻观察四品种，通过`ctx.target`实现总90%/品种50%的策略保证金开仓预算，取消一手限制，Engine按成交额0.000023收费并施加不利1tick。每日真实结算并独立核对PnL，图形与验证入口分别为`scripts.plot_rbreaker_portfolio`和`scripts.verify_rbreaker_portfolio`。预算只约束决策时开仓，存量随行情超限不强平；保证金15%并非已验收历史规则。详见[说明](RBREAKER_PORTFOLIO.md)。

`backtest.rbreaker.run(output='outputs/rbreaker_minute', start=date(2020,1,1), end=date(2026,10,8), products=('IF','IH','IC','IM'))`：固定快照、昨日持仓量选择主力，各品种一手，`RBreaker` 作为唯一 `on_bar` 使用 `ctx.target`；分钟 Frame 在日终提供日线真实结算价，Engine/Ledger 负责撮合结算。返回品种及合计毛PnL，输出每日与月份合约归因和成交账务审计。输入时间边界和历史规则尚有研究假设，详见 [方法](RBREAKER_MINUTE.md)。
