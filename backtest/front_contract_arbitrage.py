"""One front-contract hedge per product, user-specific fixed entry/exit thresholds."""
from collections import defaultdict
from datetime import date
from pathlib import Path
import hashlib
import json

from .all_contract_cash_arbitrage import analyze_contract,plot
from .arbitrage_capital_returns import capacity_rows,calculate
from .cash_futures_basis import load_spot
from .factor_data import load_research_data
from .factor_research import csv_if,dump
from .models import D

PRODUCTS=('IF','IH','IC','IM')
THRESHOLDS={'IF':(D('.04'),D('-.02')),'IH':(D('.04'),D('-.02')),
            'IC':(D('.02'),D('-.04')),'IM':(D('.02'),D('-.04'))}


def front_map(instruments,first_dates,calendars):
    result={}
    for product in PRODUCTS:
        choices=[code for code,i in instruments.items() if i.product==product]
        for day in calendars.get(product,[]):
            active=[code for code in choices if first_dates[code]<=day<=instruments[code].expiry]
            result[product,day]=min(active,key=lambda c:(instruments[c].expiry,c)) if active else None
    return result


def run(output='outputs/front_contract_cash_arbitrage',skip_charts=False):
    out=Path(output);out.mkdir(parents=True,exist_ok=True)
    parent=json.loads(Path('outputs/all_contract_cash_arbitrage/config.json').read_text(encoding='utf-8'))
    dv,mv,raw,facts,instruments=load_research_data('data/index_futures',parent['daily_version'],parent['metadata_version'])
    sv,spot_rows=load_spot(version=parent['spot_version'])
    prices={(r['instrument'],r['trading_date']):r for r in raw}
    spot={(r['instrument'],r['trading_date']):r for r in spot_rows}
    calendar=defaultdict(set)
    for r in raw:calendar[r['product']].add(r['trading_date'])
    calendar={p:sorted(ds) for p,ds in calendar.items()}
    first_dates={code:date.fromisoformat(f['first_trade_date']) for code,f in facts.items()}
    mapping=front_map(instruments,first_dates,calendar)
    summaries=[];cycles=[];daily=[];signals=[];issues=[]
    for code,inst in sorted(instruments.items()):
        if not any(mapping[inst.product,d]==code for d in calendar[inst.product]):continue
        enter,exit_=THRESHOLDS[inst.product]
        result=analyze_contract(code,inst,prices,spot,calendar[inst.product],first_dates[code],enter,exit_,
                                lambda d,c=code,p=inst.product:mapping.get((p,d))==c)
        summaries.append(result[0]);cycles.extend(result[1]);daily.extend(result[2]);signals.extend(result[3]);issues.extend(result[4])
    csv_if(out/'contracts.csv',summaries);csv_if(out/'cycles.csv',cycles);csv_if(out/'contract_daily.csv',daily)
    csv_if(out/'signals.csv',signals);csv_if(out/'issues.csv',issues)
    csv_if(out/'front_mapping.csv',[dict(product=p,day=d,front=c) for (p,d),c in sorted(mapping.items())])
    aggregates=[];totals=[]
    for product in PRODUCTS:
        bycode=defaultdict(list)
        for r in daily:
            if r['product']==product:bycode[r['contract']].append(r)
        changes=defaultdict(lambda:[D(0),D(0)])
        held_counts=defaultdict(int)
        for code,rows in bycode.items():
            last_g=last_p=D(0)
            for r in rows:
                if r['future_tick_pnl'] is None:raise ValueError('Incomplete held valuation')
                changes[r['day']][0]+=r['gross_pnl']-last_g;changes[r['day']][1]+=r['future_tick_pnl']-last_p
                last_g,last_p=r['gross_pnl'],r['future_tick_pnl']
                held_counts[r['day']]+=int(r['holding'])
        if max(held_counts.values(),default=0)>1:raise AssertionError('More than one simultaneous product hedge')
        g=n=D(0)
        for day in calendar[product]:
            g+=changes[day][0];n+=changes[day][1]
            aggregates.append(dict(product=product,day=day,gross_pnl=g,future_tick_pnl=n))
        trades=[r for r in cycles if r['product']==product]
        totals.append(dict(product=product,entry_threshold=THRESHOLDS[product][0],exit_threshold=THRESHOLDS[product][1],
                           entries=len(trades),gross_pnl=g,future_tick_pnl=n,maximum_simultaneous_hedges=max(held_counts.values(),default=0)))
    csv_if(out/'aggregate_daily.csv',aggregates);csv_if(out/'product_summary.csv',totals)
    capital=capacity_rows(cycles,aggregates);returns,yearly=calculate(capital)
    csv_if(out/'capital_daily.csv',capital);csv_if(out/'returns.csv',returns);csv_if(out/'yearly_returns.csv',yearly)
    for product in PRODUCTS:
        group=[r for r in cycles if r['product']==product]
        largest=max((r['entry_index']*r['multiplier']*D('1.5') for r in group),default=D(0))
        actual=next(r['fixed_initial_capital'] for r in returns if r['product']==product)
        assert actual==largest  # one-combination reserve, never sum all monthly contract reserves
    dump(out/'config.json',dict(daily_version=dv,metadata_version=mv,spot_version=sv,thresholds=THRESHOLDS,
         max_hedges_per_product=1,scope='nearest listed not-yet-expired actual contract; expiry day retains old front',
         timing='completed close -> next observed day open; no automatic roll or reentry on exit date',
         capital='1.5*entry index open*multiplier; maximum single executed combination per product',
         returns='fixed initial reserve equal full-history capital peak; calendar CAGR; annual PnL / opening equity',
         costs='gross and future one-tick only; complete fees/ETF costs/dividends/financing excluded',
         source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()))
    if not skip_charts:plot(out,daily,aggregates,cycles,title='Front only, one hedge: IF/IH +4%/-2%; IC/IM +2%/-4%')
    lines=['# 仅当月、一份组合：分品种阈值','',
           'IF/IH严格>4%入场、<−2%退出；IC/IM严格>2%入场、<−4%退出，否则到期现金结清。收盘信号最早次日开盘，不自动展期，不沿用此前止损/均值规则。',
           '当月定义为已开始交易且未到期的最近实际月份，交割日仍保留到期合约。入场信号日和执行日必须均为当月，每品种最多一份一手期货＋乘数单位理想指数对冲。所有日期的并行持仓数已检查≤1。',
           '只给每品种预留一份组合资金：每个实际入场周期资金=入场指数开盘×乘数×1.5，品种固定初始本金取其全历史峰值，不给所有实际月份同时配置本金。峰值是事后统计，非当时已知资金计划。',
           '原理想指数PnL口径保留，主收益扣期货1tick不利滑点，不含完整手续费/现货成本/融资/股息，不代表实际ETF总回报。到期期货最终结算与指数收盘取样不同，不强行设置到期价差为零。', '',
           '|品种|开仓次数|固定单份资金峰值（万元）|日均占用（万元）|扣期货tick后PnL（万元）|全期收益率|CAGR|',
           '|---|---:|---:|---:|---:|---:|---:|']
    for r in returns:
        n=next(t['entries'] for t in totals if t['product']==r['product'])
        lines.append(f"|{r['product']}|{n}|{r['fixed_initial_capital']/10000:.2f}|{r['mean_occupied_capital']/10000:.2f}|{r['future_tick_pnl']/10000:.2f}|{r['total_return_future_tick_pnl']:.2%}|{r['cagr_future_tick_pnl']:.2%}|")
    lines+=['','## 各年实际收益率','','|年份|IF|IH|IC|IM|','|---|---:|---:|---:|---:|']
    for year in sorted({r['year'] for r in yearly}):
        values=[]
        for product in PRODUCTS:
            r=next((r for r in yearly if r['product']==product and r['year']==year),None)
            values.append(f"{r['actual_return_future_tick_pnl']:.2%}" if r else '—')
        lines.append('|'+str(year)+'|'+'|'.join(values)+'|')
    lines+=['','年度实际收益率=当年PnL/年初权益，收益留存；CAGR按自然日计算。首年/2026为部分年度，2026截至行情末日。',
            '逐合约/周期/信号/当月映射/资金/收益均导出，PnL主图及逐有入场合约图标记全部开仓。历史公告PIT、独立日历和可成交性仍待核实。']
    (out/'report.md').write_text('\n'.join(lines),encoding='utf-8');print(returns,flush=True)
    return returns,yearly
