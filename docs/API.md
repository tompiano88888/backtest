# 第一批接口

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
必须为待结算的衍生品持仓提供结算价，否则运行失败。
日终回调是 `engine.step(frame, on_day_end=...)`；其中新指令不会在刚结束的 Bar 成交。

## 账本

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

`request` 原样传入 SDK 的 codes、indicators、parameters、start、end；frequency 选择 THS_HQ（日线）或 THS_HF（分钟）。
`column_mapping` 是标准字段 → 供应商响应列名。输出时间必须带时区，交易日须已按日历标准化。实际 SDK 返回结构与字段必须用有权限的样本核验。
接口参考：https://quantapi.51ifind.com/gwstatic/static/ds_web/quantapi-web/help-center/manual.html
