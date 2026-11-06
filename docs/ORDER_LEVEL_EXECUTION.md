# AKShare 日线撮合规则

`execution.price_model` 为默认模型，`execution_by_instrument` 可按标的覆盖。每笔指令还可通过 `Instruction.price_model` 选定一个模型。预生成指令和回调使用同样的撮合规则。

| 模型 | 定价与成交时间 |
|---|---|
| `next_open` | 默认。下一允许日线开盘价加减滑点，买价截到最高价，卖价截到最低价，默认一跳。带显式限价时，区间内按报价触价，价格穿过整个区间时按受限开盘市价，被动报价在区间外则未成交。 |
| `bar_average` | 标准成交额 ÷（成交量 × 乘数）。成交额缺失或算出的价格超出高低价时用 OHLC 四价平均。当前 AKShare 下载不填标准成交额，因此这些快照使用四价平均。均价在收盘记账，定价不加滑点。 |
| `limit_open_then_market` | 需要正数 `limit_price`。下一允许日线开盘价优于或等于报价时在开盘成交，否则触价才在收盘按报价记账。剩余量转下一允许日线的受限开盘市价。 |
| `previous_close_then_open` | 用上一根已完成日线收盘加减滑点生成报价，按 `next_open` 的限价规则尝试。剩余量转下一允许日线的受限开盘市价。 |

触价和均价都是日线估计。开盘可成交的订单先处理，同一时点先平后开。各模型共用每标的每根日线 10% 的默认参与率，可通过 `participation` 覆盖。停牌、涨跌停、T+1、可平数量、手续费及交易单位等约束继续适用。要复现旧配置的零滑点开盘价，可设 `slippage_ticks: 0`。

两阶段模型的普通订单有一次跨日市价机会，之后未成交余量到期。组合仍按原有三个共同交易 Bar 的规则处理。带冻结报价的持续目标不会在余量到期后自动重新生成旧报价，回调可提交新目标和新报价。其他持续目标在没有在途补单时按日终规则重建。引擎生成的展期和超时退出按配置执行，配置要求冻结报价时使用受限开盘市价。

```python
from backtest.models import Instruction

def on_bar(ctx):
    if ctx.bars["600000.SH"].trading_date.isoformat() == "2024-01-02":
        ctx.submit([Instruction("600000.SH", 1, 100, limit_price="10.20",
                                price_model="limit_open_then_market")])
```

`target`、`target_group` 和 `target_weight` 接受 `price_model` 和 `limit_price`。预生成目标使用同名字段。回调可通过 `ctx.cancel(operation_id)` 撤销在途余量，或通过 `ctx.execution_details_since(cursor)` 对照 `ctx.fills_since(cursor)` 核对成交方法。

完整运行的结果 JSON 包含 `execution_modes`、`execution_details` 和 `execution_attempts`。标准报告的图片显示撮合摘要，`execution_modes.json` 和 `metrics.json` 提供默认配置、标的覆盖、逐单选择及实际方法和未成交原因。
