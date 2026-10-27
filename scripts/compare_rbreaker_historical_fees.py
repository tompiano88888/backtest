"""Report the unchanged ATR checkpoint replay with historical execution fees."""
import argparse
from datetime import date
from decimal import Decimal as D
import json
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.ticker import PercentFormatter
from scripts.summarize_rbreaker_stops import read,write


BASE=Path('outputs/rbreaker_portfolio_stop_1pct_auction')
RESULT=Path('outputs/rbreaker_portfolio_stop_1pct_auction_historical_fees')


def run(root=RESULT,baseline=BASE):
    root,baseline=Path(root),Path(baseline)
    sources=[baseline,root]
    summaries=[json.loads((p/'summary.json').read_text()) for p in sources]
    metrics=[json.loads((p/'performance_metrics.json').read_text()) for p in sources]
    daily=[read(p/'daily.csv') for p in sources]
    safe=bool(summaries[1]['config'].get('fee_avoidance'))
    baseline_label='Historical fees: direct exits' if summaries[0]['config'].get('fee_mode')=='historical' else 'Original fixed-fee checkpoint'
    current_label='Historical fees: fee-safe locks' if safe else 'Historical execution fees'
    assert [r['day'] for r in daily[0]]==[r['day'] for r in daily[1]]
    for key in ('initial_cash','start','end','margin_rate','total_margin_cap','product_margin_cap',
                'slippage_ticks','participation','minute_version','daily_version','metadata_version','include_auction','stop','allocation','reversal'):
        assert summaries[0]['config'][key]==summaries[1]['config'][key],key
    current=summaries[1];m=metrics[1]
    products=read(root/'product_daily.csv')
    by_day={}
    for r in products:by_day.setdefault(r['day'],{})[r['product']]=r['pnl']
    rows=[]
    for r,old in zip(daily[1],daily[0]):
        rows.append(dict(day=r['day'],daily_net_pnl=r['daily_pnl'],cumulative_net_pnl=r['pnl'],
                         settled_equity=r['equity'],settled_margin=r['margin'],
                         settled_fund_usage=r['usage'],settled_fund_usage_pct=D(r['usage'])*100,
                         **{p+'_cumulative_pnl':by_day[r['day']][p] for p in ('IF','IH','IC','IM')},
                         baseline_cumulative_pnl=old['pnl'],baseline_settled_fund_usage=old['usage']))
    write(root/'daily_pnl_funding.csv',rows)
    comparison=[]
    for label,s,stat,days in zip((baseline_label,current_label),summaries,metrics,daily):
        maximum=max(days,key=lambda r:D(r['usage']))
        average=sum((D(r['usage']) for r in days),D(0))/len(days)
        comparison.append(dict(variant=label,net_pnl=s['net_pnl'],cagr=stat['cagr'],sharpe=stat['sharpe'],
                               max_daily_drawdown=stat['max_daily_drawdown'],max_minute_drawdown=stat['max_minute_drawdown'],
                               calmar=stat['calmar'],fees=s['fees'],fills=s['fills'],
                               max_settled_fund_usage=maximum['usage'],max_settled_usage_day=maximum['day'],
                               mean_settled_fund_usage=average,final_settled_fund_usage=days[-1]['usage'],
                               max_minute_fund_usage=s['max_minute_margin_ratio']))
    write(root/'comparison_to_checkpoint.csv',comparison)
    x=[date.fromisoformat(r['day']) for r in daily[1]]
    fig,axes=plt.subplots(3,1,sharex=True,figsize=(14,11),layout='constrained',height_ratios=[1.2,1,1])
    axes[0].plot(x,[float(D(r['pnl'])/10000) for r in daily[0]],color='#999999',linestyle='--',linewidth=1.2,label=baseline_label)
    axes[0].plot(x,[float(D(r['pnl'])/10000) for r in daily[1]],color='#176B87',linewidth=1.5,label=current_label)
    axes[0].set_title(f'Net PnL CNY {D(m["net_pnl"]):,.0f} | CAGR {m["cagr"]:.2%} | Sharpe {m["sharpe"]:.3f} | Daily Calmar {m["calmar"]:.3f}',loc='left')
    axes[0].set_ylabel('Cumulative PnL (CNY 10,000)');axes[0].legend(loc='upper left')
    for p,color in dict(IF='#2774AE',IH='#009E73',IC='#D97706',IM='#8B5CF6').items():
        axes[1].plot(x,[float(D(by_day[r['day']][p])/10000) for r in daily[1]],color=color,linewidth=1.2,
                     label=f'{p}: CNY {D(current["product_pnl"][p]):,.0f}')
    axes[1].set_title('Historical-fee run: net PnL attribution by product',loc='left')
    axes[1].set_ylabel('Cumulative PnL (CNY 10,000)');axes[1].legend(loc='upper left',ncol=2)
    axes[2].plot(x,[float(D(r['usage'])) for r in daily[0]],color='#aaaaaa',alpha=.65,linewidth=.8,label=baseline_label)
    axes[2].plot(x,[float(D(r['usage'])) for r in daily[1]],color='#176B87',linewidth=.85,label=current_label)
    axes[2].set_title('End-of-day fund usage = actual larger-side margin / settled equity',loc='left')
    axes[2].set_ylabel('Settled fund usage');axes[2].yaxis.set_major_formatter(PercentFormatter(1))
    axes[2].legend(loc='upper left',ncol=2)
    axes[2].set_xlabel('Trading date | ATR20 + frozen 1% trade risk | No pyramiding | 15% research margin')
    for ax in axes:ax.grid(alpha=.2);ax.axhline(0,color='#777777',linewidth=.5)
    fig.suptitle('R-Breaker '+('fee-safe locks' if safe else 'checkpoint replay')+' | Historical vendor fees, today-first | CNY 10 million',fontsize=14)
    fig.savefig(root/'pnl_and_daily_funding.png',dpi=160);plt.close(fig)
    text='# R-Breaker节点：'+('避免平今费的对锁路由' if safe else '历史手续费重跑')+'\n\n'
    if safe:
        text+='保留ATR20×1、当前权益1%风险定仓、不主动增加方向批次、原六价位和隔夜规则。执行路由修改：所有平仓及解锁先查同合约同方向当日计费余额；非零时改用反向开仓，次日优先释放合规反向库存再新开。两组均使用相同历史手续费。当前主力锁仓留待信号释放，旧主力次日竞价前提交清理；所有物理持仓进入同一账本，真实占用保证金。\n\n'
        text+=f'实际平今计费手数：{current["close_today_quantity"]}。逻辑方向交易PnL {D(current["logical_trade_pnl"]):,.2f}元，库存清理额外成交/费用调整 {D(current["inventory_cleanup_adjustment"]):,.2f}元，合计与真实账户PnL一致。资金观测验收：{current["funding_feasibility"]["status"]}，实际开盘/逐成交/分钟末/结算观测峰值{D(current["funding_feasibility"]["max_ratio"]):.2%}，超100%观测{current["funding_feasibility"]["breaches"]}次；被动超限保留，不强平。\n\n'
    else:
        text+='保持ATR20×1、当前权益1%风险定仓、不加仓、主力选择、信号、隔夜与结算规则；只将实际成交费用切换为固定供应商历史版本、today_first平今匹配，佣金零。策略定仓及止损预计成本公式保持原万0.23估算。实际费用进入权益及交易已扣费用，因而会影响以后仓位和退出路径。\n\n'
    text+='|指标|'+('历史手续费直接平仓' if safe else '原固定费率节点')+'|'+('历史手续费对锁' if safe else '历史手续费重跑')+'|\n|---|---:|---:|\n'
    formats=[('net_pnl','净PnL（元）',lambda v:f'{D(str(v)):,.2f}'),('cagr','年化收益率',lambda v:f'{float(v):.2%}'),
             ('sharpe','夏普',lambda v:f'{float(v):.4f}'),('max_daily_drawdown','日终回撤',lambda v:f'{float(v):.2%}'),
             ('max_minute_drawdown','分钟回撤',lambda v:f'{float(v):.2%}'),('calmar','日终卡玛',lambda v:f'{float(v):.4f}'),
             ('fees','手续费（元）',lambda v:f'{D(str(v)):,.2f}'),('fills','成交笔数',str),
             ('max_settled_fund_usage','日终资金使用率峰值',lambda v:f'{D(str(v)):.2%}'),
             ('mean_settled_fund_usage','日终资金使用率均值',lambda v:f'{D(str(v)):.2%}'),
             ('final_settled_fund_usage','期末日终资金使用率',lambda v:f'{D(str(v)):.2%}'),
             ('max_minute_fund_usage','分钟观察使用率峰值',lambda v:f'{D(str(v)):.2%}')]
    for key,label,fmt in formats:text+=f'|{label}|{fmt(comparison[0][key])}|{fmt(comparison[1][key])}|\n'
    text+='\n|品种|本轮净PnL（元）|对照净PnL（元）|\n|---|---:|---:|\n'
    for p in ('IF','IH','IC','IM'):text+=f'|{p}|{D(current["product_pnl"][p]):,.2f}|{D(summaries[0]["product_pnl"][p]):,.2f}|\n'
    text+='\n![PnL与日终资金使用率](pnl_and_daily_funding.png)\n\n'
    text+='daily_pnl_funding.csv逐日列出净PnL、累计PnL、结算权益、结算保证金、资金使用率（小数与百分数）及品种归因。使用率按真实持仓多空单向大边计算；结算后分母为权益，不把保证金再次扣出权益。\n\n'
    text+=f'费用版本：`{current["config"]["historical_fees"]["version"]}`；知识截止：`{current["config"]["historical_fees"]["as_of"]}`。按成交交易日查实际合约费用，不回退固定费率。历史供应商记录并不等于逐公告审定或当年可知；15%保证金仍为研究假设。\n\n'
    text+='原节点和封存保留，未自动以本结果替换节点。独立成交/原始费率/平今余额/FIFO/逐日结算及保证金核对见verification.json。\n'
    (root/'historical_fee_report.md').write_text(text,encoding='utf-8')
    if safe:
        (root/'fee_avoidance_report.md').write_text(text,encoding='utf-8')
        (root/'report.md').write_text(text,encoding='utf-8')
    print(json.dumps(comparison,default=str,indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,default=RESULT);p.add_argument('--baseline',type=Path,default=BASE)
    a=p.parse_args();run(a.root,a.baseline)
