# 固定分钟快照接入

`backtest.ifind_minute` 将已发布的股指期货、ETF、指数分钟落地区接入现有 Engine，
不包含策略、资金准入限制、日历推断或独立撮合器。

`MinuteSnapshot(root, version, dataset='futures'|'index_etf')` 只读固定 DuckDB 报告，
核验报告指纹、Parquet 路径边界、SHA256、行数和标的，按有限数量分区缓存。
不扫描其他版本文件。`rows(code, day)` 中 day 是这一日盘落地区的上海自然日；
此读取器不处理夜盘交易日。超出版本区间、未登记标的、重复时间戳、未声明的整日
缺失均报错。有 reason/source 的快照 `no_quote_dates` 保留为无行情，不补价。

`normalize_day` 必须显式传入 `SessionCalendar`、`timestamp_at` 和带单位乘数的
profile，复用 `IFinDBarNormalizer`。调用方提供时段和交易日，分钟数据不自行生成
交易日历。该接入口限同一上海自然日的日盘；不能用它推断夜盘所属交易日。

```python
from datetime import time
from backtest.ifind_minute import normalize_day, session_minute_ends

data = normalize_day(
    source.rows(code, day), code, day,
    profile={'id': code, 'asset': 'fund', 'volume_multiplier': '1'},
    calendar=calendar, timestamp_at='end',
    observation_times=(time(9, 30),),
)
```

示例的单位乘数与时间解释只是调用参数，不代表已验证供应商单位和时间戳语义。
profile 的 volume_multiplier 必须依据数据来源确定，不能按资产名称猜测。
`data.bars` 以 Bar.end 为键，`data.observations` 保留显式排除的离散边界记录。
边界记录不自动变成前一分钟 Bar，也不自动变成竞价成交机会；期货竞价可另用
`index_futures_auction`，ETF 竞价须有独立验证的数据契约。

指数 Bar 的可执行 volume 为 0，原始成交量仍保留在不可变快照中，不把可能为
小数的统计成交量四舍五入成可交易数量。Engine 始终拒绝指数指令。

`session_minute_ends` 根据显式时段生成完整预期时钟；`data.missing_ends` 单独
报告缺分钟，不生成替代 Bar。Frame.open_instruments 必须来自该时钟，不能用
bars.keys() 代替：共同交易时段内某腿缺行情仍消耗组合的一次尝试，午休不计数。
三次未完成后的退出由 Engine 在随后允许的 Bar 执行，ETF T+1 仍可能延迟退出。

分钟适配不注入日结算价。衍生品最终日线 settlement_price 仅在日终 Frame.settlements
提供；全账户日终、记账自然日、停牌估值与公司行为仍由调用方明确提供。
完整运行继续执行 finalize，并通过框架标准接口输出结果与绩效。

这项接入通过合成数据的价格、数量、现金、保证金和组合时序对账，不表示独立
交易所日历、真实历史费用/限价/公司行为、供应商单位或全资产分钟验收已完成。
