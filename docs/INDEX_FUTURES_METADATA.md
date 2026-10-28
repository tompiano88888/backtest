# 股指期货合约元数据

本文所列历史下载规模、版本与本机验证仅为适配器开发记录；数据、SDK、凭据和运行输出不随Git交付。新使用者必须准备自己的输入和固定版本，不能假定这些路径已存在。

2026-10-20另行导入固定范围537个合约的历史手续费日期序列到独立费用库，见[历史手续费](INDEX_FUTURES_FEES.md)。下文2026-10-09合约观察仍为当次基础资料，不自动变成历史费用。

2026-10-09 根据用户提供的 SuperCommand 命令，以 THS_BD 下载固定日线版本中的 537 个 IF、IC、IM、IH 实际合约资料，写入 `data/index_futures/metadata.duckdb`。查询模板为 `examples/ifind.futures-contracts.json`，71 个指标与 71 项参数逐项对应；账号不进入模板或缓存。

## 结构与时间语义

`futures_contract_snapshots` 保存独立元数据版本、关联的日线版本、原始批次内容指纹、537 个合约的核心／原始记录 revision 清单及校验报告。

`metadata.kind=instrument` 的标准化 payload 包含：id、name、asset、account、exchange、product、currency、underlying、listed_date、first_trade_date、expiry、delivery、multiplier、tick、lot、observed_sessions 以及来源与限制说明。expiry 与 delivery 分别保留供应商的最后交易日和最后交割日，不用第三个星期五公式或最后一条行情日期代替。

关键映射：

| 标准字段 | 用户确认的供应商指标 |
| --- | --- |
| listed_date | ths_contract_listed_date_future |
| first_trade_date | ths_start_trade_date_future |
| expiry | ths_last_td_date_future |
| delivery | ths_last_delivery_date_future |
| multiplier | ths_contract_multiplier |
| tick | ths_mini_chg_price_future |
| lot | ths_min_order_qty_cfe_future |
| underlying | ths_underlying_thscode_sif_future |

日期接受 YYYY-MM-DD 与 YYYYMMDD，金额／乘数使用 Decimal，交易单位为正整数。核心账户类别为 futures。

核心记录 valid_from 为上市日北京时间零点，valid_to 为交割日次日北京时间零点；known_at 为该批次实际下载时刻，recorded_at 为入库时刻。不能仅因资料描述了过去就将 known_at 回填为过去。严格历史 PIT 查询在公告可知时间补齐前可能失败，这是防止虚构历史可见性的明确限制。

`metadata.kind=ifind_contract_observation` 保留完整 71 项原始指标，valid_from／known_at 为实际下载时间，valid_to 为空。包含当前状态、当前保证金、当前手续费等原始值，不把它们转成覆盖历史的标准保证金率或费率；指标单位不一致（例如 12.0 与 0.12），须单独核验再适配。

## 交易时间的重要限制

四个日间字段 ths_open_time_day_future、ths_close_time_am_future、ths_open_time_pm_future、ths_close_time_day_future 被保存为两个 observed_sessions。夜盘字段在本次股指期货样本为空。

真实样本 IF1005、IC1505、IH1505 都返回当前 09:30–11:30／13:00–15:00 模板，不能视为该历史合约当年的时段。中金所 [2013 年 IF 交易细则](https://www.cffex.com.cn/u/cms/www/202003/27165308xfog.pdf) 与 [2015 年 IC 交易细则](https://www.cffex.com.cn/u/cms/www/202003/27165856gzzt.pdf) 明确早期普通交易日为 09:15–11:30／13:00–15:15，最后交易日为 09:15–11:30／13:00–15:00。

因此 observed_sessions 带有 session_scope=current template 标记，不自动生成历史 Bar。历史规则版本、生效公告、独立交易日历及异常提前收市仍需装配；本次下载完成不等于历史时钟已完成。

## 校验与查询

537 个核心记录均有上市、起始交易、最后交易、交割日期及正乘数、正 tick、整数 lot。要求 listed_date ≤ first_trade_date ≤ expiry ≤ delivery。日线第一条不得早于首次交易日，最后一条不得晚于最后交易日；已到期合约须包含最终交易日行情。全部批次完整、全部校验成功后以单一事务提交核心记录、原始观察及快照。失败不发布部分元数据，缓存保留以供排查。相同缓存重复发布复用同一版本。

固定日线版本的下载／中断续传：

```powershell
.venv\Scripts\python.exe -m scripts.download_index_futures_metadata --daily-version 0fff65706ce54991a3725f1bcbb6c6c9

# 仅从完整缓存重校验／发布，无需 SDK 登录
.venv\Scripts\python.exe -m scripts.download_index_futures_metadata --daily-version 0fff65706ce54991a3725f1bcbb6c6c9 --publish-only
```

修改模板中的取值日期时要逐项核对参数；新查询或修订采用新的 --cache 目录，不复用旧观察时间。

检查／读取指定元数据版本：

```python
from datetime import datetime, timezone
from backtest.storage import Store
from backtest.ifind_contracts import contract_from_snapshot

with Store("data/index_futures") as store:
    version = store.db.execute(
        "SELECT version FROM futures_contract_snapshots ORDER BY created_at DESC LIMIT 1"
    ).fetchone()[0]
    # 回溯检查过去的有效合约资料，as_of 明确为今天；这不是当年的策略可见数据。
    fact = contract_from_snapshot(
        store, version, "IF1005.CFE",
        datetime(2010, 5, 21, tzinfo=timezone.utc),
        datetime.now(timezone.utc),
    )
    print(fact["expiry"], fact["delivery"], fact["multiplier"])
```

使用 contract_from_snapshot 读取固定 revision 清单；不要把新元数据默默混入旧快照。当前 Store.metadata 的 version 参数仍指 Engine 数据集版本，与上述独立元数据版本不同。

验证报告位于 `outputs/ifind-index-futures-metadata-validation.json`。本次实际样本：IF1005 最后交易／交割日 2010-05-21，IF2611 为 2026-11-20；IC1505 和 IH1505 为 2015-05-15，IM2208 为 2022-08-19。IF/IH 乘数返回 300，IC/IM 返回 200。
