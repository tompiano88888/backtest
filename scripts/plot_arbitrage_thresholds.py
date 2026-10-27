"""Plot saved dated research selections; never rerun selection or execution."""
import argparse
import csv
import hashlib
import json
import math
from collections import defaultdict
from datetime import date
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

from backtest.factor_data import load_research_data
from backtest.front_contract_arbitrage import front_map
from backtest.factor_research import csv_if, dump
from backtest.models import D

PRODUCTS=('IF','IH','IC','IM')


def read(path):
    with path.open(encoding='utf-8-sig',newline='') as stream:
        return list(csv.DictReader(stream))


def draw(ax, data, raw=False, compact=False):
    dates=[r['day'] for r in data]
    prefix='raw_' if raw else ''
    for field,label,color,style,width in (
        ('entry_threshold','滚动开仓阈值','#669dcc','-',1),
        ('effective_entry','实际开仓边界','#1767ad','-',1.8),
        ('exit_threshold','滚动平仓阈值','#d76b27','-',1.6),
        ('entry_floor','额外开仓下限','#777777','--',1)):
        values=[None if r[prefix+field] is None else float(r[prefix+field]*100) for r in data]
        ax.step(dates,values,where='post',label=label,color=color,linestyle=style,linewidth=width)
    ax.axhline(0,color='#aaaaaa',linewidth=.6)
    ax.grid(alpha=.2)
    ax.set_ylabel('年化阈值（%）')
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%m-%d' if compact else '%Y-%m'))
    if compact:
        ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=2,maxticks=4))
    else:
        ax.xaxis.set_major_locator(mdates.YearLocator())


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',default='outputs/arbitrage_dividend_season_optimized_floors')
    parser.add_argument('--output',default='outputs/arbitrage_dividend_season_thresholds')
    args=parser.parse_args();source=Path(args.source);out=Path(args.output)
    out.mkdir(parents=True,exist_ok=True)
    config=json.loads((source/'config.json').read_text(encoding='utf-8'))
    if not config.get('dividend_season_adjustment'):
        raise ValueError('This report requires the dividend-season signal source')
    selections=read(source/'selections.csv')
    signals={(r['product'],r['contract'],r['day']):r for r in read(source/'dividend_season_signals.csv')}
    snapshots=config['source_snapshots']
    _,_,raw,facts,instruments=load_research_data('data/index_futures',snapshots['daily_version'],snapshots['metadata_version'])
    calendars={p:sorted({r['trading_date'] for r in raw if r['product']==p}) for p in PRODUCTS}
    mapping=front_map(instruments,{c:date.fromisoformat(f['first_trade_date']) for c,f in facts.items()},calendars)
    result=[];seen=set();contracts=defaultdict(list)
    for r in selections:
        p=r['product'];day=date.fromisoformat(r['decision_day']);code=mapping[p,day]
        if (p,day) in seen or not code:raise ValueError('Duplicate selection or missing front contract')
        seen.add((p,day))
        signal=signals[p,code,r['decision_day']]
        enter,exit_=D(r['entry_threshold']),D(r['exit_threshold'])
        floor=D(config['min_expected_annual_return_by_product'][p]);effective=max(enter,floor)
        shift=D(signal['annualized_dividend_adjustment']) if signal['annualized_dividend_adjustment'] else None
        row=dict(product=p,contract=code,day=day,expiry=instruments[code].expiry,
                 entry_threshold=enter,exit_threshold=exit_,entry_floor=floor,effective_entry=effective,
                 dividend_adjustment=shift,signal_available=bool(signal['adjusted_annualized_rate']),
                 raw_entry_threshold=enter-shift if shift is not None else None,
                 raw_exit_threshold=exit_-shift if shift is not None else None,
                 raw_entry_floor=floor-shift if shift is not None else None,
                 raw_effective_entry=effective-shift if shift is not None else None,
                 window_start=r['window_start'],window_end=r['window_end'])
        result.append(row);contracts[code].append(row)
    result.sort(key=lambda r:(r['product'],r['day']))
    csv_if(out/'daily_thresholds.csv',result)
    for r in read(source/'candidate_plans.csv'):
        code=r['contract'];day=date.fromisoformat(r['signal_day'])
        chosen=next(x for x in contracts[code] if x['day']==day)
        assert D(r['entry_threshold'])==chosen['entry_threshold']
        assert D(r['exit_threshold'])==chosen['exit_threshold']
    plt.rcParams.update({'font.sans-serif':['Microsoft YaHei','DejaVu Sans'],'axes.unicode_minus':False,'font.size':10})
    for raw_mode,name,title in (
        (False,'thresholds_overview.png','分红调整版：逐日滚动开平仓阈值'),
        (True,'raw_basis_thresholds.png','同一策略：换算到未调整原始基差的阈值')):
        fig,axes=plt.subplots(4,1,figsize=(16,11),sharex=True,layout='constrained')
        for ax,p in zip(axes,PRODUCTS):
            data=[r for r in result if r['product']==p]
            draw(ax,data,raw=raw_mode);ax.set_title(f"{p} · 额外下限 {D(config['min_expected_annual_return_by_product'][p]):.1%}",loc='left')
            if p=='IM':ax.text(.01,.05,'满一年研究历史后才有滚动选择',transform=ax.transAxes,fontsize=9)
        axes[0].legend(ncol=4,loc='upper right');axes[-1].set_xlabel('信号日期（收盘后决策，最早下一交易日开盘执行）')
        fig.suptitle(title+'\n2020-01-01—2026-10-08 · 1000万元统一资金池',fontsize=15)
        fig.savefig(out/name,dpi=160);plt.close(fig)
    sheets=defaultdict(list)
    summary=[]
    for code,data in sorted(contracts.items()):
        data.sort(key=lambda r:r['day'])
        p=data[0]['product'];year=data[0]['expiry'].year
        sheets[p,year].append((code,data))
        summary.append(dict(product=p,contract=code,start=data[0]['day'],end=data[-1]['day'],
                            expiry=data[0]['expiry'],selection_days=len(data),
                            threshold_pairs=len({(r['entry_threshold'],r['exit_threshold']) for r in data})))
    csv_if(out/'contracts.csv',summary)
    links=[]
    for (p,year),group in sorted(sheets.items()):
        fig,axes=plt.subplots(math.ceil(len(group)/3),3,figsize=(18,3.1*math.ceil(len(group)/3)),squeeze=False,layout='constrained')
        for ax,(code,data) in zip(axes.flat,group):
            draw(ax,data,compact=True)
            ax.set_title(f'{code} · {len(data)}个选择日',loc='left')
        for ax in list(axes.flat)[len(group):]:ax.set_visible(False)
        handles,labels=axes.flat[0].get_legend_handles_labels()
        fig.get_layout_engine().set(rect=(0,0,1,.90))
        fig.legend(handles,labels,loc='upper center',bbox_to_anchor=(.5,.965),ncol=4)
        fig.suptitle(f'{p} {year} · 各实际合约当月期间的逐日寻参阈值',fontsize=15,y=.995)
        name=f'{p}_{year}_contracts.png';fig.savefig(out/name,dpi=135);plt.close(fig)
        links.append(f'- [{p} {year}：{len(group)}个合约]({name})')
    lines=['# 分红调整重寻参版：逐日开平仓阈值','',
        f'来源：{source.as_posix()}，共{len(result)}条逐日选择、{len(contracts)}个实际合约。',
        '图中蓝细线为35组滚动寻参选中的开仓阈值，蓝粗线为其与额外下限取较大者；橙线为滚动平仓阈值，灰虚线为固定额外下限。数值均为年化百分比。',
        '精确入场条件为调整后信号 > 滚动开仓阈值，且 ≥ 额外下限；图中的实际边界不改变这两个严格/非严格比较。',
        '第一张图阈值作用于分红调整后信号。第二张图减去当日预测分红年化贡献，换算为原始基差口径；缺预测/到期不填值，线条留空。',
        '选择按品种每日进行，仅交易当月合约；合约分图展示各合约作为当月合约期间，不为远月、无选择日或IM研究历史不足期间虚构参数。',
        '**这里展示当日新决策选择。已开组合冻结入场时的退出阈值，不会每天随橙线重设；同一合约不同组合可能有不同冻结阈值。**','',
        '![逐日寻参阈值](thresholds_overview.png)','',
        '![原始基差口径](raw_basis_thresholds.png)','',
        '## 每个实际合约（按品种和年份分图）','',*links,'',
        '[完整逐日数据](daily_thresholds.csv) · [合约日期索引](contracts.csv)',
        '未重新寻参或执行回测，原结果保持；源文件校验和及候选阈值一致性核对见manifest.json。']
    (out/'report.md').write_text('\n'.join(lines),encoding='utf-8')
    dump(out/'manifest.json',dict(source=source.as_posix(),selections=len(result),contracts=len(contracts),
         contract_year_charts=len(sheets),candidate_thresholds_checked=True,
         source_sha256={n:hashlib.sha256((source/n).read_bytes()).hexdigest() for n in ('config.json','selections.csv','dividend_season_signals.csv')}))
    print(json.dumps(dict(rows=len(result),contracts=len(contracts),charts=len(sheets)+2,report=str(out/'report.md'))))


if __name__=='__main__':main()
