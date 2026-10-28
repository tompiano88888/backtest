# ETF 全历史分红元数据

本文所列历史下载规模、版本与本机验证仅为适配器开发记录；数据、SDK、凭据和运行输出不随Git交付。新使用者必须准备自己的输入和固定版本，不能假定这些路径已存在。

使用用户提供的 17 项 THS_BD 指标及 1349 个 ETF 代码；范围是该清单，
不声称覆盖清单外已退市基金。年度、累计、区间汇总和分红条款保留为原始观察，
单次事件另存标准字段，不能将年度汇总作为单次分红现金流。

```powershell
.venv\Scripts\python.exe -m scripts.download_etf_dividends
.venv\Scripts\python.exe -m scripts.download_etf_dividends --publish-only
```

默认截止 2026-10-12。真实样本已验证日期参数返回当时最近一笔分红；每次
取公告日前一天继续回溯，直至无事件。累计次数及每份累计金额必须与所取
已除息事件完全一致；已公告且除息日晚于截止日的事件另外保留，
不计入该截止日的累计值。缺字段、不支持的方案、金额差异或网络失败不发布完整版本。
首次无分红且累计次数为 null 标记为 `no_reported_events`，保留原始空值，
不将供应商空值改为已验证的零分红。

元数据保存于 `data/etf_dividends/metadata.duckdb`。`etf_dividend` 保存
instrument、announcement_date、record_date、exchange_ex_date、otc_ex_date、
payment_date、base_date、cash_per_unit、currency、distribution_plan。
每10份的方案除以10，金额全程 Decimal；只识别经过真实响应核对的明确格式，
遇到其他格式报错并保留缓存供检查。

`ifind_etf_dividend_observation` 保存全部查询观察，`etf_dividend_snapshots`
固定标准事件 revision 及覆盖报告。元数据 valid_from/valid_to 为场内除息日
的起止；known_at 保守使用真实查询时刻，recorded_at 为本地写入时刻。
公告日期单独保留，未经历史发布验证不据此回填 known_at。

原始响应写入 staging 内容缓存，断点续传复用成功批次。需要重新查询历史
修订时指定新的 `--cache data/etf_dividends/staging-revision-日期`，旧缓存、
metadata revisions 和快照全部保留。`--cutoff YYYY-MM-DD` 控制新查询截止点，
`--codes 510300.SH,510050.SH` 仅用于样本，默认是全清单。

读取固定版本：

```python
from backtest.storage import Store
from backtest.ifind_etf_dividends import read_events

with Store('data/etf_dividends') as store:
    events = read_events(store, version, '510300.SH')
```

未传 as_of 表示事后账务事件资料；策略 PIT 读取应传带时区的 as_of，
真实下载时刻之前的观察不会返回。读取缺失基金或未知版本报错，不当作无分红。
供回测按登记日确定权益、除息日确认应收和派息日到账使用；目前没有自动
连接 Engine 公司行为调度，也未重算已有 ETF 策略报告。保存数据不等于
已完成分红账务、份额调整或 ETF 总回报验收。
