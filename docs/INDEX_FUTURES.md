# 股指期货历史日线

本文所列历史下载规模、版本与本机验证仅为适配器开发记录；数据、SDK、凭据和运行输出不随Git交付。新使用者必须准备自己的输入和固定版本，不能假定这些路径已存在。

## 当月／下月跨期价差研究

默认读取最新快照并立即固定版本；用 `--version VERSION` 重现指定版本。
默认读取关联行情版本的 futures_contract_snapshots，固定 revision 清单；
用 `--metadata-version VERSION` 固定元数据版本。537 个合约资料已下载，详见
[合约元数据](INDEX_FUTURES_METADATA.md)。当前 expiry 与 delivery 一致；未来若不同，
程序明确要求核对换月口径。资料用于事后统计，不冒充历史策略可见的 PIT 信息。
上市首月使用最早挂牌月份（如 IF 在 2010-04-16 使用 IF1005/IF1006）。
缺少关联元数据时退出码为 2，并输出 BLOCKED.md、blocked.json 和 missing_deliveries.csv。

也可以依据可靠历史记录填写 CSV 的 `instrument,delivery,source`（日期为 YYYY-MM-DD，
source 保留官方记录出处），然后传入 `--deliveries PATH`。
绘图需要研究依赖：安装 `.[research]`。程序不联网取数，不从最后行情日期推断交割日。
自然月合约交割日仍是近月，交割之后使用随后两个连续月份；缺少相邻月份时留空，
不替代为季月。统计基准是各品种已观察日期并集，不能发现所有合约同时缺行情的日期。

完整运行输出 `daily.csv`、`statistics.csv`、`provenance.json`、`REPORT.md`、
`spreads.png` 和 `distributions.png`。价差为下月减当月，收盘为主、结算对照；
相对价差为价差除以近月收盘，以比例保存。统计包含全期及逐年样本数、缺失数、
均值、中位数、样本标准差、极值及所有对应日期、线性插值分位数和正负零占比。
输入快照逐文件核对 SHA256 和行数，交割 CSV 指纹写入报告，原始数据保持不变。

2026-10-09 已从 iFinD 下载并发布 IF、IC、IM、IH 实际月份合约历史日线；不混入主连或连续合约。供应商字段由用户的 SuperCommand 命令确认，历史访问从 2010-01-01 开始，完整交易日截止 2026-10-08。

| 品种 | 合约数 | 日线条数 | 最早交易日 | 最新交易日 |
| --- | ---: | ---: | --- | --- |
| IF | 201 | 16000 | 2010-04-16 | 2026-10-08 |
| IC | 141 | 11156 | 2015-04-16 | 2026-10-08 |
| IM | 54 | 4076 | 2022-07-22 | 2026-10-08 |
| IH | 141 | 11156 | 2015-04-16 | 2026-10-08 |

合计 537 个合约、42388 条记录。最早到期 IF1005 的历史查询通过。候选代码来自本机官方 CODE_FT.dat 的实际月份代码，另补充供应商查询验证通过的 2610、2611、2703 合约。目录完整性尚未与官方历史上市清单独立核对。

## 文件与字段

根目录 `data/index_futures`：

- `metadata.duckdb`：`futures_daily_snapshots` 保存版本、创建时间、内容指纹、文件清单和校验报告；`metadata` 的 `observed_futures_daily_catalog` 保存带有效／可知／记录时间的当次目录观察，不冒充历史上市或交割事实。
- `daily/<version>/<instrument>.parquet`：每个实际合约一个不可变 Zstd 文件，清单包含行数与 SHA256。
- `staging/20261008`：未发布的原始请求与响应缓存，可断点续传。缓存不会被视为有效快照。
- `outputs/ifind-index-futures-validation.json`：各合约起止日期、行数及全量校验结果。

行情列为 instrument、product、trading_date、OHLC、volume、open_interest、settlement_price、previous_settlement_price、source_amount。价格等数值以 Decimal 写入 Parquet decimal128(28,8)，成交量为整数，交易日期为 date32。

`settlement` → settlement_price；`preSettlement` → previous_settlement_price。直接保留供应商的两项数据，不由移位、收盘价或零值补齐。最终交割结算与日常结算仍使用同一个 settlement_price，将来结合交割日元数据解释。

source_amount 保留 iFinD 原始成交额，单位暂标供应商原生，不能当成人民币元直接参与计算；volume 同样保留供应商整数，单／双边统计口径尚待独立核实。

本数据集只使用交易日期；没有给历史日线附加猜测的开收盘时间。它与 Store.bars 使用的带时间戳 Bar 清单分开管理。接入 Engine 前仍须装配历史交易日历、时段、合约生命周期和交易规则。

## 重跑与读取

本次本地任务清单在 `outputs/ifind-index-futures-download.json`，包含代码、起止日期、批大小及缓存目录；不含凭据。配置变更日期会生成不同请求缓存。重新下载同日期的修订数据应配置新缓存目录，避免复用旧响应。

```powershell
# 已有响应：无需登录，重新校验、发布；相同数据复用同一快照
.venv\Scripts\python.exe -m scripts.download_index_futures outputs\ifind-index-futures-download.json --publish-only

# 新任务或中断续传：账号由环境变量读取；所有批次成功才发布
.venv\Scripts\python.exe -m scripts.download_index_futures outputs\ifind-index-futures-download.json
```

读取时先固定版本，再使用清单指定的 Parquet 文件，不对 daily 目录递归 glob，否则会混合不同历史版本：

```python
import json
from pathlib import Path
import duckdb

root = Path("data/index_futures")
db = duckdb.connect(str(root / "metadata.duckdb"), read_only=True)
version, manifest = db.execute(
    "SELECT version, manifest FROM futures_daily_snapshots ORDER BY created_at DESC LIMIT 1"
).fetchone()
files = [str(root / entry["path"]) for entry in json.loads(manifest)]
daily = db.from_parquet(files)
print(daily.filter("instrument = 'IF1005.CFE'").order("trading_date").df())
db.close()
```

## 校验及限度

关键字段无空值，OHLC、正结算价、非负成交量／成交额／持仓量通过校验。相同主键冲突报错；同品种返回日期并集之内，每个合约首末记录之间无缺口；相邻记录 settlement_price 与下一记录 previous_settlement_price 完全一致。Parquet 读回逐条相等，重复发布复用原版本，旧版本不可变。

这不是独立交易所日历审计：所有合约一起缺失的日期、上市日前后边界、供应商历史修订和统计口径仍需官方元数据对账。缺字段或检测出的日期／结算连续性异常会阻止发布，缓存保留以供排查。旧的 Engine Bar 存储／查询契约不受本次日期型落地表改变。
