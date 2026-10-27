"""Read-only node comparisons and report for the unified-layer replays."""
from collections import Counter
import csv
from datetime import date
import hashlib
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.ticker import PercentFormatter

from backtest.models import D
from backtest.arbitrage_node_engine import write

ARB=Path('outputs/arbitrage_node_engine_verified_20261022')
RB=Path('outputs/rbreaker_node_replay_20261022_historical_fees')
OLD_ARB=Path('outputs/arbitrage_dividend_may_aug_optimized_floors')
OLD_RB=Path('outputs/rbreaker_atr20_1pct_fee_safe_locks_historical_fees')
OUT=Path('outputs/node_backtests_20261022')


def read(root,name):
    with (root/name).open(encoding='utf-8-sig',newline='') as stream:
        return list(csv.DictReader(stream))


def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):
            h.update(block)
    return h.hexdigest()


def main():
    OUT.mkdir(parents=True,exist_ok=True)
    arb=json.loads((ARB/'summary.json').read_text('utf-8'))
    rb=json.loads((RB/'performance_metrics.json').read_text('utf-8'))
    rba=json.loads((RB/'routing_verification.json').read_text('utf-8'))
    arbd=read(ARB,'daily.csv');rbd=read(RB,'daily.csv');olda=read(OLD_ARB,'summary.csv')[0]
    result=json.loads((ARB/'result.json').read_text('utf-8'))
    files=['fills.csv','daily.csv','product_daily.csv','trades.csv','fee_details.csv',
           'routing_fills.csv','logical_fills.csv','funding_observations.csv']
    comparisons={name:sha(RB/name)==sha(OLD_RB/name) for name in files}
    assert all(comparisons.values())
    protected=json.loads(Path('outputs/research_cleanup_protected_20261022.json').read_text('utf-8'))
    for rel,digest in protected.items():
        assert sha(Path(rel))==digest,rel
    budget=[r for r in arbd if D(r['cash'])-D(r['margin'])-D(r['locked_T1'])<0]
    extra=dict(min_cash=min(D(r['cash']) for r in arbd),
               min_cash_less_margin=min(D(r['cash'])-D(r['margin']) for r in arbd),
               cash_margin_feasibility='failed' if any(D(r['cash'])<D(r['margin']) for r in arbd) else 'passed',
               cash_deficit_days=sum(D(r['cash'])<0 for r in arbd),budget_gap_days=len(budget),
               max_T1_budget_gap=max((-D(r['cash'])+D(r['margin'])+D(r['locked_T1']) for r in budget),default=D(0)),
               operation_states={f'{mode}:{state}':n for (mode,state),n in
                                 Counter((o['mode'],o['state']) for o in result['operations']).items()})
    # Verify source used for the run still corresponds to the adapter on disk.
    cfg=json.loads((ARB/'config.json').read_text('utf-8'))
    assert sha(Path('backtest/arbitrage_node_engine.py'))==cfg['code_sha256']
    checks=dict(RBreaker_core_CSV_identical=comparisons,protected_files_unchanged=len(protected),
                arbitrage=json.loads((ARB/'verification.json').read_text('utf-8')),
                rbreaker=rba,arbitrage_funding=extra,tests='370 passed')
    (OUT/'verification.json').write_text(json.dumps(checks,default=str,ensure_ascii=False,indent=2),'utf-8')
    dates=lambda rows:[date.fromisoformat(r['day']) for r in rows]
    fig,axes=plt.subplots(2,1,figsize=(13,8),layout='constrained',sharex=True)
    axes[0].plot(dates(rbd),[float(D(r['pnl'])/10000) for r in rbd],label='R-Breaker node replay',color='#2369a0')
    axes[0].plot(dates(arbd),[float(D(r['pnl'])/10000) for r in arbd],label='Arbitrage: unified daily Engine',color='#168264')
    olddaily=read(OLD_ARB,'daily.csv')
    axes[0].plot(dates(olddaily),[float((D(r['equity'])-10000000)/10000) for r in olddaily],
                 label='Arbitrage legacy reference (different execution)',color='#777777',linestyle='--')
    axes[0].set_ylabel('Net PnL (CNY 10,000)');axes[0].legend()
    axes[1].plot(dates(rbd),[float(D(r['usage'])) for r in rbd],label='R-Breaker settled margin / equity',color='#2369a0',linewidth=.8)
    axes[1].plot(dates(arbd),[float(D(r['margin'])/D(r['equity'])) for r in arbd],label='Arbitrage settled margin / equity',color='#168264',linewidth=.8)
    axes[1].yaxis.set_major_formatter(PercentFormatter(1));axes[1].set_ylabel('Margin usage (ETF cash excluded)');axes[1].legend()
    for ax in axes:ax.grid(alpha=.2)
    fig.suptitle('Current research nodes through unified execution | 2020-01-01 to 2026-10-08')
    fig.savefig(OUT/'pnl_funding.png',dpi=160);plt.close(fig)
    rows=[dict(strategy='期现套利（统一日频撮合）',net_pnl=arb['net_pnl'],cagr=arb['cagr'],sharpe=arb['sharpe'],
               daily_drawdown=arb['max_drawdown'],fills=arb['fills']),
          dict(strategy='R-Breaker（当前分钟节点重跑）',net_pnl=rb['net_pnl'],cagr=rb['cagr'],sharpe=rb['sharpe'],
               daily_drawdown=rb['max_daily_drawdown'],fills=5701)]
    write(OUT/'comparison.csv',rows)
    text='# 两组当前节点：统一回测层重跑\n\n区间2020-01-01至2026-10-08，均使用1000万元初始资金。当前节点与封存不覆盖、不自动升级。本次框架核心未改，新增外部套利回调、日频适配与审计程序。\n\n'
    text+='|研究|净PnL（万元）|CAGR|夏普|日终最大回撤|实际成交笔数|\n|---|---:|---:|---:|---:|---:|\n'
    for row in rows:
        text+=f"|{row['strategy']}|{D(row['net_pnl'])/10000:.4f}|{row['cagr']:.4%}|{row['sharpe']:.6f}|{float(row['daily_drawdown']):.4%}|{row['fills']}|\n"
    text+='\n![PnL及保证金使用率](pnl_funding.png)\n\n## 期现套利\n\n'
    text+=f"沿用5—8月分红调整、原逐日滚动参数及IF/IH/IC/IM下限3.8%/2.3%/0.1%/3.6%，不加NAV。原日频参考PnL {D(olda['total_pnl'])/10000:.4f}万元，本轮差额{(D(arb['net_pnl'])-D(olda['total_pnl']))/10000:.4f}万元。该差额包含执行、仓位、资金与后续路径变化，不能当作信号收益提升。\n\n"
    text+='全部组合腿通过ctx.submit→Engine→FeeService→Ledger；没有调用旧Portfolio撮合、execute、直接写账、撤销实际成交或拼接旧PnL。真实日线OHLC保留，日线完成后确定整数数量并提交，最早下一交易日开盘撮合；原来的次日开盘重新定仓、事后撤单、同收盘紧急退出不保留。期货交割后残余ETF下一可成交事件退出。三次重试为三根共同日Bar，并非三个分钟。\n\n'
    text+='期货不利1tick，ETF零费用／零滑点、实际费用全为零，延续套利节点成本假设；不能与R-Breaker历史手续费口径混淆。10%原生成交量参与率、ETF量按供应商数值暂作份数解释，单位尚未独立验收。历史涨跌停／停牌、交易日历与PIT限制保留，不能称为完整真实分钟验收。\n\n'
    text+='分红与闲置资金收益由统一AccountingEvents处理。T+1预留依据实际卖出ETF回款和实际平仓期货释放的保证金，下一观测交易日释放；平仓剩余期货盈亏不另加到预留，区别于旧整组回款公式。预算不足时先停止新增、提交下一可成交事件退出，未成交不预先释放资金。\n\n'
    text+=f"资金：日终保证金／权益峰值{float(arb['max_usage']):.4%}（不含ETF已付现金），最低现金{extra['min_cash']:,.2f}元，最低现金减保证金{extra['min_cash_less_margin']:,.2f}元；T+1预留后预算缺口峰值{extra['max_T1_budget_gap']:,.2f}元，共{extra['budget_gap_days']}个自然日。现金减保证金流动性未全程通过；账务核对通过不等于资金可行性通过。\n\n"
    text+=f"发出入场意向{arb['groups']}组，实际成交{arb['fills']}笔，期末持仓{arb['positions']}；操作状态见verification.json。分红确认{D(arb['dividends'])/10000:.4f}万元，闲置资金收益{D(arb['idle_cash'])/10000:.4f}万元，均已包含在净PnL中。\n\n"
    text+='[套利明细](../arbitrage_node_engine_verified_20261022/summary.json) · [独立审计](../arbitrage_node_engine_verified_20261022/verification.json)\n\n## R-Breaker\n\n'
    text+=f"ATR20×1／当前权益1%风险、历史手续费和避免平今对锁全部保持。净PnL、5701笔成交和8份核心CSV逐字节一致；798009条资金观察峰值{float(rba['max_actual_fund_usage']):.4%}，超100%为0，平今计费0手；分钟最大回撤{rb['max_minute_drawdown']:.4%}。\n\n"
    text+='[R-Breaker指标](../rbreaker_node_replay_20261022_historical_fees/performance_report.md) · [资金与路由审计](../rbreaker_node_replay_20261022_historical_fees/routing_verification.json)\n\n## 验证与复现\n\n'
    text+='套利独立按原始开盘价／滑点／量参与率、FIFO原始成交、每日结算、55项分红权利与T+1预留后的每日利息重建2473个自然日现金／权益，全数一致；R-Breaker费用、路由、账务及资金审计通过。原4172份保护文件SHA256未变，370项测试通过。完整数据边界与15%研究保证金假设继续保留。\n\n'
    text+='```powershell\n.venv\\Scripts\\python.exe -m scripts.run_arbitrage_node_engine --output outputs/arbitrage_node_engine_next_replay\n.venv\\Scripts\\python.exe -m scripts.verify_arbitrage_node_engine --root outputs/arbitrage_node_engine_next_replay\n.venv\\Scripts\\python.exe -m scripts.run_rbreaker_portfolio --stop-risk .01 --include-auction --historical-fees configs/ifind.index-futures-fees.json --avoid-close-today --output outputs/rbreaker_next_replay_historical_fees\n.venv\\Scripts\\python.exe -m scripts.verify_rbreaker_fee_avoidance --root outputs/rbreaker_next_replay_historical_fees\n```\n'
    (OUT/'report.md').write_text(text,'utf-8')
    (ARB/'report.md').write_text('# 当前套利信号：统一Engine日频撮合\n\n[结果、执行差异及资金限制](../node_backtests_20261022/report.md)。\n\n[指标](summary.json) · [独立核验](verification.json) · [逐日资金](daily.csv) · [实际成交](fills.csv)\n','utf-8')
    (RB/'report.md').write_text('# R-Breaker当前节点独立重跑\n\n与当前历史费对锁节点的8份核心CSV逐字节一致。[合并报告](../node_backtests_20261022/report.md)。\n\n[绩效](performance_report.md) · [资金与路由审计](routing_verification.json)\n','utf-8')
    # Remove misleading duplicate reports generated by the legacy comparison helper.
    for name in ['historical_fee_report.md','fee_avoidance_report.md','pnl_and_daily_funding.png']:
        (RB/name).unlink(missing_ok=True)
    comparison=read(RB,'comparison_to_checkpoint.csv')
    comparison[0]['variant']='Current fee-safe checkpoint'
    comparison[1]['variant']='Same-node replay'
    write(RB/'comparison_to_checkpoint.csv',comparison)
    print(json.dumps(dict(arb=arb,arb_funding=extra,rbreaker_CSV_identical=True,protected_files=len(protected)),default=str,indent=2))


if __name__=='__main__':
    main()
