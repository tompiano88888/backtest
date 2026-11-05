# 订单级价格模型（回调逐单选择）

在原有 `ExecutionConfig.price_model`（引擎/合约级默认，见 [REFERENCE_EXECUTION.md](REFERENCE_EXECUTION.md)、
[AVERAGE_AND_LIMIT_EXECUTION.md](AVERAGE_AND_LIMIT_EXECUTION.md)）之上，接入的回调函数可以对**每一笔报单**
选择价格模型。同一策略的不同类型报单可选不同模型，每笔只有一个模型；没有选择的报单使用配置默认。
定价规则、参与率（分钟Bar 50%、日线及其他10%，同标的同Bar共享）、费用、T+1、涨跌停等仍由统一 Engine 执行，
本功能只新增“选择权”和披露，不新增成交价格规则。

## 选择方式

`Instruction.price_model` 取值（`None`=用配置默认）：

| price_model | 要求 | 含义 |
|---|---|---|
| `limit_open_then_market` | 必须带 `limit_price` | 信号价±策略指定加点后的**冻结报价**，下一Bar：开盘不劣于报价取开盘；否则Bar触及报价取报价（买价高于最高价、卖价低于最低价时即取开盘，成交价不出区间）；整根Bar触不到则不成交，余量随后按开盘±tick的受限市价继续 |
| `bar_average` | 不带 `limit_price` | 成交额÷（成交量×合约乘数）；缺失或不在[low,high]内用OHLC/4；不加tick |
| `next_open` | 可带 `limit_price`（限价触价） | 开盘±滑点tick，买不高于最高价、卖不低于最低价 |
| `previous_close_then_open` | 不带 `limit_price` | 上一Bar收盘±tick触价，失败后续按开盘市价 |

```python
from backtest.models import D, Instruction

def on_bar(ctx):
    signal, offset_ticks = D("100"), 2
    tick = D("0.2")  # 合约最小变动价位，由研究层提供
    # 开仓：信号价+2个不利tick作为冻结报价
    ctx.submit([Instruction("IF2606.CFE", 1, 2, "open", limit_price=signal + offset_ticks * tick,
                            price_model="limit_open_then_market")])
    # 平仓：按均价成交
    ctx.submit([Instruction("IF2606.CFE", 1, 2, "close", price_model="bar_average")])
    # 持续目标同样可选；target / target_group / target_weight 都有 price_model、limit_price 参数
    ctx.target("510300.SH", 1000, price_model="bar_average")
    # 成交确认：ctx.fills_since(cursor) 给出成交；ctx.execution_details_since(cursor) 给出定价依据
    for detail in ctx.execution_details_since(cursor):
        print(detail["price_model"], detail["price_model_source"], detail["method"], detail["attempt"])
```

信号价、加点由策略/研究层决定，引擎不替研究生成信号。`limit_price` 是报价，不是研究指定的成交价。

## 披露

- `execution_details` / `execution_attempts` 每条记录增加 `price_model`、`price_model_source`（`order`=回调逐单选择，`config`=配置默认）。
- 运行结果新增 `execution_modes`（`Engine.execution_report()`）：可用模型说明、参与率规则、默认配置及合约覆盖，
  以及每个（模型, 来源）的操作数、成交笔数/数量、待成交腿数、限价报价笔数、成交方法分布和未成交原因分布。
  存在逐单选择时 `assumptions` 增加 `callback_selected_order_level_price_models`。
- 标准绩效输出：`standard_report/execution_modes.json`、`metrics.json["execution_modes"]`，图片底部一行汇总默认模型、逐单选择及参与率。
- `Fill.instruction` 仍保留 `price_model`、`limit_price`，`result()["fills"]` 可逐笔核对。

## 取舍与限制

1. 持续目标（`ctx.target*`）带 `limit_price` 时，该报价只对生成它的决策有效：日终结算后不会用旧报价自动重提，须回调重新给出目标和新报价；
   不带限价的目标（如 `bar_average`）沿用原持续目标重提逻辑。
2. 引擎自行生成的订单（组合三次失败后的退出、展期腿）没有订单级选择，使用配置默认价格模型，并发出 `UNWIND_USES_CONFIGURED_PRICE_MODEL` 警告。
3. `locked_close_average` 仍只能由 `close_locked` 使用，不能经 `target*` 选择。
4. `limit_open_then_market` 的第一Bar未成交余量按原规则转受限市价，并非“仅一根Bar有效”；需要仅一根Bar有效时，回调可在下一帧用 `ctx.cancel(operation_id)` 取消。
5. 回调逐单选择只改变定价模型；日线/分钟线价格路径、同Bar内成交顺序及保守成交时间沿用既有说明，不新增秒级排队假设。
