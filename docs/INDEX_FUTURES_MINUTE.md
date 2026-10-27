# 股指期货分钟供应商观察

2026-10-20后续用户确认：09:30记录是集合竞价结果，对应日线开盘价，按结果价成交可不加滑点。固定快照23720合约日全量核对全部相等，见outputs/index_futures_auction_audit.json。回测层新增独立Auction事件及通用撮合，详见[验收范围](MINUTE_ENGINE_ACCEPTANCE.md)；后文“时间戳语义未验收”为此前观察记录，其中09:30部分已由本次确认补充，其他分钟边界、量额与历史交易状态仍保留限制。此前研究结果未加入竞价事件，不自动重跑覆盖。

2026-10-20补充：已基于本快照完成[R-Breaker分钟框架实验](RBREAKER_MINUTE.md)，四品种各一手、单一回调接入原Engine/Ledger，累计毛PnL1238360元，逐日结算与独立PnL核对差异零。该实验显式假设标签为分钟结束并剔除09:30记录，未将本页尚待验收的时间戳、单位及量额问题标为已解决。

2026-10-20 用户授权下载 IF/IH/IC/IM 实际月份合约自2020年起分钟数据，截止与原日线一致的2026-10-08。
范围固定到日线版本 `0fff65706ce54991a3725f1bcbb6c6c9`，309个合约、23720个合约交易日。
任务清单：`outputs/ifind-index-futures-minute-plan.json`。

本次1376个分段全部完成，发布5716520条记录。快照版本：
`df7196984c186d24c4c24fdb1aa87ecbbfcde14d35b20ea12be05c8600ce41c1`。
完整清单在`data/index_futures_minute/validation.json`，额外跨文件审计在`audit.json`。

全量审计：23720个合约交易日均241条，无重复键、无多余/缺失日线日期，2752个文件校验和通过。
IF/IC/IH各85合约、1578068条；IM54合约、982316条。
23688个合约交易日的分钟成交量之和与日线相等，分钟成交额之和等于日线原始amount的10000倍。
仅记录这一观测比例，不据此未经确认就给所有供应商字段标注单位或覆盖原始值。
2023-02-07与2023-06-16各16个合约，共32个合约交易日量额不一致，
详见`daily_aggregate_differences.json`。对IF2302/2023-02-07与IF2306/2023-06-16
独立单日重查仍返回相同成交量（40286与826），原始重查在`discrepancy_requery.json`。
尚不能判断是分钟历史缺陷或日线修订；这些日期不可宣称分钟量额已验收。
本次发布代表完整供应商观察落地，质量差异明确保留，不补造或按日线摊分分钟。

运行：`.venv\Scripts\python.exe -m scripts.download_index_futures_minute`。
每合约按30个自然日分段，先写原始响应缓存，再校验并写不可变Parquet。
重复运行复用请求指纹缓存，校验已有Parquet内容；失败不发布完整快照。
全部分段完成后才写 `data/index_futures_minute/metadata.duckdb` 的
`futures_minute_snapshots` 和 `validation.json`。
进行中状态保存在 `progress.json`，不能当作完整快照。

接口与字段来自用户提供的THS_HF命令，参数保留`Fill:Original`，未添加未验证参数。
保存OHLC、volume、amount、change、sellVolume、buyVolume、openInterest、changeRatio_periodical。
数值以精确十进制字符串存入Parquet，读取计算时须显式转Decimal，禁止转浮点后作为账务金额。
`source_timestamp`保留供应商北京时间墙钟值，不赋予Bar起止语义。
实测2020-01-02包含09:30独立记录；不能盲目将其改成09:29—09:30成交Bar。

校验包括日线文件SHA256、逐合约逐日覆盖、OHLC、非负量额/持仓、整数成交量、重复冲突及Parquet读回相等。
不填充缺失分钟，不将日结算价灌入盘中；原始响应与Parquet各有SHA256。
单位、成交量统计口径、分钟内完整性、集合竞价处理和时间戳起止语义仍需独立核对。
此数据为供应商观察落地，不是统一Engine Bar/Frame接入验收，也未改变策略或账务路径。
