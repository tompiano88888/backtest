# iFinD股指期货历史手续费

本文所列历史下载规模、版本与本机验证仅为适配器开发记录；数据、SDK、凭据和运行输出不随Git交付。新使用者必须准备自己的输入和固定版本，不能假定这些路径已存在。

2026-10-20已完成本仓库固定日线版本内全部537个IF/IH/IC/IM实际月份合约的手续费日期序列导入，覆盖2010-04-16至2026-10-08各合约已有行情日期，共42388条合约日记录（IF16000、IH11156、IC11156、IM4076）。范围没有自动延长到本次下载日，也不包含主连代码、股指期权或仓库范围外的新合约。

费用库：`data/execution_fees/metadata.duckdb`。固定费用版本：`bf211dd35fc143b6a7d62bf4a483aa98`；知识截止：`2026-10-20T06:55:57.828971+00:00`。关联日线版本：`0fff65706ce54991a3725f1bcbb6c6c9`。

## 字段与单位

沿用用户此前确认的合约字段，先用THS_BD逐日查询，再用[官方THS_DS日期序列接口](https://quantapi.51ifind.com/gwstatic/static/ds_web/quantapi-web/help-center/manual.html)对照2023-03-01至03-22样本，历史变更一致后批量下载。

| iFinD字段 | 标准字段 |
| --- | --- |
| ths_transaction_procedure_rate_future | 开仓及普通平仓比例费 |
| ths_transaction_procedure_fee_future | 开仓及普通平仓按手费 |
| ths_to_transaction_procedure_rate_future | 平今比例费 |
| ths_to_transaction_procedure_fee_future | 平今按手费 |

比例原值按千分口径除以1000转换为名义金额小数费率；按手费保持人民币/手。样本原值0.023转换0.000023，0.345转换0.000345，0.23转换0.00023；IF2306在2023-03-20平今从0.345切换为0.23，与[中金所2023年费率调整通知的会员转发](https://www.sywgqh.com.cn/Pc/Customer_Service/Announcement/117522.html)所述万分之二点三一致。原始精确十进制字符串始终保留。

同类费用的比例或按手费至少有一项明确有值，另一项null才作为未启用分项记零；两项均缺失、负值、非有限值、日期缺失或重复均报错，禁止整体缺费填零。全量数据的按手字段均为null、比例字段有值，计费按比例执行。

## 落库与校验

- 537条`metadata.kind=ifind_fee_observation`保留每个合约整批原始查询、时间与数据；537份JSON缓存位于`data/execution_fees/staging/20261020`，SHA256清单写入validation.json。
- 646条`metadata.kind=fee_rule`按观察到的费率变化分段，匹配规则为today_first。有效边界为北京交易日零点，valid_to不含；末尾截至最后行情日次日，禁止推断区间之后费率。休市日包含于相邻相同费率段的时间区间，不用于产生虚构成交。
- known_at是本次实际取得时间，不回填历史公告时间；供应商修订/PIT并未全面验收。费率用于回放历史交易日费用，不等于策略当年已获得该信息。
- `futures_fee_snapshots`保存固定版本、关联日线、原始/标准revision清单与校验报告。全部合约通过日期及原行情SHA核对后，在单一事务中发布；相同完整缓存重复发布复用同版本。
- 独立脚本未调用适配层标准化函数，逐条重算原始费率与标准规则、按固定行情日期对账，并核对原始缓存及DuckDB观察一致；42388条全部通过，缺失和重复均为零。

独立审计发现IF在2012-06-01至2012-06-15共11个交易日，不同月份合约原始普通费率不同。以2012-06-01为例，IF1206普通/平今原值为0.032/0.035，IF1209为0.035/0.035；独立THS_BD重查仍一致。2012-12-03的0.001低费率原值也已用THS_BD复查，仍一致。均保留供应商原值，未人工统一或修正。**供应商历史下载和算术对账通过，不表示所有历史费率已逐公告审定**；尤其上述早期差异仍需供应商或交易所资料进一步核实。此次不覆盖历史回测结果、不自动修改研究费率模式；未纳入交割费、申报费、佣金和历史保证金等其他项目。

## 使用与复现

```powershell
# 首次下载/断点续传，完成后统一发布
.venv\Scripts\python.exe -m scripts.download_index_futures_fees
# 已有缓存离线校验发布，无需登录
.venv\Scripts\python.exe -m scripts.download_index_futures_fees --publish-only
# 独立核对
.venv\Scripts\python.exe -m scripts.verify_index_futures_fees
```

若修订查询日期/字段或重新取得供应商版本，请使用新的--cache目录，不覆盖本批原始观察。连接本机SDK时，受限进程登录返回-340，沙箱外以相同环境凭据登录和下载成功；此次未遇到周额度限制。不在日志或源码保存凭据。

按[统一手续费配置示例](EXECUTION_FEES.md)创建自己的本地配置，将execution及historical_fees合并到完整run_scenario配置，指令或回调均可；其中1tick为示例，可按研究设置修改。每合约完整execution配置必须同样显式启用historical，否则该合约会保留其自身fixed模式。金额佣金和按手佣金默认为零。接口详见[统一手续费](EXECUTION_FEES.md)。

原始小样本及复查：`outputs/ifind_futures_fees_import/probe.json`、`probe_ds.json`、`recheck.json`。全量报告：`validation.json`、`independent_verification.json`及`cross_contract_differences.csv`。

验证结果：9项新增适配测试，全套354项通过；compileall、demo与实际费用配置读取通过，离线重复发布版本一致。验证摘要见outputs/ifind_futures_fees_import/checks.json。
