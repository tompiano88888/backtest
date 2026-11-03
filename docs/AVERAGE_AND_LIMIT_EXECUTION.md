# 均价与委托价/开盘择优撮合

以下是外部调用方显式选择的通用价格模型，不是框架默认策略。
数量、费用、T+1、交易限价、共同交易时钟及参与率共用现有Engine/FeeService/Ledger。
默认参与率仍为一分钟50%、日线10%，同标的同Bar所有订单共享额度。

## Bar均价

`ExecutionConfig(price_model='bar_average', slippage_ticks=0)`：

- 有有效`Bar.turnover`时，均价为`turnover / (volume * Instrument.multiplier)`。
  turnover为人民币成交额，volume是标准化的股/份/手；乘数只用于合约单位换算。
- 成交额缺失，或上述比值不在闭区间[low,high]内时，按`(open+high+low+close)/4`。
  异常均价不会静默截到边界，实际方法记录为`bar_vwap`、`bar_ohlc4`、`bar_ohlc4_invalid_vwap`。
- 不再加tick；无量不成交。使用完整Bar的信息，成交保守记在Bar.end，不回填开盘时间。
- 均价按Decimal四舍六入五成双保留12位小数，微小舍入仍限制在OHLC内；避免循环小数导致资金账与组合归因的加总出现尾差。费用按此实际成交价计算。
- 买入/开仓按交易单位取整。该模型部分平仓也按交易单位取整；可平持仓的最后整笔零股余量可一次退出。
- 未成交量保留并在后续允许Bar按当时均价重试；不会重复已成交量。
  普通单腿订单仍有日终到期规则，跨日重发或组合持续完成由调用方显式管理。
- 显式Instruction.limit_price仍表示限价委托，优先使用对应限价规则；均价市价意图应省略此字段。

固定分钟适配器支持`normalize_day(..., turnover_column='amount')`；字段映射必须由调用方明确提供，
profile同时提供正数`turnover_multiplier`。原快照不修改；未配置时仍不读取成交额。

## 委托价与下一Bar开盘择优

`ExecutionConfig(price_model='limit_open_then_market', slippage_ticks=1)`要求每条Instruction显式携带
已知时点冻结的`limit_price`。研究可根据当时信号价格加减一跳生成报价；引擎不替研究生成信号。

第一允许Bar，买入报价低于low、卖出报价高于high时不成交。其他情况买入按`min(open, limit_price)`、
卖出按`max(open, limit_price)`成交，不重复加滑点。
若成交价等于open记在Bar.start，否则按限价触价、保守记在Bar.end。
不使用报价时点以后才得到的价格重新修改原始报价。

第一Bar未完成余量从随后允许Bar起转普通市价：按开盘加减配置的不利tick、截到方向OHLC边界。
策略回调在Bar结束后提交，始终最早下一允许Bar成交，不能在发信号的已完成Bar成交。
离散集合竞价仍按独立事件和竞价价成交，限价约束仍适用。

## 取消剩余数量

`ctx.cancel(operation_id, reason='...')`或`engine.cancel(operation_id, at=engine.now, reason='...')`
只取消当前观察时点的剩余委托；实际成交、持仓、现金和费用保持原账务记录。
保留原操作、已成交腿和未成交腿，状态为cancelled，产生ORDER_CANCELLED审计。
重复取消已完成/已取消操作返回False；未知操作、回填或提前取消报错。

外部执行调度可读取自身操作的剩余数量，用累计目标减去实际成交量和在途量后下单，
防止重试、计划切换或分批退出时重复委托。具体定时、分批参数和交割日策略属于外部研究配置，
不成为框架默认规则。期货到期现金结算仍按现有结算接口在交割日收盘进行。
