# 四个股指对应指数与 ETF 日线

本文所列历史下载规模、版本与本机验证仅为适配器开发记录；数据、SDK、凭据和运行输出不随Git交付。新使用者必须准备自己的输入和固定版本，不能假定这些路径已存在。

2026-10-09 从 iFinD 下载并发布，原始价格、不复权、不前值填充。任务从 1990-01-01 查询，保存全部返回日期，截止 2026-10-08。

| 股指品种 | 指数 | 指数代码 | ETF | 指数记录数 | ETF 记录数 |
| --- | --- | --- | --- | ---: | ---: |
| IF | 沪深300 | 000300.SH | 510300.SH | 6004 | 3497 |
| IC | 中证500 | 000905.SH | 510500.SH | 5284 | 3301 |
| IM | 中证1000 | 000852.SH | 512100.SH | 5284 | 2409 |
| IH | 上证50 | 000016.SH | 510050.SH | 5527 | 5254 |

指数代码来自已落地实际合约资料中的 underlying。ETF 采用用户指定代码。合计 36560 条供应商日线观察。

## 存储与字段

`data/index_etf/metadata.duckdb` 的 index_etf_daily_snapshots 保存版本、创建时间、内容指纹、不可变文件清单与质量报告；日期型 Parquet 位于 daily/<version>/<code>.parquet，清单附带 SHA256。metadata 的 observed_index_etf_catalog 记录带有效／可知／记录时间的目录观察，不冒充历史证券主数据。

行情字段：instrument、asset（index 或 fund）、product（对应 IF/IC/IM/IH）、trading_date、open、high、low、close、source_volume、source_amount、ohlcv_complete。
2026-10-12 启用每日净值后，新快照另含 unit_nav、unit_nav_known_at；指数对应列为空，旧快照仍保留原 Schema。

OHLC 与供应商原生成交量／成交额使用 Decimal，保留为 Parquet decimal128(30,8)，日期为 date32。指数原生成交量可能带小数，不截断成整数或套用期货单位。ETF 非空成交量须为整数；其计量单位仍作为供应商原生口径保存。

指数只是数据输入，不成为可交易资产；ETF 为 fund。这里没有附加未经验证的交易时段，没有装配 Engine Frame，也没有复权、股息或份额调整账务。

## 真实响应中的缺值

全部价格字段均通过 OHLC 校验，但部分供应商记录没有成交量和成交额：000905.SH 与 000852.SH 各 1 条；510300.SH 为 8 条，510500.SH 为 9 条，512100.SH 为 1 条。源数据空值原样保留并置 ohlcv_complete=false，不补零、不中断日期后伪造完整历史。这是原始观察快照，不能把它整体当作已验收的可撮合行情。

ETF 第一条完整 OHLCV：510300 为 2012-05-28、510500 为 2013-03-15、512100 为 2016-11-04、510050 为 2005-02-23。它们只是响应里的完整行情边界，不等于已经核实的上市日期。未来撮合 Provider 必须拒绝成交量缺失记录；零成交量也不能保证成交。

指数的最早返回日分别为 000300：2002-01-04、000905／000852：2004-12-31、000016：2003-12-31。供应商回溯指数数据不证明该指数当时已经发布或可知；未来历史策略还须结合指数发布日期、PIT 成分和真实 ETF 上市日。

## 重跑与读取

任务配置为 examples/ifind.index-etf-daily.json，无凭据。每个标的一个完整历史请求，缓存按完整请求哈希保存；修改日期／参数生成不同缓存。获取修订请指定新缓存目录。

```powershell
.venv\Scripts\python.exe -m scripts.download_index_etf_daily
.venv\Scripts\python.exe -m scripts.download_index_etf_daily --publish-only
```

相同数据重复发布复用原版本并核验文件校验和。下载失败、缺少标的、OHLC 异常或主键冲突均不发布部分版本。

固定版本读取，不对不同版本目录整体 glob：

```python
import json
from pathlib import Path
import duckdb

root = Path("data/index_etf")
db = duckdb.connect(str(root / "metadata.duckdb"), read_only=True)
version, manifest = db.execute(
    "SELECT version, manifest FROM index_etf_daily_snapshots ORDER BY created_at DESC LIMIT 1"
).fetchone()
daily = db.from_parquet([str(root / e["path"]) for e in json.loads(manifest)])
print(daily.filter("instrument = '510300.SH' AND ohlcv_complete").order("trading_date").df())
db.close()
```

报告位于 outputs/ifind-index-etf-validation.json，包含每个标的起止日期、完整数据边界、缺值日期列表和行数。全量 Parquet 逐条读回一致；独立交易所日历、上市边界、单位和公司行为仍需另行核验。

## 每日基金单位净值

默认配置新增 `include_etf_nav: true`，ETF 的原 THS_HQ 指令追加 `netAssetValue`，
指数请求保持原字段。2026-10-12 通过官方 SDK 的真实样本及全历史返回验证；
例如 510300.SH 在 2026-09-21 收盘价为 4.608，单位净值为 4.6121。
`unit_nav` 使用 Decimal／decimal128(30,8)，没有复权、前值填充或用收盘价补缺。
这是每日基金单位净值，不是盘中 IOPV，也不能作为市场撮合价。

行情文件带上同日期的净值；完整净值序列另外保存在
`nav_daily/<version>/<code>.parquet`，同一 DuckDB 快照 report 内的 `nav_manifest`
固定文件路径、行数及 SHA256。净值独有日期不进入市场行情清单，防止伪造成交数据。
这两个清单同时发布，净值值修订及净值独有日期修订都会生成新快照；重复导入复用原版本。
旧配置未启用此项时仍可按原 Schema 离线发布，旧文件及版本不覆盖。

已发布版本 `6feaff33a4ac4320832a06e488b1794a`，截止仍为 2026-10-08：

| ETF | 净值序列记录数 | 非空净值 | 净值独有日期 |
| --- | ---: | ---: | ---: |
| 510300.SH | 3500 | 3493 | 3 |
| 510500.SH | 3306 | 3300 | 5 |
| 512100.SH | 2415 | 2415 | 6 |
| 510050.SH | 5265 | 5265 | 11 |

共 14486 条日期观察、14473 条非空净值；13 条源净值缺失保留 null，质量报告
列出确切日期。没有净值字段、全基金净值均缺失、非法／非正净值、部分价格字段
缺失或数据冲突会报错，不发布不完整版本。25 条净值独有观察包含上市前日期，
不将该边界当作已验证成立／上市日期。36560 条原行情逐条比较完全相同，
旧版本文件校验和不变。核对材料在 `outputs/ifind-index-etf-nav-verification.json`。

`unit_nav_known_at` 保守使用查询时间，净值为空时该时间也为空。
尚未核实历史发布时间，不能默认将当天净值用于当天收盘前的策略决策。
完整序列读取接口验证校验和；传 as_of 会按该查询时间进行 PIT 过滤：

```python
from backtest.storage import Store
from backtest.ifind_index_etf import nav_from_snapshot

with Store('data/index_etf') as store:
    rows = nav_from_snapshot(store, '6feaff33a4ac4320832a06e488b1794a', '510300.SH')
```

该接口对没有净值的旧快照或未知 ETF 报错。数据新增不自动改变已有研究信号、
Ledger 市场收盘估值或生产 Engine 的成交规则。
