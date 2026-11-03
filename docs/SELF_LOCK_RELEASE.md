# 通用自锁仓解除

外部策略先判断开仓信号，再调用 `OpeningUnlockPolicy`。框架不计算策略信号，
不默认扫描或平掉其他策略持仓。所有双边解除使用 `ctx.close_locked` /
`engine.close_locked` 共用定价、成交量、费用和账务。

```python
from backtest.self_lock import LockScope, OpeningUnlockPolicy

policy = OpeningUnlockPolicy()

def on_bar(ctx):
    signals = opening_signals(ctx)  # 当时可见、尚未经过资金预算筛选的开仓信号
    policy.after_bar(
        ctx,
        scopes=[LockScope("F", "my-long", "my-short")],
        opening_signals=signals,
        first_bar_ends=calendar_first_minute_ends(ctx),
    )
    # policy.pending_instruments 中的合约暂缓新的相关委托。
```

## 时序与数量

- `first_bar_ends` 由调用方按交易日历显式提供。竞价不是第一根连续分钟；
  夜盘所属交易日不能根据自然日期猜测。缺第一分钟行情记录为缺失，不拿后续分钟替代。
- 第一根连续分钟结束时，有该合约开仓信号则保留锁仓。空信号集合表示调用方明确确认无信号。
  无信号则提交双边平仓，最早从下一允许分钟（连续交易时第二根）成交。
- 只解除指定独立归属中的重叠数量 `min(long_quantity, short_quantity)`；
  净方向仓和其他归属不受影响。组合归属须由其自身生命周期管理，不由此接口接管。
- 直接调用 `ctx.close_locked(instrument, long_group, short_group, quantity=None)`
  可在其他已确认需要解除的时点复用同一执行逻辑；只接受当前观察时点。
  `quantity` 必须为正整数且不超过重叠量。
- 解除前取消这两个归属的旧在途普通操作、移除持续目标，防止重新开回；
  不允许取消混有其他归属的操作。调用方解除期间应暂缓这些归属的普通新目标。
  同一解除未完成时重复请求返回原操作，不重复下单。

## 价格、容量与费用

- 使用**实际成交分钟**的均价。有有效成交额时为
  `turnover / (volume * multiplier)`，否则沿用 OHLC 四价均值，实际方法写入审计。
  成交额映射和单位由适配层明确提供，不在核心猜测供应商字段。
- 卖平多仓 `average - tick/2`，买平空仓 `average + tick/2`。
  每一对等量平仓的合计不利成本为一跳乘合约乘数；不另加普通开盘滑点，
  也不把半跳分别取整成一跳。均价和半跳是明确的分钟成交近似，不是盘口报价还原。
- 使用完整分钟均价，成交时间保守记录在该分钟结束，不能回填开盘。
- 两边共同使用同合约的分钟50%预算。例如成交量10，预算5，仅能平2手多仓加2手空仓；
  不是每边各5手。有两边待成交时先平均分配当前剩余额度，单边残余只补剩余量。
- 停牌、无量、涨跌停、可平数量、历史费用及显式执行守卫仍适用。
  含半跳的价格越过OHLC或涨跌停时该腿不成交，不截价来改变约定的一跳成本。
  分钟过窄时可能两边均不成交；没有保证第二根一定完成。
- 允许部分成交；不保证严格原子完成。一边受约束时，已成交边保持真实结果，
  只重试未完成边，记录 `ORDER_UNFILLED` 和期末残余。解除操作跨日持续，
  到期仍按统一现金结算规则终止，不触发三次失败重新开仓。
- 每边按实际成交价格和数量收费；手续费不包含在“一跳”里。若发生平今，
  仍由真实费用规则计费；框架不内置“避免平今”或资金准入规则。

`result.lock_releases` 保存原始数量、归属和操作ID；`operations`、
`execution_details`、`execution_attempts`、`fee_details` 和 `residual`
保存成交、方法、重试、费用与剩余量。调用方可另保存 `policy.decisions`。

## 手工对账

乘数10、tick=1、均价100、手续费万1。平2手多仓与2手空仓：卖价99.5、买价100.5；
总不利价差成本 `2 * (100.5-99.5) * 10 = 20`，两边手续费
`2*99.5*10*.0001 + 2*100.5*10*.0001 = .4`，合计现金及PnL减少20.4。
已有日终结算基准不会再重复计入。对应案例见 `tests/test_self_lock.py`。
