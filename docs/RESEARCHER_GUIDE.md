# 研究员与AI接入指引

## 交付边界

框架负责执行、费用、账务、估值、结算、诊断与标准结果；研究负责数据选择、信号、参数、目标数量、策略风险预算和实验记录。研究代码在独立项目中维护，或放本仓库被Git忽略的research/、strategies/。不向backtest/、scripts/、tests/或configs/提交具体策略、模型、寻参脚本和研究参数。

克隆当前main并安装框架后，先运行README中的demo和合成回调示例。它们只验证接口契约，不代表投资策略或行情可成交性验收。需要纯当前快照时可使用main的浅克隆；仓库历史仍保留旧提交。

## 两条策略入口

1. 显式场景：构造config中的instruments、历史规则、frames与不带成交价格的instructions，执行`python -m backtest run scenario.json --output outputs/<unique>/result.json`。
2. 外部回调：构造同样的时钟与数据，传入自己的on_bar。完整示例见[external_callback.py](../examples/external_callback.py)。

```python
from backtest.runner import run_scenario

engine = run_scenario(
    config,
    on_bar=my_callback,
    strategy={"name": "my-experiment", "params": my_parameters},
    output="outputs/unique-run/result.json",
)
```

所有完整持久化运行都传output。返回Engine用于进一步只读核验；框架自动写结果JSON和标准绩效图，研究代码不需要再实现绘图或收益统计。纯内存run_scenario不产生文件，供单元测试/嵌入式编排使用，不能据此省略最终完整报告。

`ctx`提供time、bars、auctions、marks、account、positions、warnings，以及submit、target、target_group、target_weight、fills_since等接口。使用独立group区分组合，不将互不相关持仓混在同组。持续目标未更新不意味着清仓，显式零目标才退出。回调只能观察当时信息，通过指令改变状态，不能直接修改账本或指定成交价。

普通Bar完成后生成的指令最早在下一允许Bar执行。集合竞价只有事前指令能参与；不可用竞价结果回填成交。夜盘交易日、交易时段、价格/数量单位、结算和公司行为由明确数据与日历提供，不按自然日期猜测。

## 自定义逐帧调度

需要自定义调度时调用Engine.step；运行结束调用engine.finalize()汇总未完成操作。如果保留了与snapshots一一对应的显式frames，可使用runner.save_result(engine, config, path)获得统一JSON和图表。

否则调用方应保存完整审计结果，并收集真实全账户日终权益，再调用框架`export_performance(daily_rows, initial_cash, start_date, output, title=...)`。每行包含day、equity，可选is_market_day。不要把单个市场先收盘时的权益冒充全账户日终。报告模块不会替调用方猜测缺失时钟，也不会改变盈亏。

## 输入及诊断责任

- 固定数据版本、历史费用/保证金、as_of和运行配置；known_at为取得时间时不得冒充历史公告时间。
- 股票/基金用未复权行情撮合，公司行为通过显式账户事件记账。期货使用真实结算价，缺字段不能用收盘或零填充。
- 账户统一资金池，保证金只作风险统计。自定义资金准入通过策略/执行守卫显式定义，不改变框架默认成交规则。
- 读取ctx.warnings设计应对；最终还要检查warnings和residual。最后一次回调之后的警告不会再回传策略，不忽略未成交或期末持仓。
- 同一初始资金且无外部入金出金是标准指标输入前提。样本不足、零波动和非正权益的不适用指标保留空值及说明。
- 并发回放显式使用Store(read_only=True)，写入任务与只读回放分时；它不是OneDrive跨电脑写锁。

## 标准成果

`result.json`含配置/代码/策略来源、数据版本、成交、费用、账务、持仓、警告和期末残留。同名目录下standard_report保存四项文件：绩效PNG、指标JSON、年度CSV、逐日CSV。样式和统计公式由框架统一维护，见[标准报告](STANDARD_PERFORMANCE.md)。

每次实验使用独立目录，额外在研究侧记录完整参数、命令、代码提交、输入快照和独立对账结果。研究侧的胜率、信号分类和参数比较可附加输出，不覆盖或改写框架标准指标。

## 给AI的任务模板

> 请只把此仓库作为回测依赖，先读AGENTS.md、SPEC.md、IMPLEMENTATION.md及docs/API.md。在独立研究目录实现我指定的策略，不修改框架撮合、费用或账务来迎合策略收益。固定数据版本，按当时可见信息生成实际数量指令，通过统一Engine执行。完整运行必须调用run_scenario(output=...)或框架finalizer，输出标准报告并检查warnings/residual与资金缺口。不得向框架Git提交策略、训练结果、参数表、数据或运行产物。发现缺少框架能力时先给出最小通用能力需求及可手工核对案例。
