"""Plot saved arbitrage Engine PnL only; does not rerun trading."""
import csv
from datetime import date
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.ticker import PercentFormatter

from backtest.models import D


def main():
    root=Path('outputs/arbitrage_node_engine_verified_20261022')
    summary=json.loads((root/'summary.json').read_text('utf-8'))
    with (root/'daily.csv').open(encoding='utf-8-sig',newline='') as stream:
        rows=list(csv.DictReader(stream))
    days=[date.fromisoformat(r['day']) for r in rows]
    equity=[D(r['equity']) for r in rows]
    pnl=[float(D(r['pnl'])/10000) for r in rows]
    peak=D('10000000');drawdown=[]
    for value in equity:
        peak=max(peak,value)
        drawdown.append(float(value/peak-1))
    assert abs(min(drawdown)+float(summary['max_drawdown']))<1e-12
    components={**summary['product_pnl'],'Idle cash':summary['idle_cash']}
    assert sum((D(v) for v in components.values()),D(0))==D(summary['net_pnl'])
    plt.rcParams.update({'font.family':'DejaVu Sans','font.size':11})
    fig=plt.figure(figsize=(13,10),layout='constrained')
    gs=fig.add_gridspec(3,1,height_ratios=[2.1,.75,1.25])
    ax=fig.add_subplot(gs[0]);dd=fig.add_subplot(gs[1],sharex=ax);bar=fig.add_subplot(gs[2])
    ax.plot(days,pnl,color='#087f6c',linewidth=2)
    ax.fill_between(days,pnl,0,color='#087f6c',alpha=.08)
    ax.scatter(days[-1],pnl[-1],color='#087f6c',s=30,zorder=3)
    ax.annotate(f'{pnl[-1]:,.2f}',xy=(days[-1],pnl[-1]),xytext=(-8,-22),
                textcoords='offset points',ha='right',fontweight='bold',color='#087f6c')
    ax.set_title('Cumulative net PnL',loc='left',fontweight='bold');ax.set_ylabel('CNY 10,000')
    ax.tick_params(labelbottom=False)
    dd.fill_between(days,drawdown,0,color='#bc4b51',alpha=.7)
    dd.yaxis.set_major_formatter(PercentFormatter(1));dd.set_ylabel('Drawdown')
    labels=list(components);values=[float(D(v)/10000) for v in components.values()]
    bars=bar.barh(labels,values,color=['#2675a7','#087f6c','#ca8629','#7f5bb3','#90979b'],height=.6)
    bar.invert_yaxis();bar.set_xlim(0,max(values)*1.17)
    bar.bar_label(bars,labels=[f'{v:,.2f}' for v in values],padding=6)
    bar.set_xlabel('Net PnL contribution (CNY 10,000)')
    bar.set_title('Contribution by product + idle cash income',loc='left',fontweight='bold')
    for axis in [ax,dd,bar]:
        axis.grid(alpha=.18,axis='x' if axis is bar else 'both');axis.set_axisbelow(True)
        axis.spines[['top','right']].set_visible(False)
    fig.suptitle('Cash / futures arbitrage | Unified daily Engine\n'
                 f'2020-01-01 to 2026-10-08  |  Net PnL CNY {D(summary["net_pnl"]):,.2f}\n'
                 f'CAGR {summary["cagr"]:.2%}  |  Sharpe {summary["sharpe"]:.4f}  |  Daily max drawdown {float(summary["max_drawdown"]):.2%}',
                 fontsize=14,fontweight='bold')
    fig.supxlabel('Product contributions include actual dividends; do not add dividends again. 2026 is partial.',fontsize=9,color='#555555')
    for ext in ['png','svg']:
        fig.savefig(root/f'pnl_only.{ext}',dpi=170)
    plt.close(fig)
    print(root/'pnl_only.png')


if __name__=='__main__':
    main()
