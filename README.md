# 中国多资产回测基础设施

Python 3.12+，版本 0.1.0。包含统一撮合与账务、组合部分成交、每日结算、分红和可配置闲置资金收益，以及 iFinD 数据接口。本版本尚未完成完整 V1 验收。

- [需求规格](SPEC.md)
- [实现进度与限制](IMPLEMENTATION.md)
- [框架接口](docs/API.md)
- [账户事件](docs/ACCOUNT_EVENTS.md)
- [iFinD 接入](docs/IFIND.md)
- [本地数据结构](docs/LOCAL_IFIND_DATA.md)

## 安装与验证

```powershell
uv --cache-dir .uv-cache venv .venv
uv --cache-dir .uv-cache pip install --python .venv\Scripts\python.exe -e ".[dev]"
.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
.venv\Scripts\python.exe -m backtest demo --output outputs\demo.json
.venv\Scripts\python.exe -m backtest run examples/account_events.synthetic.json --output outputs/account_events/result.json
```

组合合成示例预期 PnL 52、四条警示。策略通过只读 on_bar 上下文提交指令，框架负责后续撮合、费用、估值和账务。显式 Frame 必须提供真实交易日历与日终事件；当前没有自动装配全历史分钟回放任务。

## 数据

行情采用不可变 Parquet，元数据与版本清单采用 DuckDB。修订创建新版本；历史数据与凭据不包含在仓库中。

iFinD 只从 IFIND_USERNAME / IFIND_PASSWORD 环境变量读取凭据。安装官方 SDK 后可执行：

```powershell
.venv\Scripts\python.exe -m backtest ifind-check --login
.venv\Scripts\python.exe -m backtest update examples/ifind.template.json --validate-only
.venv\Scripts\python.exe -m backtest ingest examples/bars.jsonl --frequency 1m --root data
.venv\Scripts\python.exe -m backtest metadata examples/metadata.jsonl --root data
```

供应商字段、单位、权限和历史可知时间须单独核验。示例不是通用全市场生产配置。

- [股指期货日线](docs/INDEX_FUTURES.md)
- [合约元数据](docs/INDEX_FUTURES_METADATA.md)
- [指数与 ETF 日线、单位净值](docs/INDEX_ETF_DAILY.md)
- [ETF 分红元数据](docs/ETF_DIVIDENDS.md)

## 账务边界

统一资金池；保证金只做风险提示。期货逐日盯市，期权按权利金资产／负债估值；交割日按配置现金结清。组合允许部分成交并按共同交易时钟重试。

股票／ETF 分红支持登记日锁资格、除息应收和派息到账。闲置收益默认关闭，启用时按显式历史年率计息，不代表真实回购成交。历史规则装配、完整股票税额与份额调整、真实分钟数据验收仍待完成。

## 2026-10-20 回测框架费用版本

统一撮合支持按真实成交计费、平今FIFO或today_first匹配、比例/按手佣金加点、固定费率研究及固定版本历史费率。指令流水与回调共用配置，普通/竞价/缩量费用同源。

- [费用配置、时序与审计](docs/EXECUTION_FEES.md)
- [股指期货历史费用导入、固定版本及供应商限制](docs/INDEX_FUTURES_FEES.md)
- [离散竞价与分钟层验收范围](docs/MINUTE_ENGINE_ACCEPTANCE.md)

历史费用配置片段：configs/ifind.index-futures-fees.json。行情、DuckDB、供应商缓存和回测结果不随Git分发；取得数据并发布本地费用快照后，配置对应版本及as_of。当前固定本地供应商版本含537个合约、42388个合约日、646段规则；早期供应商差异及公告PIT限制保留。本版本不包含策略研究模块。
