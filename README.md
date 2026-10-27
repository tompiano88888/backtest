# AKShare 股票、期货日线回测

`backtest_akshare` 从原统一框架分出，公共入口只提供 **AKShare 日线下载和股票／实际期货合约回测**。沿用 Decimal 账务、统一资金池、下一 Bar 开盘撮合、T+1、参与率、费用、组合重试及期货逐日结算。不提供分钟、竞价、ETF、期权或 iFinD CLI。

## 安装与运行

Python 3.12+：

```bash
python -m pip install -e '.[dev]'
python -m backtest update examples/akshare.daily.json --validate-only
python -m backtest update examples/akshare.daily.json --root data/akshare
# 上一步打印不可变数据版本，填入下面的 VERSION
python -m backtest run examples/akshare.daily.json --root data/akshare --version VERSION --output outputs/result.json
python -m pytest -q -p no:cacheprovider
```

示例使用真实股票代码，但交易时段、涨跌停价及费用是**合成配置示范**，不代表真实历史规则；实证回测须替换。`update` 才访问网络，`run` 固定版本离线读取，不会自动追随最新数据。下载或校验失败不发布快照，不填充缺行情，不回退到其他供应商。

## 数据与配置

- 股票：`stock_zh_a_hist(period="daily", adjust="")`，成交量由手乘 100 转股，原始价撮合；A 股要求 `lot=100`、`sell_delay_days=1`。
- 期货：`get_futures_daily`，支持 CFFEX、SHFE、DCE、CZCE、INE、GFEX；按实际合约筛选，成交量保持手。当日 `settle` 是每日结算价，缺失直接失败，禁止用 `close` 替代。主连／指数合约不能交易。
- `instruments`：原框架 Instrument 定义；期货须明确乘数、tick、到期日和交割日；`margin_rules` 明确保证金研究参数。
- `symbols`：内部代码到 AKShare 代码的显式映射。
- `sessions`：每个预期合约交易日的 `instrument/trading_date/start/end/upper_limit/lower_limit`。时间须有时区，夜盘起点属于上一自然日时也须明确提供。不能从当日是否返回行情反推预期时钟。缺预期行情拒绝整批发布。
- `instructions`、`execution`、`execution_by_instrument`：沿用原框架目标持仓／多腿指令、手续费、滑点和参与率参数。此分支支持显式固定费用假设，不加载原 iFinD 历史费率库。

行情唯一在线来源为 AKShare。合约参数、历史时段、涨跌停和费率是显式仿真输入；**尚未自动从 AKShare 获取并审定全部历史规则**。不能用当前参数冒充全历史真实规则。证券短仓、北交所／科创板特殊申报单位等原核心边界未在此扩展；当前股票配置采用普通 A 股 100 股单位。

## 回调

```python
from backtest.akshare_daily import run
from backtest.models import Instruction

def on_bar(ctx):
    if not ctx.positions:
        ctx.submit([Instruction("600000.SH", 1, 100)])

engine, scenario = run(config, "data/akshare", version, on_bar=on_bar)
```

完整日线在收盘后可见，回调指令最早下一允许日线开盘成交，末日决策不会回填到当日开盘。日线成交量参与率是简化假设，无法还原盘中路径或排队。

## 当前边界

未自动获取分红送转、历史股票池、历史主连映射或退市信息；不会把复权行情当现金分红账务。当前日线入口拒绝 `accounting`，包含公司行为的股票长期回测尚不能视为总回报回测。期货交割沿用原框架现金结清约定；若交割日无行情，须另建明确结算事件，不能把普通收盘价冒充最终交割价，此入口尚未自动生成这种事件。跨市场不同收盘时间的全账户日终、自动展期与完整历史 PIT 元数据仍待完善。

原 `ifind*.py`、分钟／期权核心及旧文档保留为原框架历史代码和回归参考，不属于此分支公共日线入口的支持范围。没有删除已有数据，也没有调用 iFinD。详细原核心 API 见 `docs/API.md`；分支范围以本 README 为准。
