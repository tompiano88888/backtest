# 统一回测手续费与历史费率

手续费由Engine的FeeService统一报价，Ledger按实际Fill.fee扣账。研究层提交数量及运行配置，不提交手续费金额。普通Bar、离散竞价、部分成交与execution_guard缩量报价共用计费逻辑；报价无副作用，仅实际成交更新计费余额。竞价仍零滑点但照常收手续费和佣金。

## 运行配置

指令流水`run_scenario(config)`和回调`run_scenario(config,on_bar=...)`使用同一配置：

```json
{
  "execution": {
    "participation": "0.1",
    "slippage_ticks": 1,
    "fee_mode": "fixed",
    "fee_matching": "fifo",
    "fee_rate": "0.000023",
    "fee_per_unit": "0",
    "close_today_fee_rate": "0.00023",
    "close_today_fee_per_unit": "0",
    "commission_rate": "0.000001",
    "commission_per_unit": "1"
  },
  "execution_by_instrument": {
    "IF2612.CFE": {
      "participation": "0.1",
      "slippage_ticks": 1,
      "fee_mode": "fixed",
      "fee_matching": "today_first",
      "fee_rate": "0.000023",
      "close_today_fee_rate": "0.00023",
      "commission_rate": "0",
      "commission_per_unit": "0"
    }
  }
}
```

以上费率仅是研究配置示例，不是经验证的历史真实费率。合约配置完整替换全局ExecutionConfig，不逐字段继承；未写字段使用ExecutionConfig默认值。费用均为非负有限Decimal，slippage_ticks为非负整数；佣金默认零。原配置默认fixed/fifo，沿用旧行为。

固定模式基础费用为数量×fee_per_unit＋实际成交名义金额×fee_rate。平今对应字段为None时分别沿用普通值，显式零表示免收该项。佣金始终另加数量×commission_per_unit＋实际成交名义金额×commission_rate，不随交易所平今减免打折。按手字段对证券表示每单位数量费用；本次不增加最低佣金、印花税等专项规则。

## 平今计费匹配

- `fifo`：按原账务FIFO、指定group实际消耗的批次交易日确定平今数量。
- `today_first`：统一账户同合约、同持仓方向的当日实际开仓量构成独立计费余额，跨group汇总；平仓优先消耗这个余额，剩余数量收普通平仓费用。
- 昨多10、今日买开3、随后卖平5：3手平今、2手普通；随后余额为零，即使账务中今天开仓批次仍存在也不重复计平今。
- 先平昨再开不追溯，反向开仓对锁按开仓费用，多空方向和不同月份合约分别统计。使用trading_date而非自然日，下一交易日余额重置。

实际持仓、成本、FIFO和group归属不随计费匹配改变。不按品种名称硬编码；研究固定配置或历史合约规则明确选择today_first，其他合约继续fifo。新建Engine默认从无成交开始；直接修改Ledger或中途注入今仓不构成支持的计费余额恢复入口。

## 历史费率输入

历史模式`fee_mode="historical"`使用成交trading_date对应的规则，忽略固定基础费率字段，仍应用配置的佣金与滑点。历史模式不自动回退；缺Provider、缺区间或冲突均报错。通过同一个全局配置或合约完整配置启用。

```json
{
  "execution": {"fee_mode": "historical", "slippage_ticks": 1},
  "historical_fees": {
    "root": "data/execution_fees",
    "version": "导入后发布的固定版本",
    "as_of": "2026-10-20T16:00:00+08:00"
  }
}
```

费用库沿用Store元数据表，kind=`fee_rule`，key为实际合约代码。valid_from、valid_to为带时区有效区间，右端不含，建议北京时间交易日零点；known_at为真实公告可知时间，recorded_at由Store写入。payload完整提供：

```json
{
  "open_rate": "0.000023",
  "open_per_unit": "0",
  "close_rate": "0.000023",
  "close_per_unit": "0",
  "close_today_rate": "0.00023",
  "close_today_per_unit": "0",
  "matching": "today_first",
  "source": "供应商记录或交易所公告来源"
}
```

所有rate都是成交名义金额的小数比例，per_unit都是人民币/手（或证券单位），不是万分数原始值。iFinD原字段、单位及有效期必须在适配/导入层核实后标准化。

```python
from backtest.storage import Store
from backtest.fees import publish_fee_snapshot, HistoricalFees

with Store("data/execution_fees") as store:
    revision = store.put_metadata("fee_rule", contract, valid_from, known_at,
                                  payload, valid_to=valid_to)
    version = publish_fee_snapshot(store, source="已验证的费率导入批次")
provider = HistoricalFees("data/execution_fees", version, as_of)
# 直接构造Engine时用fee_provider=provider。
```

建议费用使用独立root；publish_fee_snapshot发布无行情分区的元数据版本，不需要虚构Bar。发布验证字段、金额和匹配模式；读取时再按as_of验证可见规则的重叠冲突，成交时验证覆盖。元数据快照发布本身不表示完整区间费率已验收。导入后应关闭Store写连接再创建只读Provider。

Provider启动时一次性读取固定datasets版本的recorded_at截止与固定as_of以内记录，随后不查询活动数据库。完全相同有效区间的修订选最新可知版本，同一可知时点不同修订或不同区间重叠报错。有效日期按北京时间交易日零点查询。as_of表示本次研究采用的知识截止，不保证该资料在历史成交时已经可知；不能用下载时间伪造历史公告时间。

初始接口开发时仅有合约观察字段，使用合成规则验证。后续2026-10-20已独立下载日期序列并导入537合约、42388合约日历史费用，固定版本及供应商/PIT限制见[历史手续费导入](INDEX_FUTURES_FEES.md)；R-Breaker已显式启用该版本另行重跑，见[重跑说明](RBREAKER_HISTORICAL_FEES.md)。合约观察字段仍不能代替历史规则序列。

## 审计输出

Engine.fees.details及runner.result.fee_details逐笔提供instruction、time、trading_date、normal_quantity、close_today_quantity、exchange_fee、commission、total、rule_revision、rule_source、matching和mode。normal_quantity在开仓时表示普通开仓数量；在平仓时表示非平今数量。

result.fee_provenance记录历史版本和as_of。明细与本次Engine真实成交一一对应，total=exchange_fee+commission=Fill.fee，合计等于Ledger手续费现金流的相反数。原Fill结构及旧研究输出不改写；旧独立日频研究路径不在此次迁移范围。
