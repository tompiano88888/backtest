# 中国多资产回测基础设施

当前版本为 **0.1.0：需求基线 + 第一批可运行核心**，不是完整 V1 的生产验收版本。

- [完整需求](SPEC.md)：本轮讨论的有效结论、数据契约、账务与组合规则。
- [实现进度和限制](IMPLEMENTATION.md)：已实现能力、待接通部分、真实数据验收条件。
- [接口说明](docs/API.md)：研究输入、撮合时钟、存储及更新方法。

## 安装和运行

Python 3.12+。项目使用 DuckDB、PyArrow；Windows 时区支持通过 tzdata、pytz 提供。

```powershell
uv --cache-dir .uv-cache venv .venv
uv --cache-dir .uv-cache pip install --python .venv\Scripts\python.exe -e ".[dev]"
.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
.venv\Scripts\python.exe -m backtest demo --output outputs\demo.json
```

当前工作区已经创建 `.venv` 并安装测试依赖；即使未安装 editable 包，也可以在项目根目录执行上面的 `python -m backtest` 命令。

示例使用明确标记的合成行情：两腿组合中 B 连续三个 Bar 缺行情，A 在第四个 Bar 退出。初始现金 10000，A 两手、乘数 10，101 开仓、104 平仓，每手单边费用 2；预期最终现金 **10052**、PnL **52**、四条组合警示，期末无持仓。

## 本地数据

```powershell
.venv\Scripts\python.exe -m backtest ingest examples\bars.jsonl --frequency 1m --root data
.venv\Scripts\python.exe -m backtest metadata examples\metadata.jsonl --root data
```

命令返回不可变行情版本。导入会按标的、交易日和时间戳合并，不产生重复行情。每次新版本仍保存旧版本读取得到的内容；元数据建议先于行情快照导入，使行情版本固定的元数据截止点包含所需记录。

## 研究调用

直接使用 `Engine.submit` 提交带决定时间的实际手数，或由 `Engine.step(frame, on_bar=...)` 驱动只读回调。
`python -m backtest run scenario.json --output outputs/result.json` 可重放包含完整标准行情 Frame 和交易指令的 JSON 场景。
这是显式事件输入接口；当前尚未自动把全量行情仓库、交易日历和历史保证金参数组装成生产回测任务。

## iFinD

已提供 SDK 传输层（`THS_HQ` / `THS_HF`）、列映射、重试、校验、版本发布及 `scripts/update.ps1`。
本工作区尚未配置 iFinDPy SDK、凭据、指标和真实响应样本，因此**没有执行真实 iFinD 下载，也没有创建系统定时任务**。

SDK 须安装到使用本项目的 Python 环境。凭据通过 `IFIND_USERNAME`、`IFIND_PASSWORD` 环境变量提供，不放在配置文件中。请求配置模板是 `examples/ifind.template.json`，其中大写占位值必须通过 iFinD 超级命令工具及真实响应确定，不可直接运行。
实际原始分钟数据通常需要额外处理时区、Bar 边界、夜盘交易日和单位；列映射本身不能代替这些校验。

完成配置后，可用 Windows Task Scheduler 调用：

```powershell
powershell -NoProfile -File scripts\update.ps1 -Config your-ifind-config.json -DataRoot data
```

## 设计约束

一个资金池；保证金仅报告风险；期货按品种多空保证金较大侧计收；期权权利金与持仓负债独立记账；数量指令无资金限制；组合分腿成交、三 Bar 超时退出。所有仿真假设在结果 JSON 中披露。

测试是合成数据的正确性检查，不代表已完成真实市场规则校准。下一阶段按 `IMPLEMENTATION.md` 推进。
