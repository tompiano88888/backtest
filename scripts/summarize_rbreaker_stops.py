"""Standalone stopped-portfolio PnL, calendar CAGR and daily-return statistics."""
import argparse
from collections import defaultdict
import csv
from datetime import date
from decimal import Decimal as D
import json
import math
from pathlib import Path
from statistics import mean, stdev

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter


def read(path):
    with path.open(encoding='utf-8-sig',newline='') as handle:
        return list(csv.DictReader(handle))


def write(path,rows):
    with path.open('w',encoding='utf-8-sig',newline='') as handle:
        writer=csv.DictWriter(handle,fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def run(root=Path('outputs/rbreaker_portfolio_stop_1pct')):
    root=Path(root)
    summary=json.loads((root/'summary.json').read_text())
    daily=read(root/'daily.csv')
    products=read(root/'product_daily.csv')
    trades=read(root/'trades.csv')
    initial=D(summary['config']['initial_cash'])
    start=date.fromisoformat(summary['config']['start'])
    end=date.fromisoformat(daily[-1]['day'])
    final=D(daily[-1]['equity'])
    net_pnl=D(summary['net_pnl'])
    assert final-initial==net_pnl
    elapsed=(end-start).days
    cagr=math.expm1(math.log(float(final/initial))*365.25/elapsed)
    returns=[]
    prior=peak=initial
    peak_day=start.isoformat()
    dd=D(0)
    dd_peak=dd_trough=''
    pnl_by_year=defaultdict(lambda:D(0))
    first_equity_by_year={}
    last_equity_by_year={}
    for row in daily:
        equity=D(row['equity'])
        if prior<=0 or equity<=0:
            raise ValueError('Nonpositive equity; conventional return statistics unavailable')
        returns.append(float(equity/prior-1))
        year=row['day'][:4]
        first_equity_by_year.setdefault(year,prior)
        last_equity_by_year[year]=equity
        pnl_by_year[year]+=D(row['daily_pnl'])
        if equity>peak:
            peak=equity
            peak_day=row['day']
        current=(peak-equity)/peak
        if current>dd:
            dd=current
            dd_peak,dd_trough=peak_day,row['day']
        prior=equity
    volatility=stdev(returns)*math.sqrt(252)
    sharpe=mean(returns)/stdev(returns)*math.sqrt(252)
    minute_dd=D(summary['max_drawdown'])
    calmar=cagr/float(dd)
    daily_products=defaultdict(dict)
    curves=defaultdict(list)
    for row in products:
        daily_products[row['day']][row['product']]=D(row['pnl'])
        curves[row['product']].append((date.fromisoformat(row['day']),D(row['pnl'])))
    for row in daily:
        assert sum(daily_products[row['day']].values(),D(0))==D(row['pnl'])
    costs=defaultdict(lambda:D(0))
    for fill in read(root/'fills.csv'):
        costs[fill['instrument'][:2]]+=D(fill['fee'])
    product_rows=[]
    for product,value in summary['product_pnl'].items():
        pnl=D(value)
        product_rows.append(dict(product=product,net_pnl=pnl,share_of_total_pnl=pnl/net_pnl,
                                 contribution_on_initial_capital=pnl/initial,fees=costs[product]))
    annual_products=defaultdict(lambda:defaultdict(lambda:D(0)))
    previous_products=defaultdict(lambda:D(0))
    for row in products:
        product=row['product']
        pnl=D(row['pnl'])
        annual_products[row['day'][:4]][product]+=pnl-previous_products[product]
        previous_products[product]=pnl
    annual_rows=[]
    for year,pnl in sorted(pnl_by_year.items()):
        assert sum(annual_products[year].values(),D(0))==pnl
        annual_rows.append(dict(year=year+(' YTD' if year==str(end.year) and (end.month,end.day)!=(12,31) else ''),
                                net_pnl=pnl,return_on_year_start=pnl/first_equity_by_year[year],
                                **{p+'_pnl':annual_products[year][p] for p in curves}))
    closed=[r for r in trades if r['exit_time']]
    trade_pnls=[D(r['net_pnl']) for r in closed]
    wins=sum(v>0 for v in trade_pnls)
    gain=sum((v for v in trade_pnls if v>0),D(0))
    loss=-sum((v for v in trade_pnls if v<0),D(0))
    metrics=dict(start=start.isoformat(),first_trading_day=daily[0]['day'],end=end.isoformat(),
                 initial_cash=str(initial),final_equity=str(final),net_pnl=str(net_pnl),
                 total_return=float(final/initial-1),cagr=cagr,sharpe=sharpe,annualized_volatility=volatility,
                 max_daily_drawdown=float(dd),daily_drawdown_peak=dd_peak,daily_drawdown_trough=dd_trough,
                 calmar=calmar,max_minute_drawdown=float(minute_dd),calmar_using_minute_drawdown=cagr/float(minute_dd),
                 trading_days=len(daily),fees=summary['fees'],closed_trades=len(closed),
                 trade_win_rate=wins/len(closed),profit_factor=float(gain/loss),worst_closed_trade=str(min(trade_pnls)),
                 formulas=dict(cagr='(final/initial)^(365.25/calendar elapsed days)-1; initial date=config start',
                               daily_return='equity_t/equity_previous_trading_day-1; first day uses initial cash',
                               sharpe='mean(daily_return)/sample_std(daily_return)*sqrt(252); risk-free rate=0',
                               volatility='sample_std(daily_return)*sqrt(252)',
                               calmar='calendar CAGR / maximum drawdown from daily settled equity, including initial cash',
                               minute_calmar='calendar CAGR / stored maximum minute drawdown',
                               product='shared-pool attribution, includes all actual contracts; no separate product capital'))
    (root/'performance_metrics.json').write_text(json.dumps(metrics,indent=2),encoding='utf-8')
    write(root/'product_performance.csv',product_rows)
    write(root/'annual_performance.csv',annual_rows)
    x=[start]+[date.fromisoformat(r['day']) for r in daily]
    y=[0]+[float(D(r['pnl'])/10000) for r in daily]
    fig,axes=plt.subplots(2,1,figsize=(13,8.5),sharex=True,layout='constrained')
    axes[0].plot(x,y,color='#1F6E8C',linewidth=1.6)
    axes[0].fill_between(x,y,0,color='#1F6E8C',alpha=.10)
    axes[0].set_title(f'Total net PnL: CNY {net_pnl:,.2f} | CAGR {cagr:.2%} | Sharpe {sharpe:.2f} | Calmar {calmar:.2f}',loc='left',fontsize=12)
    colors=dict(IF='#2774AE',IH='#009E73',IC='#D97706',IM='#8B5CF6')
    for product,points in curves.items():
        a,b=zip(*points)
        axes[1].plot(a,[float(v/10000) for v in b],color=colors[product],linewidth=1.3,
                     label=f'{product}: CNY {b[-1]:,.0f}')
    axes[1].set_title('Net PnL attribution by product | All actual contracts included',loc='left')
    axes[1].legend(loc='upper left',ncol=2)
    for ax in axes:
        ax.set_ylabel('Cumulative PnL (CNY 10,000)')
        ax.yaxis.set_major_formatter(FuncFormatter(lambda value,_:f'{value:,.0f}'))
        ax.axhline(0,color='#777777',linewidth=.6)
        ax.grid(alpha=.2)
    batch_mode=summary['config']['stop'].get('mode')=='batch_cash'
    policy_title='5% prior-equity margin batches + 0.5% cash stop per batch' if batch_mode else '1% trade risk + prior ATR20'
    if 'rolling_allocation' in summary['config']:
        policy_title='Rolling 3-month allocation + weighted 1% risk + prior ATR20'
    if summary['config'].get('lock_today'):
        policy_title='5% margin batches + ATR20 + same-day exit locks'
    if summary['config'].get('net_exposure'):
        policy_title='5% net-exposure batches + ATR20 + '+('direct closes' if summary['config'].get('direct_close_control') else 'physical locks')
    if summary['config'].get('fee_avoidance'):
        policy_title='ATR20 + 1% exposure risk + fee-safe lock routing'
    fig.suptitle('R-Breaker with stops | CNY 10 million shared pool | '+policy_title,fontsize=14)
    fee_label='Historical vendor fees (today-first)' if summary['config'].get('fee_mode')=='historical' else ('Close-today 0.00023, others 0.000023' if summary['config'].get('direct_close_control') else 'Fee 0.000023')
    axes[1].set_xlabel('Trading date | '+fee_label+' + 1 adverse tick | 15% margin assumption')
    fig.savefig(root/'stopped_pnl_performance.png',dpi=170)
    plt.close(fig)
    text='# R-Breaker止损版收益表现\n\n'
    text+=f'区间：{daily[0]["day"]}至{end}，初始资金{initial:,.2f}元，期末权益{final:,.2f}元。\n\n'
    text+='|指标|数值|\n|---|---:|\n'
    for label,value in [('净PnL',f'{net_pnl:,.2f}元'),('累计收益',f'{metrics["total_return"]:.2%}'),
                        ('年化复合收益率',f'{cagr:.2%}'),('夏普（无风险0、252交易日）',f'{sharpe:.4f}'),
                        ('年化日收益波动率',f'{volatility:.2%}'),('最大日终回撤',f'{dd:.2%}'),
                        ('卡玛（日终回撤）',f'{calmar:.4f}'),('最大分钟回撤',f'{minute_dd:.2%}'),
                        ('卡玛（分钟回撤）',f'{metrics["calmar_using_minute_drawdown"]:.4f}'),
                        ('完整交易胜率',f'{metrics["trade_win_rate"]:.2%}'),('完整交易利润因子',f'{metrics["profit_factor"]:.4f}')]:
        text+=f'|{label}|{value}|\n'
    text+='\n|品种|净PnL（元）|总利润占比|对初始资金收益贡献|\n|---|---:|---:|---:|\n'
    for r in product_rows:
        text+=f'|{r["product"]}|{r["net_pnl"]:,.2f}|{r["share_of_total_pnl"]:.2%}|{r["contribution_on_initial_capital"]:.2%}|\n'
    text+='\n|年度|净PnL（元）|该年期初权益收益率|\n|---|---:|---:|\n'
    for r in annual_rows:
        text+=f'|{r["year"]}|{r["net_pnl"]:,.2f}|{r["return_on_year_start"]:.2%}|\n'
    text+='\n年化复合收益以配置初始时点2020-01-01至期末的自然日/365.25计算；每日收益首日与1000万元本金相比，后续与上个交易日权益相比。夏普为日收益算术均值/样本标准差×sqrt(252)，无风险收益率0。卡玛主口径采用相同日终权益序列的最大回撤，另列分钟回撤口径；分品种是统一资金池中的净收益归因，不是各自独立账户收益率。2026为截至期末的年内收益，不是全年收益。费用和滑点已计入，期末未平仓按结算价估值。\n'
    text+='\n保留固定15%研究保证金、分钟标签及历史交易状态/PIT未验收等既有假设。指标由保存报表只读计算，不重跑或改变策略。\n'
    if summary['config'].get('include_auction'):
        text+=f"\n本结果包含09:30独立集合竞价（实际竞价成交{summary.get('auction_fills',0)}笔，零滑点）；竞价结果后新信号进入连续交易，仍加1tick。该版本另存，没有覆盖此前排除竞价的试验。\n"
    if summary['config'].get('fee_mode')=='historical':
        text+='\n实际成交按固定供应商历史手续费、today_first平今匹配收费，佣金为零。策略风险定仓及止损预计退出成本沿用原公式；已成交费用按真实扣款进入策略可见盈亏和权益。费用版本见config.json，逐笔拆分见fee_details.csv；历史公告PIT及供应商修订限制保留。\n'
    if summary['config'].get('fee_avoidance'):
        text+=f'\n本轮保留净方向ATR20/1%风险定仓且不主动加仓；所有平仓（含解锁）先查当日计费余额，非零时以反向开仓实现敞口变化。方向交易统计来自实际成交形成的净敞口生命周期；库存清理额外成交和费用调整{summary["inventory_cleanup_adjustment"]}元，另计入账户净PnL，不混同方向交易胜率。锁仓大边保证金真实计入，超限记录不强平。\n'
    if summary['config'].get('lock_today') and not summary['config'].get('net_exposure'):
        text+='\n本版本每批为前日结算权益5%保证金，ATR20×1止损，不采用1%风险定仓。平今替换为同合约反向开仓，锁仓按多空单向大边占用，次日按信号逐批解锁或旧主力竞价双边退出；所有成交真实收万0.23费用。完整交易统计按整份锁仓容器最终双边清空计算，一个容器可能包含多次释放/退出，不等同每次方向敞口的胜率。\n'
    if summary['config'].get('net_exposure'):
        text+='\n本版本新开和解锁共享每次5%前日权益的净敞口保证金预算，品种净敞口50%，实际大边保证金90%限制新增，开盘撮合处复核。锁仓资金占用不扣除净额；不强平，任何实际超100%均判资金可行性未通过并继续记录。完整交易统计按物理容器，非逻辑敞口批次胜率。\n'
        if summary['config'].get('direct_close_control'):
            text+='\n本结果是直接平仓控制组：平今万2.3，其他成交万0.23；不建立对锁仓。\n'
    (root/'performance_report.md').write_text(text,encoding='utf-8')
    print(json.dumps(metrics,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path('outputs/rbreaker_portfolio_stop_1pct'))
    run(parser.parse_args().root)
