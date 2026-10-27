# 2020起统一1000万资金池

运行 `.venv\Scripts\python.exe -m scripts.test_pooled_arbitrage`，支持
`--output`、`--initial`、`--margin`，默认1000万元、期货名义保证金估计15%。
四品种只做当月；当前收盘选择过去一自然年35组最优账户收益参数，参数模型沿用
单品种含完全空仓回购的原模型，不根据本次组合未来结果再选择。
IM需要上市观察满一年，实际可参加优化约自2023-07起，不能伪造上市前历史。

候选排序=当日实际年化升水−该日选定退出年化阈值，非预期金额或保证利润。
收盘形成计划，次日开盘用当时可见的开盘价格和资金预算选择最高且可负担候选，
每份一手，重复至无可负担候选；最高组太贵时可选次高较便宜组。
同品种可多份，各份独立冻结退出阈值/开仓归属，期末不强平、不自动展期。
新的资金约束在研究层规划整数手数，不修改Ledger资金准入或保证金不冻结现金的规则。

预算现金=唯一Ledger现金−当前期货名义×15%保证金估计−T1锁定资金。
现货代理资金已在购买时扣现金，不能在回购本金里重复扣现货金额。
现货只是指数理想对冲现金资产账务，没有将指数注册为可交易Instrument；
指数代码仅参考，现金代理购销和市值单列，不冒充实际ETF成交。
期货真实合约用原Ledger每日结算，无重复盈亏。

普通退出于次日开盘，现金交割日以最终settlement结清期货、指数close关闭代理。
关闭后现金售出回款＋释放保证金＋当次期货剩余平仓现金流全部锁到下一观察市场日期。
过去已结算的期货现金PnL不再重复锁。周末/休市不释放。
每自然日把正的预算剩余按2020年2%、2021起1.5%、ACT/365、日息1e−8元计回购，
即使存在持仓或当日开平仓，也对真正未使用资金计息。

大行情可能产生流动性缺口：现货代理的浮盈不是可用现金，期货空头的逐日结算损失
却减少现金。15%估计或现金锁定也可能加大预算缺口。报告分开实际负现金与含保证金/锁的
预算缺口，不因缺口自动借款或修改已有持仓；缺口时不开新预算、不计回购。
因此结果不是严格无需追加资金的可执行收益，也未计融资缺口成本。

成本仍仅期货1tick，完整手续费、ETF跟踪/股息、现货和融资成本未核实；
日频开盘全额成交是研究假设。15%是用户指定研究参数，不是历史真实保证金规则。

daily.csv为统一现金/代理市值/保证金/T1锁/回购本金/权益/缺口；cycles.csv为独立组合；
allocations.csv为排名预算与锁定操作；all_trials/selections保存每次35候选和最佳参数；
future_fills为真实合约模拟成交，ideal_spot_cashflows为理论现货现金流，ledger_entries供对账。
summary/yearly为总回报与年度分解，portfolio.png展示净值、全部入场、收益来源和资金占用。

2026-10-12新增资金缺口退出研究：

```powershell
.venv\Scripts\python.exe -m scripts.test_pooled_arbitrage --funding-exit --output outputs/pooled_rolling_arbitrage_funding_exit
```

普通开平时序与T1释放不变。在当日结算前预估所有持仓按当日结算价记账后的可用预算，
若为负，则根据收盘年化升水与每份组合冻结退出阈值的绝对距离，优先退出最近者。
同距离按组合ID排序；不足时继续退出，不假设一份必然足够。到期日年化率未定义，留给原交割处理。
期货按收盘加1tick买入平空，理想指数代理按收盘卖出；先成交后剩余持仓结算，避免重复账务。
缺任一收盘报价不伪造成交，未能解决的缺口保留在每日明细。

紧急退出的回款当天用于支付缺口，不再锁到次日，也不新增当日开仓。
这是为实现用户当日补足资金要求的研究例外；普通退出仍T1锁定。
使用当日收盘及结算信息不能证明盘中已知或可成交，不代表实盘资金始终充足。
funding_actions.csv逐笔记录选择距离及退出前后缺口；原结果目录保留。

重复运行后可独立核对：

```powershell
.venv\Scripts\python.exe -m scripts.verify_pooled_outputs --output outputs/pooled_rolling_arbitrage_funding_exit --repeat outputs/pooled_rolling_arbitrage_funding_exit_repeat --require-no-gap
```

实际ETF腿（2026-10-12）：

```powershell
.venv\Scripts\python.exe -m scripts.test_pooled_arbitrage --funding-exit --etf --output outputs/pooled_rolling_arbitrage_etf
```

配对IF/510300.SH、IH/510050.SH、IC/510500.SH、IM/512100.SH。
按实际开仓开盘指数点位×期货乘数÷ETF开盘价格，向下取整至100份，数量逐组合冻结。
ETF作为fund通过统一Ledger真实证券买卖账务记账，现金以成交额扣除、权益按ETF收盘市值。
期货结算只遍历future持仓。信号和过去一年35组选参仍使用原指数模型，未改用ETF盈亏优化。
期货1tick保持，ETF原价成交且费用/滑点0沿用上轮现货腿成本基线；尚未计分红、份额调整和完整费用。
完整行情合格才开两腿，缺行情不假设平仓；持有ETF缺收盘或交割退出缺报价时报错，不发布伪造结果。
执行ETF T1可卖限制，新买入组合不能当日紧急退出。缺口仍如实记录。
etf_fills.csv、etf_cashflows.csv和cycles中的ETF代码/数量/入退场价格供独立对账。

```powershell
.venv\Scripts\python.exe -m scripts.verify_pooled_outputs --output outputs/pooled_rolling_arbitrage_etf --repeat outputs/pooled_rolling_arbitrage_etf_repeat --etf
```

资金缺口优先撤当天开仓计划（2026-10-12）：

```powershell
.venv\Scripts\python.exe -m scripts.test_pooled_arbitrage --funding-exit --etf --cancel-new-openings --output outputs/pooled_rolling_arbitrage_etf_cancel
```

出现日终预计预算缺口时，先撤销当天原计划预期收益最低的一份。
预期收益仍为信号日年化升水−该组合冻结退出阈值，不能改用实际事后盈利排序。
同值按品种、组合ID排序，必要时逐份撤销；没有剩余当日开仓时，仍有缺口才平老仓，
老仓选择、收盘成交、T1可卖与紧急回款规则不变。
研究分支保留普通开盘退出后的Ledger检查点，只重新执行保留的开仓计划，
被撤计划不产生有效成交、费用、持仓、资金锁或盈亏，原提议与撤销原因另存审计。
这不是将真实成交撤销或绕过ETF T1卖出约束，而是用户要求的事后重排计划；
使用当天收盘及结算信息回看开盘计划，具有同日信息回看偏差，不能称为无未来信息的开盘可执行策略。
原数据和此前结果保留；不修改生产Ledger/撮合资金准入。

输出新增proposed_openings.csv与cancelled_openings.csv。验证命令：

```powershell
.venv\Scripts\python.exe -m scripts.verify_pooled_outputs --output outputs/pooled_rolling_arbitrage_etf_cancel --repeat outputs/pooled_rolling_arbitrage_etf_cancel_repeat --etf --cancel-new-openings --require-no-gap
```

现金分红账务（2026-10-12）：

```powershell
.venv\Scripts\python.exe -m scripts.test_pooled_arbitrage --funding-exit --etf --cancel-new-openings --dividends --output outputs/pooled_rolling_arbitrage_etf_dividends
```

只读固定分红版本fc5b0209d6f86935f26477238c1a50aceee8c1b1a43e73404ce0df7ecb947339。
四ETF分别14/18/4/2笔元数据事件。登记日最终收盘持仓确定每组权益，
除息日认列应收增加权益但不增加可用现金，派息日才把每份金额×登记数量计入Ledger现金。
派息研究假定当日起现金可用，未验证具体日内到账时刻；未扣红利税，不自动再投资。
登记日卖出的份额无权益，登记后卖出保留应收；撤销开仓计划不会产生分红权益。
应收与派息之间转移不重复计PnL，到账后才可开仓、补资金或回购。
策略/优化仍用原指数模型，分红事后用于账务，不将真实查询known_at回填成历史可知时间。
份额调整与完整费用仍未补齐，不声明完整ETF总回报已验收。
daily增加应收与累计分红，cycles增加分红PnL；总套利PnL已含分红，不能再相加。
dividend_events、dividend_claims、dividend_audit保存事件、登记权益、除息及派息对账记录。

```powershell
.venv\Scripts\python.exe -m scripts.verify_pooled_outputs --output outputs/pooled_rolling_arbitrage_etf_dividends --repeat outputs/pooled_rolling_arbitrage_etf_dividends_repeat --etf --cancel-new-openings --dividends --require-no-gap
```

2026-10-13最新决定：恢复IF/IH/IC/IM四品种，CLI默认范围仍为四品种。
当前基准为`outputs/pooled_rolling_arbitrage_etf_dividends`，保留ETF分红及其他设置。

2026-10-13新增ETF净值信号研究：

```powershell
.venv\Scripts\python.exe -m scripts.test_pooled_arbitrage --funding-exit --etf --cancel-new-openings --dividends --nav-adjustment --output outputs/pooled_rolling_arbitrage_nav
```

用户确认加法信号：`[(F−S)/S+(NAV−ETF收盘)/NAV]×365/剩余自然日`。
折价提高信号、溢价降低信号；到期日、缺净值或价格不足不生成信号，不填充。
新信号同时用于35组历史开平搜索、实际开仓/冻结退出阈值判断、预计收益排序和老仓紧急退出。
滚动一年最大含回购账户收益目标及原理想指数训练PnL模型保持，未改成实际ETF收益择参。
实际组合PnL仍实际ETF价格＋分红＋期货结算及既定成本，NAV不作撮合价或市场估值。
固定净值快照6feaff33a4ac4320832a06e488b1794a，其他行情和分红版本保持。
历史净值发布时间未核实，按同日NAV为收盘后决策研究假设，不宣称历史PIT或盘中IOPV。
nav_signals.csv输出所有训练和实际观察的原基差、ETF折价、净值、可知时间和合成率；
all_trials与selections保存重新寻参的完整35候选及结果，其他限制保持。

```powershell
.venv\Scripts\python.exe -m scripts.verify_pooled_outputs --output outputs/pooled_rolling_arbitrage_nav --repeat outputs/pooled_rolling_arbitrage_nav_repeat --etf --cancel-new-openings --dividends --nav-adjustment --require-no-gap
```

修正NAV寻参评分（用户明确要求）：

```powershell
.venv\Scripts\python.exe -m scripts.test_pooled_arbitrage --funding-exit --etf --cancel-new-openings --dividends --nav-adjustment --etf-scoring --output outputs/pooled_rolling_arbitrage_nav_etf_score
```

上轮NAV只改信号、仍以理想指数PnL评分，不能视为完整修正目标。本轮每个候选在
过去一年独立单品种账户中，最多持有一份套利组合，用NAV调整信号开平，并计入
实际ETF100份数量、市场价盈亏、期货tick/逐日结算、现金分红应收和到账、剩余资金
回购、15%预算、T1锁及撤新/平老资金处理。按窗口末实际权益收益最大选择；
窗口开始空仓，各35候选用相同当时可知峰值资本，窗口末持仓只估值不强制平仓。
不提前计未来成交、分红现金或到期PnL，派息未到账仅计应收。
单品种一份训练评分与最终四品种多份资金竞争分别进行，不宣称全组合全局最优。
快速Decimal递推与完整Portfolio/Ledger逐日重放对照验证；缓存只复用窗口内完全相同
开平信号日期的候选，不省略35组输出。原成本/历史NAV发布时间与事后撤计划限制保留。

真实窗口对账：`.venv\Scripts\python.exe -m scripts.verify_etf_candidate_scoring`。

原基差信号＋实际ETF评分对照（建议1）：

```powershell
.venv\Scripts\python.exe -m scripts.test_pooled_arbitrage --funding-exit --etf --cancel-new-openings --dividends --etf-scoring --output outputs/pooled_rolling_arbitrage_original_etf_score
```

本轮不传--nav-adjustment，不加载NAV快照。原(F−S)/S×365/天数信号不变；
只把35候选评分改为实际ETF账户收益，其他四品种、版本、成本、数量、分红、回购、
15%保证金预算、T1锁、撤新/平老规则保持。记录signal_formula和nav_adjustment=false。
真实窗口对账加`--no-nav --output outputs/pooled_rolling_arbitrage_original_etf_score`。
结果重跑验证加`--etf-scoring --original-signal`，不使用--nav-adjustment。

以下排除IC仅为历史试验：增加`--products IF,IH,IM`，输出到独立目录
`outputs/pooled_rolling_arbitrage_no_ic`。统一1000万在剩余三个品种间重新分配，
不是从四品种结果中减去IC盈亏。其他行情和分红版本、参数选择、成本和时序保持。
配置记录实际品种范围，IC归因列为0，无IC信号、选择或成交。
