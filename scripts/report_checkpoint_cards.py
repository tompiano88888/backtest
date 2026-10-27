"""Read saved node replays; export consistent PnL / annual-performance figures."""
import argparse
import csv
from datetime import date
from decimal import Decimal as D
import hashlib
import json
import math
import os
from pathlib import Path
from statistics import mean, stdev


def read(path):
    with Path(path).open(encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


from backtest.performance import statistics, plot


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--arbitrage',type=Path,required=True)
    p.add_argument('--rbreaker',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    if (args.output/'metrics.json').exists():
        raise FileExistsError('Choose a new output directory; completed reports are preserved')
    os.environ['MPLCONFIGDIR']=str(args.output/'matplotlib')
    all_metrics={}
    for key,root in [('arbitrage',args.arbitrage),('rbreaker',args.rbreaker)]:
        rows=read(root/'daily.csv')
        m=statistics(rows,'10000000',date(2020,1,1))
        if key=='arbitrage':
            summary=read(root/'summary.csv')[0]
            assert D(m['pnl'])==D(summary['total_pnl'])
            m['original_node_cagr']=summary['cagr']
            title='期现套利';subtitle='5—8月分红调整  /  35组滚动参数  /  四品种统一资金池'
            extra=f'原节点CAGR {float(summary["cagr"]):.4%}；本图统一采用自然日差/365.25'
            caveat='研究边界：全样本选参、原日频回看执行假设；完整费用/税/PIT限制保留，不等同统一分钟框架验收。'
            accent='#087f8c'
        else:
            summary=json.loads((root/'summary.json').read_text())
            assert D(m['pnl'])==D(summary['net_pnl'])
            m['minute_max_drawdown']=summary['max_drawdown']
            title='R-Breaker';subtitle='历史手续费  /  ATR20 × 1  /  当前权益1%风险定仓  /  避免平今费对锁'
            extra=f'分钟最大回撤  {float(summary["max_drawdown"]):.2%}  ·  主指标采用日终口径'
            caveat='研究边界：15%保证金为研究假设；费用与滑点已入账，期末持仓按结算价估值；非Tick，历史PIT限制保留。'
            accent='#3d62a8'
        m['source']=str(root.resolve())
        m['source_daily_sha256']=hashlib.sha256((root/'daily.csv').read_bytes()).hexdigest()
        plot(m,title,subtitle,accent,extra,caveat,args.output/f'{key}_performance.png')
        with (args.output/f'{key}_annual.csv').open('w',encoding='utf-8-sig',newline='') as f:
            w=csv.DictWriter(f,fieldnames=list(m['annual'][0]));w.writeheader();w.writerows(m['annual'])
        all_metrics[key]=m
    (args.output/'metrics.json').write_text(json.dumps(all_metrics,indent=2,ensure_ascii=False),encoding='utf-8')
    print(json.dumps({k:{f:v for f,v in m.items() if f not in ('series','annual')} for k,m in all_metrics.items()},indent=2))


if __name__=='__main__':main()
