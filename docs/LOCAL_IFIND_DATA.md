# 本地 iFinD 数据目录与结构

2026-10-19增量：四个全收益指数H00300.CSI/H00016.CSI/H00905.CSI/H00852.CSI共21379条日线已独立存储data/index_total_return，固定版本3f899b4d11604eef86a318e7496f9741，截止2026-10-08。IH自2003-12-31，其余自2004-12-31；回溯起点不代表发布日期，不作撮合资产。验证报告outputs/ifind-index-total-return-validation.json，采集配置examples/ifind.index-total-return-daily.json。与旧目录及以下旧清单分开，不覆盖原版本。

核对日期：2026-10-12。依据本机 DuckDB 表、已发布版本清单和 Parquet Schema，
明细机器清单见 `outputs/ifind-local-data-inventory.json`。本次只核对结构和清单，
未重新联网下载、未重新执行全量业务验收。下列条数按指定快照统计，不能将
全部版本目录或 staging 缓存的行数直接相加。

## 数据类型与覆盖范围

| 类型 | 已落地范围 | 当前快照规模 | 存放位置 |
| --- | --- | ---: | --- |
| 股指期货日线 | IF、IC、IM、IH 实际月份合约；截至 2026-10-08 | 537 合约，42388 行 | data/index_futures/daily，Parquet |
| 期货合约元数据 | 上述 537 合约；每份原始观察含用户提供的 71 个指标 | 537 标准资料＋537 原始观察 | data/index_futures/metadata.duckdb |
| 指数日线 | 000300、000905、000852、000016.SH；截至 2026-10-08 | 22099 行 | data/index_etf/daily，Parquet |
| ETF 市场日线 | 510300、510500、512100、510050.SH；截至 2026-10-08 | 14461 行 | data/index_etf/daily，Parquet |
| ETF 每日单位净值 | 同上四只 ETF；截至 2026-10-08 | 14486 日期观察，14473 非空 | data/index_etf/nav_daily，Parquet |
| ETF 分红元数据 | 用户清单 1349 只；查询截止 2026-10-12 | 102 只返回587笔事件；另1247只未返回事件 | data/etf_dividends/metadata.duckdb |
| 股票分钟联调样本 | 300033.SZ，2026-09-21 09:31–09:35 | 5 行 | outputs/ifind-sample-store，Parquet＋DuckDB |

指数与 ETF 市场日线合计 36560 行。净值序列与 ETF 市场日期存在重叠，
不能把 14486 条净值全部当作新增交易日。净值独有日期共25条；其余对应市场行情日期。
13 条净值缺失保留 null。分红587笔中585笔截至截止日已除息，2笔已公告待除息；
最早公告为2006-05-13。未返回事件不等于已验证不存在分红。

期货覆盖：

| 品种 | 合约数 | 日线行数 | 最早观察日 |
| --- | ---: | ---: | --- |
| IF | 201 | 16000 | 2010-04-16 |
| IC | 141 | 11156 | 2015-04-16 |
| IM | 54 | 4076 | 2022-07-22 |
| IH | 141 | 11156 | 2015-04-16 |

指数及 ETF 覆盖（最早返回日不是已经核实的发布／上市日）：

| 代码 | 类型 | 日线行数 | 最早观察日 |
| --- | --- | ---: | --- |
| 000300.SH | 沪深300指数 | 6004 | 2002-01-04 |
| 000905.SH | 中证500指数 | 5284 | 2004-12-31 |
| 000852.SH | 中证1000指数 | 5284 | 2004-12-31 |
| 000016.SH | 上证50指数 | 5527 | 2003-12-31 |
| 510300.SH | ETF | 3497 | 2012-04-24 |
| 510500.SH | ETF | 3301 | 2013-01-29 |
| 512100.SH | ETF | 2409 | 2016-11-04 |
| 510050.SH | ETF | 5254 | 2005-02-23 |

## 文件组织

```text
data/
  index_futures/
    metadata.duckdb
    daily/<日线版本>/<实际合约>.parquet
    staging/20261008/                 原始日线请求与响应
    contract-staging/20261009/        原始合约资料请求与响应
  index_etf/
    metadata.duckdb
    daily/<版本>/<指数或ETF代码>.parquet
    nav_daily/<含净值版本>/<ETF代码>.parquet
    staging/20261008/                 原始行情／净值请求与响应
  etf_dividends/
    metadata.duckdb
    staging/<请求哈希>.json           原始分红请求与响应
    validation-<版本>.json
outputs/
  ifind-sample-store/                 五条分钟数据的联调回放
  ifind-*-probe.json                  真实接口小样本
  ifind-*-validation.json             下载／质量核对结果
  ifind-*-verification.json           读回／版本核对材料
```

Parquet 保存时间序列；DuckDB 保存元数据和固定文件清单；JSON 缓存保留原始
请求、响应与观察时刻。staging 是获取和续传材料，以 DuckDB 已提交快照为发布依据。
outputs 中策略的成交、收益、参数试验和图表是研究产物，不是新增 iFinD 源数据。

## Parquet 标准字段

### 股指期货日线

每个实际合约一个版本化文件，业务主键为 `(instrument, trading_date)`。

- 标识与日期：instrument、product、trading_date。
- 原始行情：open、high、low、close、volume、open_interest、source_amount。
- 结算：settlement_price、previous_settlement_price。

日期为 date32，volume 为 int64，其余数值为 decimal128(28,8)。
日常与最终交割结算共用 settlement_price，结合合约交割日期解释。
source_amount、volume 保留供应商原生单位与统计口径，尚未完成独立单位核验。

### 指数／ETF 市场日线

业务主键同样为 `(instrument, trading_date)`。

- 标识：instrument、asset（index/fund）、product、trading_date。
- 行情：open、high、low、close、source_volume、source_amount。
- 质量标记：ohlcv_complete。
- 含净值新快照另有：unit_nav、unit_nav_known_at；指数对应字段为空。

数值为 decimal128(30,8)，日期为 date32，可知时刻为带 UTC 时区 timestamp。
20条观察缺成交量／成交额，其中ETF18条；保留 null 并标记不完整。
没有原始价格复权、前值填充或通过基金净值补造市场行情。

### ETF 完整净值序列

字段为 instrument、trading_date、unit_nav、unit_nav_known_at。
同日期净值也出现在ETF行情行，单独序列保留上市前等净值独有日期。
unit_nav 是 THS_HQ netAssetValue 的每日单位净值，使用 decimal128(30,8)，
不是盘中 IOPV。可知时间保守使用查询时刻，不假设当日收盘前已经发布。

### 股票分钟样本

含 instrument、start、end、trading_date、OHLC、volume、turnover、suspended，
以及当前为空的 settlement_price、upper_limit、lower_limit、open_interest。
start/end 带 Asia/Shanghai 时区，volume 为整数，价格／成交额为 Decimal。
这是捕获真实响应后的本地回放样本，不是完整股票分钟数据仓库；时间边界仍待核实。

## DuckDB 表结构与业务内容

三个 data 根目录各有独立数据库，不共享一个全局数据库。它们都有以下通用表：

| 表 | 作用 |
| --- | --- |
| metadata | 历史元数据及供应商观察；标准时态字段＋JSON payload |
| datasets | 通用带时间戳 Bar 的版本清单；三个正式数据根目录中目前均为空 |
| update_runs | 通用更新任务日志；三个正式数据根目录中目前均为空 |

`metadata` 列为 revision、kind、key、valid_from、valid_to、known_at、recorded_at、payload。
revision 是记录版本，kind 区分业务类型，key 定位合约／事件；payload 为 JSON。
有效时间描述事实适用时期，可知时间描述资料何时可获得，recorded_at 描述本地写入时间。
日期序列资料的历史日期和查询日期不能混为一个时间。

### index_futures/metadata.duckdb

- futures_daily_snapshots：version、created_at、fingerprint、manifest、report。
  manifest 固定537个 Parquet 路径、行数与 SHA256。
- futures_contract_snapshots：version、created_at、daily_version、fingerprint、revisions、report。
  daily_version 关联日线快照，revisions 固定合约核心资料和原始观察。
- metadata 含三类各537条：instrument、ifind_contract_observation、observed_futures_daily_catalog。

instrument 标准 payload：id、name、asset、account、exchange、product、currency、underlying、
listed_date、first_trade_date、expiry、delivery、multiplier、tick、lot、observed_sessions，
以及 source、session_scope、pit_status。expiry 与 delivery 分别存储。
原始观察保留71项供应商指标，包含当前保证金／费用等；它们不等于已核实的历史规则。

### index_etf/metadata.duckdb

- index_etf_daily_snapshots：version、created_at、fingerprint、manifest、report。
  manifest 是8个市场行情文件；含净值版本的 report.nav_manifest 固定另4个净值文件。
- metadata.kind=observed_index_etf_catalog：目前16个修订、8个代码，对应新旧两次版本目录观察。
  该目录不等于完整历史 ETF 证券主数据或指数发布日期资料。

### etf_dividends/metadata.duckdb

- etf_dividend_snapshots：version、created_at、fingerprint、revisions、report。
  revisions 固定每只基金的分红事件，report 包含全1349只代码的覆盖与累计核对状态。
- metadata.kind=etf_dividend：587笔标准事件。
- metadata.kind=ifind_etf_dividend_observation：1936条原始查询观察，保留17项供应商字段，
  包括年度／累计／区间汇总、分红条款和分红日期／方案。

分红标准 payload 为 instrument、announcement_date、record_date、payment_date、
exchange_ex_date、otc_ex_date、base_date、cash_per_unit、currency、distribution_plan、
source、known_at。每次分红以基金代码和公告日定位，cash_per_unit 是每份现金金额。
基金全体分红金额不能直接作为本账户分红，实际收款取决于登记日持有份额。

## 固定版本与读取

| 数据 | 当前使用版本 |
| --- | --- |
| 股指期货日线 | 0fff65706ce54991a3725f1bcbb6c6c9 |
| 关联期货合约资料 | f12d3b245a824432a144c967bbe3bc36 |
| 指数／ETF行情＋每日净值 | 6feaff33a4ac4320832a06e488b1794a |
| 不含净值的原指数／ETF行情 | a14a83ddc5574482b8407bca420c5014 |
| ETF分红 | fc5b0209d6f86935f26477238c1a50aceee8c1b1a43e73404ce0df7ecb947339 |

读取时先固定版本，再读取清单指向的文件；不要对 daily/nav_daily 整体递归 glob。
净值有 nav_from_snapshot，分红有 read_events，合约资料有 contract_from_snapshot；
接口细节见 [API](API.md)。金额 JSON 往往保存为字符串，读取后转回 Decimal。

## 当前使用边界

已有期货、指数和ETF日级研究可以读取这些固定资料；ETF分红已接入统一账户研究
分支的登记／应收／派息处理。完整生产 Engine 的历史 Provider、预期时钟、交易日历、
历史费用／保证金规则和公司行为调度尚未全部装配。

尚未形成正式历史数据集的内容包括：全市场股票／ETF分钟数据、期权行情与合约资料、
ETF／指数历史成分、独立历史交易日历、PIT主连映射、全历史交易规则／费用／保证金、
ETF份额折算及期权合约调整等。没有盘中 IOPV 数据。
期货连续价差、基差、策略净值和信号是在研究层计算的派生产物。

合约资料、分红和基金净值保守使用真实查询时刻作为可知时间；历史事实已保存，
但尚不能将其全部认定为对应历史决策时点可见的数据。旧快照保留历史修订，
数据发布不等于完整 V1 回测能力已验收。

详细说明：[期货日线](INDEX_FUTURES.md)、[合约资料](INDEX_FUTURES_METADATA.md)、
[指数／ETF与净值](INDEX_ETF_DAILY.md)、[ETF分红](ETF_DIVIDENDS.md)。
