# 前收盘触价与次 Bar 市价执行

通用 Engine 新增两个显式可选配置，供外部研究选择执行方案。原来的下一 Bar
开盘加滑点、组合三次失败退出仍为默认行为；不内置具体策略或市场参数。

```json
{
  "combination_max_attempts": null,
  "execution": {
    "price_model": "previous_close_then_open",
    "slippage_ticks": 1,
    "participation": null
  }
}
```

直接构造 Engine 时传 `combination_max_attempts=None`；`engine_from_config` 与
`run_scenario` 接受同名 JSON 配置。缺省值为 3，正整数可配置超时尝试数。
None 仅关闭组合的自动取消/退出，未完成部分持续尝试，不重复已成交数量。
实际交割、日历、可平数量及区间结束等既有事件规则仍有效。普通非组合订单
仍为当日有效；调用方需要跨日持续操作时应使用组合操作或持续目标接口。

`ExecutionConfig.price_model` 默认 `next_open`，`slippage_ticks`默认1。
所有普通市价、后续市价补单和积极报价共用定价：
买入`min(open + slippage_ticks*tick, high)`，卖出`max(open - slippage_ticks*tick, low)`。
买价高于high或卖价低于low属于积极报价，不因报价超出区间而拒绝。
`Instruction.limit_price`可提交显式报价；它与引擎参考报价适用完全相同的判断，
不是研究指定成交价。显式限价余量保留报价，不自动改为市价。
默认participation=None：一分钟Bar（起止相差60秒）50%，日线及其他时长10%，
独立竞价10%；非空配置可固定覆盖。额度按标的、按Bar共享，不按订单分别分配。

选择 `previous_close_then_open` 后：

1. 首个允许 Bar，用上一根已完成、当时已知的同标的 Bar.close 加不利方向
   `slippage_ticks * Instrument.tick` 定价；买入加、卖出减，含买平/卖平。
2. 报价在该Bar的闭区间[low,high]内按报价触价，保守记在Bar.end。
   买价高于high、卖价低于low时按上述市价规则成交，记在Bar.start；
   买价低于low、卖价高于high时不成交。审计保留start/end及实际定价方法。
3. 首次未完成的数量，从随后允许Bar起按开盘加减不利tick、截取到方向边界成交。
   它是下一Bar开盘，不回填第一次失败Bar的开盘，也不使用失败Bar.close定价。
4. 后续仍被约束阻止的数量持续按后续开盘尝试。没有上一完整收盘时，首次记录
   `NO_REFERENCE_CLOSE`，随后转开盘市价；缺行情的预期 Bar 也消耗一次尝试。

第一阶段是“固定参考价格在后续 OHLC 中触及”的分钟近似，不模拟秒级排队、
10 秒/20 秒撤改单路径、盘口队列或成交概率。上一完整收盘保存在 Engine 内，
valuation_marks、竞价结果和每日 settlement_price 不替代这根 Bar.close。
集合竞价仍按既有离散事件模型，不额外计作连续 Bar 的升级尝试。

停牌、无量、涨跌停、最小单位、T+1、共享成交量参与率、费用和显式研究守卫均
继续生效。市价近似不表示保证成交，不通过无量或停牌 Bar 伪造成交。
同帧的开盘成交先于 Bar 内触价成交记账；同一模型时间内仍是先平仓、后开仓，
相同优先级稳定提交顺序，防止用本 Bar 稍后的现金流支持更早的开盘订单。

选择新 price_model 的实际成交写入 `execution_details`，含 fill_index、operation、
method、attempt、time、bar_start/end、reference_time/close。
`execution_attempts` 保留每次请求、实际数量和阻塞原因，组合亦有逐腿诊断。
两者由 runner.result 统一输出。新原因包括 `REFERENCE_PRICE_NOT_TOUCHED`。

单腿暴露时长需由研究/执行报告按目标比例及实际成交统计。分钟数据下不能声称
掌握 Bar 内准确成交秒数，应同时披露模型时间或保守时间界限，并区分交易未完成、
交割清除一腿以及休市跨日的情况。期末未配平的操作不能报告为已完成。
