# 统一框架分红与闲置资金收益

2026-10-16：`Engine`调用`AccountingEvents`，全部现金、应收及PnL进入同一`Ledger`。
策略仍只有只读账户快照和下单接口。未启用配置时，原撮合和资金行为保持。

## 核心与公共日线入口边界

AKShare日线入口拒绝accounting配置；本页描述通用核心账户事件，不声称数据适配器已提供公司行为。合成例examples/account_events.synthetic.json仅可通过Python run_scenario显式Frame运行，不适用于本分支的AKShare CLI run。

## 配置

显式事件：

```json
{
  "accounting": {
    "dividends": [{
      "event_id": "example-dividend-1",
      "instrument": "510300.SH",
      "record_date": "2020-01-03",
      "ex_date": "2020-01-06",
      "payment_date": "2020-01-06",
      "cash_per_unit": "0.01",
      "withholding_rate": "0",
      "source": "explicit-normalized-input"
    }],
    "idle_cash": {
      "enabled": true,
      "rates": [
        {"valid_from": "2020-01-01", "annual_rate": "0.02"},
        {"valid_from": "2021-01-01", "annual_rate": "0.015"}
      ],
      "base": "cash_less_margin",
      "reserve": "0",
      "day_count": 365,
      "quantum": "0.00000001"
    }
  }
}
```

年率示例沿用先前用户指定的研究假设，不代表真实回购行情。`idle_cash`省略或
`enabled=false`关闭收益；分红必须显式列出，不能把无数据解释为无分红。
`withholding_rate`默认0，表示未扣税假设，不能据此认定股票依法免税。

本分支核心只接受显式dividends事件，不支持旧供应商dividend_snapshot；提供该配置会明确报错。AKShare公共日线入口仍拒绝全部accounting配置。

## 显式账户日历

配置`accounting`后，Frame须提供独立账户日历：

- 当天第一帧`accounting_day_start: "YYYY-MM-DD"`，在撮合前认列除息应收和到账现金。
- 全部市场日终工作完成后的帧`accounting_day_end: "YYYY-MM-DD"`，捕获登记日持仓并计息，再更新账户快照和权重权益基准，调用`on_day_end`。
- 同一帧可同时包含开始和结束，适用于显式日频场景。分钟场景把两个标记分置第一帧和最终帧。
- 连续自然日必须完整，周末/节假日以`bars: []`、`open_instruments: []`空帧处理分红和利息；不自动填行情，不把周末算作组合撮合尝试。
- 账户自然日与`Bar.trading_date`分别提供，夜盘交易日仍来自交易所日历，不由自然日期推断。

每个账户日必须显式开始后才能处理Frame；重复/跳日会报错。
区间内最后一天未结束时报告`ACCOUNTING_DAY_NOT_CLOSED`，不偷偷认定已完成登记或计息。
只按显式日期处理到区间末日，不延伸到未来派息日。
提供的分红登记日早于运行起点会报错，避免用期初当前持仓猜测过去资格；初始分红债权导入尚未实现。

调用方须提供正确的全市场日终屏障与逐市场结算指令。启用账户日历后，单个市场的
`settle_instruments`不再提前更新`last_equity`或触发全账户`on_day_end`；它仍执行该市场结算和订单日终处理。
跨市场预期时钟/完整Provider自动装配仍待完成。

## 分红账务与股票扩展

`CashDividend`同时支持`Asset.FUND`与`Asset.EQUITY`，现金金额按持仓数量×乘数×每单位分红计算。
目前要求登记日<除息日≤派息日；不猜测登记/除息同日资料的盘前盘后含义。

1. 登记日全账户日终，逐实际持仓批次锁定数量、取得日期、组合归属。登记日买入且收盘持有者计入；此前卖出者不计入。
2. 除息日开始，确认税后净应收股利，计入权益与原组合PnL，现金不变。以原始除息价格撮合/估值，不能再加复权收益。
3. 派息日开始，净应收转现金，权益与PnL不再增加。卖出或组合已关闭也保留原分红债权。

税额在登记时按显式假设计量，除息时随应收认列；报告保留税前、税额及税后金额。
税额不舍入，使用Decimal精度；未模拟依法扣税或追补税的舍入规则。
派息日首帧撮合前到账是当前工程假设，并非已验证所有证券实际到账时间。

为股票后续扩展保留：

- `DividendTaxPolicy.tax_amount(event, Entitlement)`：冻结的批次数量、取得日期与组合归属，可替换当前固定比例模型。
- `AccountEventHandler.day_start/day_end`：可信框架事件处理器接口，后续接送转、零碎股补偿及红利税调整；不属于策略回调。
- `Ledger.post_cash_event`：带事件ID的现金入账接口；`recognize_dividend/pay_dividend`处理应收及转现金，重复事件报错。
- 现有`Ledger.corporate_action`仍是简化同日现金/整数送转原语；不能与新现金分红事件重复输入同一经济事项。

股票持有期税率、卖出后补税、送转股自动调度、零碎股及ETF期权合约调整尚未实现，不能称为完整股票公司行为验收。
扩展处理器和自定义税模型须作为框架代码接入，并自行保留幂等事件ID、组归因和测试；JSON当前只装配内置现金分红与闲置收益。

持有证券的除息日首帧必须包含该证券原始行情，或提供显式`valuation_marks: {"证券代码": "原始参考估值"}`。
否则报错，防止沿用除息前价格再加股利导致权益虚增。估值参考价不是可撮合行情，
不会凭此成交；若同时有Bar，则以实际Bar.close估值。停牌参考价须由可信数据源明确提供，不自动推算。

## 闲置收益与标准报告

在每个显式全账户日终、结算和公司行为之后计算：

`principal = max(cash - reserve - (margin if base == cash_less_margin else 0), 0)`

`income = principal × 当天有效年率 / day_count`

`base`支持`cash`和`cash_less_margin`，默认后者；保证金只影响此项计息基数，不冻结现金，
不从权益扣除，不产生资金准入限制。应收股利和证券浮盈不是现金，不计息；有持仓时仍对剩余正现金计息。
`reserve`为固定金额计息排除项，不影响撮合。负现金不生息，也不自动计融资利息。
年率按`valid_from`选最近生效值，首日无可用年率报错；不抓取当前利率替代历史。
支持ACT/365或ACT/360，按指定十进制最小位四舍六入五成双（ROUND_HALF_EVEN），每天收益入现金并复投。
这是估计现金收益，不模拟回购订单、费用、质押、期限或实际到账延迟。

权益在原公式上增加未收净股利；`Snapshot.dividend_receivable`单列。
分红归原持仓组合，闲置收益归`cash_management`；全部`group_pnl`之和仍等于账户PnL。
标准输出新增`income`、`accounting`、`receivables`、`noncash_entries`，
其中逐日利息记录本金/利率/收益，分红记录资格/应收/派息状态及税额，现金流水保留事件ID。

验证记录：全套217个测试通过，编译通过，原demo仍为PnL52及4条警示。
新增回归覆盖股票/ETF、登记日前卖出、除息后买入、卖出后派息、期末应收、净税额、
空行情计息、跨年利率、负现金、保证金计息基数、重复事件、缺除息估值与全账户日终基准。
