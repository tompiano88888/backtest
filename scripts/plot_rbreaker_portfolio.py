"""Render shared-pool product attribution and total net PnL."""
import argparse
from collections import defaultdict
import csv
from datetime import date
from decimal import Decimal
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter, PercentFormatter


def run(root=Path('outputs/rbreaker_portfolio')):
    root=Path(root)
    with (root/'daily.csv').open(encoding='utf-8-sig',newline='') as handle:
        daily=list(csv.DictReader(handle))
    curves=defaultdict(list)
    with (root/'product_daily.csv').open(encoding='utf-8-sig',newline='') as handle:
        for row in csv.DictReader(handle):
            curves[row['product']].append((date.fromisoformat(row['day']),Decimal(row['pnl'])))
    summary=json.loads((root/'summary.json').read_text(encoding='utf-8'))
    for index,row in enumerate(daily):
        assert sum((points[index][1] for points in curves.values()),Decimal(0))==Decimal(row['pnl'])
    assert Decimal(daily[-1]['pnl'])==Decimal(summary['net_pnl'])
    x=[date.fromisoformat(row['day']) for row in daily]
    fig,(ax,risk)=plt.subplots(2,1,figsize=(13,9),sharex=True,height_ratios=[3,1],layout='constrained')
    colors=dict(IF='#2774AE',IH='#009E73',IC='#D97706',IM='#8B5CF6')
    ax.plot(x,[float(row['pnl']) for row in daily],color='#202020',linewidth=1.9,label=f"Total ({Decimal(summary['net_pnl']):,.0f})")
    for product,points in curves.items():
        a,b=zip(*points)
        ax.plot(a,[float(v) for v in b],label=f'{product} ({b[-1]:,.0f})',linewidth=1.1,color=colors[product])
    ax.axhline(0,color='#777777',linewidth=.6)
    ax.set_title('R-Breaker | CNY 10 million shared pool | Net cumulative PnL',loc='left')
    ax.set_ylabel('Cumulative PnL (CNY)')
    ax.yaxis.set_major_formatter(FuncFormatter(lambda value,_:f'{value:,.0f}'))
    ax.legend(loc='upper left',title='Final net PnL (CNY)')
    ax.grid(alpha=.2)
    risk.plot(x,[float(row['usage']) if row['usage'] else float('nan') for row in daily],color='#2774AE',linewidth=.8,label='Daily settled margin / equity')
    risk.axhline(.9,color='#C0392B',linestyle='--',linewidth=.8,label='90% entry budget')
    risk.yaxis.set_major_formatter(PercentFormatter(1))
    risk.set_ylabel('Margin usage')
    risk.set_xlabel('Trading date | Fee 0.000023 per fill + 1 adverse tick | 15% margin assumption')
    risk.grid(alpha=.2)
    risk.legend(loc='upper left',fontsize=8)
    fig.savefig(root/'portfolio_pnl.png',dpi=170)
    plt.close(fig)
    print(root.resolve()/'portfolio_pnl.png')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path('outputs/rbreaker_portfolio'))
    run(parser.parse_args().root)
