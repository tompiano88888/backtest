# 中国多资产回测基础设施

当前版本0.1.0，具备统一撮合、账务、每日结算、组合状态与版本化数据存储；完整V1尚未验收。

## 两组当前研究

|研究|当前节点|净PnL|夏普|
|---|---|---:|---:|
|期现套利|5—8月分红调整＋滚动寻参＋额外开仓下限|360.2477万元|1.5373|
|R-Breaker|历史手续费＋ATR20／1%风险＋避免平今费对锁|1989.8846万元|0.969945|

- [套利规则、配置与复现](docs/RESEARCH_CHECKPOINT.md)。
- [R-Breaker规则、封存与复现](docs/RBREAKER_CHECKPOINT.md)、[执行路由](docs/RBREAKER_FEE_AVOIDANCE.md)。
- [信号归因](docs/RBREAKER_SIGNAL_DIAGNOSTICS.md)、[年度多维图](docs/RBREAKER_YEARLY_DIAGNOSTICS.md)。
- [无限易R-Breaker模拟盘适配](docs/RBREAKER_INFINITRADER_SIMULATION.md)：独立执行层，原策略与柜台90%双重准入，不改变回测节点；实际柜台验收另列。
- [程序依赖与永久清理记录](docs/RESEARCH_MAINTENANCE.md)。旧方案不作为默认入口。
- [2026-10-22两组节点统一回测层重跑](outputs/node_backtests_20261022/report.md)：R-Breaker原样复现；套利按当前信号经Engine日频撮合，[执行方法与差异](docs/CASH_CARRY_CALLBACK.md)。

## 框架与能力边界

策略通过外部回调／指令接入统一Engine/FeeService/Ledger。支持部分成交、组合重试、FIFO账务、每日结算、显式账户事件、独立集合竞价、可选历史手续费和执行期复核。资金准入由研究层配置，不改变默认框架规则。

当前真实分钟验证集中于股指期货；15%保证金为研究假设，分钟行情不提供Tick路径或盘口排队保证。历史规则／PIT、统一Provider、独立日历调度、跨市场和其他资产真实分钟验收仍待完成。期现套利仍为独立日频研究。

- [完整需求](SPEC.md) · [实际实现与限制](IMPLEMENTATION.md) · [接口](docs/API.md)
- [分钟验收](docs/MINUTE_ENGINE_ACCEPTANCE.md) · [历史费用](docs/EXECUTION_FEES.md) · [账户事件](docs/ACCOUNT_EVENTS.md)
- [本地数据清单](docs/LOCAL_IFIND_DATA.md) · [iFinD接入](docs/IFIND.md) · [费用数据](docs/INDEX_FUTURES_FEES.md)

## 环境和检查

每次完整回测默认生成[标准绩效报告](docs/STANDARD_PERFORMANCE.md)：蓝色PnL线、回撤面积图、年度收益/回撤、夏普/卡尔玛等；收益红、亏损绿、中性指标黑。报告保存在结果目录的`standard_report/`，通用JSON结果则放在同名子目录下。

2026-10-27备份迁移已恢复本机离线回测；统一入口、验证结果和SDK待办见[本机工作流恢复](docs/WORKFLOW_RESTORE.md)。

Python 3.12+；优先使用项目虚拟环境。依赖见pyproject.toml及requirements.lock。

```powershell
uv --cache-dir .uv-cache venv .venv
uv --cache-dir .uv-cache pip install --python .venv\Scripts\python.exe -e ".[dev,research]"
.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
.venv\Scripts\python.exe -m backtest demo --output outputs/demo.json
.venv\Scripts\python.exe -m backtest run examples/account_events.synthetic.json --output outputs/account_events/result.json
```

iFinD凭据仅使用IFIND_USERNAME／IFIND_PASSWORD环境变量。真实数据已落地；具体覆盖、未完成下载与质量限制以数据文档为准。不可变快照不得原地覆盖，未完成任务不得发布为完整版本。
