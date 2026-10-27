# 期现套利指数与ETF分钟供应商观察

## 本次状态：供应商周额度阻塞，尚未完成

已完成109/664个分段、518632条记录：000016.SH全区间394517条，
000300.SH部分区间124115条。iFinD返回-4302，官方THS_GetErrorInfo明确解释为
本周行情提取量超过1.5亿。未发布完整快照，其余标的尚未开始批量下载。
缓存与状态保存在`data/index_etf_minute/download_status.json`，额度恢复后运行同一入口续传。
这些分段或`progress.json`不能当作完整数据集。

2026-10-20用户授权，沿用上一项股指期货分钟任务的2020年起至2026-10-08范围。
指数000300.SH/000905.SH/000852.SH/000016.SH，ETF510300.SH/510500.SH/512100.SH/510050.SH。
固定日线快照`6feaff33a4ac4320832a06e488b1794a`，8个标的、13096个标的交易日。
计划在`outputs/ifind-index-etf-minute-plan.json`；仅使用market日线清单，不将NAV独有日期算作交易日。

入口：`.venv\Scripts\python.exe -m scripts.download_index_etf_minute`。
共享期货分钟下载器的分段缓存、精确十进制字符串Parquet、不可变文件及完成后发布机制，
根目录独立为`data/index_etf_minute`，不覆盖期货或原日线数据。
THS_HF字段为已通过真实样本验证的open/high/low/close/volume/amount，
明确使用`Interval:1,CPS:no,Fill:Original`，不复权、不前值填充，不下载/拼入每日净值。
每次30个自然日，所有分段通过后才发布`metadata.duckdb/index_etf_minute_snapshots`。
进行中进度为`progress.json`，完整快照在`validation.json`。

指数只是参考数据，不作为交易资产；ETF为fund。资产及品种映射固定在任务清单中。
价格严格校验，指数成交量可有小数，ETF非空成交量必须为整数，量额源空值原样保留。
缺量额的记录不能直接进入撮合。请求失败、缺交易日期、非法价格或主键冲突不发布完整快照。
原始响应也会保留供排查；原始响应与Parquet均有SHA256。

供应商时间戳保留为北京时间墙钟`source_timestamp`，不猜测分钟起止或集合竞价映射。
样本含09:30独立记录，不能直接映射成09:29—09:30的Engine Bar。
分钟量额统计单位、时间边界及分钟覆盖仍须独立核对，不能把观察落地说成统一框架接入验收。
读取时必须固定快照并按manifest读取，不能glob不同历史版本的所有文件。

新增2项指数/ETF边界校验，加原期货测试及全项目共248项测试通过；
使用`--basetemp outputs/pytest-index-etf-minute-20261020`规避沙箱临时目录权限。
