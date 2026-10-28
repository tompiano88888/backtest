# 中国多资产回测框架

供研究员或AI编写外部策略使用的独立回测层：统一撮合、费用、资金池账务、结算、组合状态机与标准绩效报告。仓库不提供策略信号、寻参模型、研究节点或交易平台适配；完整V1尚未验收。

## 安装与验证

Python 3.12+；在新工作目录创建本机环境，不复用另一台电脑的虚拟环境。

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.lock
.venv\Scripts\python.exe -m pip install --no-deps -e .
.venv\Scripts\python.exe -m pip check
.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
.venv\Scripts\python.exe -m backtest demo --output outputs/demo/result.json
.venv\Scripts\python.exe examples/external_callback.py --output outputs/callback/result.json
```

demo和外部回调示例使用合成数据，不需要供应商账号或本地研究文件。真实数据、历史规则和时钟需调用方显式准备。

## 接入与输出

外部策略传入`on_bar`回调或不含成交价格的指令。`run_scenario(config, on_bar=callback, output=...)`统一保存结果及标准图表；不传output只适用于内存组合/测试。策略不得直接修改Ledger。回调身份与参数写入provenance，未成交原因与期末残留写入结果。

完整输出为结果JSON及同名目录中的`standard_report/performance.png`、`metrics.json`、`annual.csv`、`daily.csv`。涨红跌绿、中性黑、蓝色PnL线、原回撤面积图；包含年度收益/回撤、夏普、卡尔玛等。

- [研究员与AI接入指引](docs/RESEARCHER_GUIDE.md)
- [接口](docs/API.md) · [标准报告](docs/STANDARD_PERFORMANCE.md)
- [需求规格](SPEC.md) · [实现状态与限制](IMPLEMENTATION.md)
- [账户事件](docs/ACCOUNT_EVENTS.md) · [费用](docs/EXECUTION_FEES.md) · [数据接口](docs/IFIND.md)

## 协作边界

Git只管理框架。研究放独立仓库或被忽略的research/strategies目录；数据与输出被忽略。多电脑各用独立仓库和虚拟环境；OneDrive仅交接已关闭数据库、已完成的快照与结果，不同时写共享Git目录或DuckDB。并发只读须显式使用Store(read_only=True)，它不提供跨电脑分布式锁。

2026-10-28起main当前树剥离研究材料；此前提交仍在Git历史中。仅需要干净当前框架可浅克隆main。Git历史清除和其他分支处理不属于这次主线目录整理。

框架交付文件由framework_files.json显式登记；新增通用模块/测试/文档需审查后更新清单。运行`python -m scripts.check_framework_scope`检查未登记文件及误入Git的研究材料。清单不是自动判断代码语义的替代，更新时仍须人工审查。
