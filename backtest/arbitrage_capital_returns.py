"""Capacity-based return attribution for independent ideal index hedge cycles."""
from collections import defaultdict
from datetime import date
from pathlib import Path
import csv
import hashlib
import json

from .factor_research import csv_if,dump
from .models import D

PRODUCTS=('IF','IH','IC','IM')


def annualized(initial,final,days):
    if initial<=0 or final<=0 or days<=0:return None
    return ((final/initial).ln()*D(365)/D(days)).exp()-1


def capacity_rows(cycles,daily,multiple=D('1.5')):
    events=defaultdict(lambda:dict(entry=D(0),open_exit=D(0),delivery=D(0),new=0))
    for r in cycles:
        value=D(r['entry_index'])*D(r['multiplier'])*multiple
        key=(r['product'],date.fromisoformat(str(r['entry_day'])))
        events[key]['entry']+=value;events[key]['new']+=1
        if r['exit_day']:
            key=(r['product'],date.fromisoformat(str(r['exit_day'])))
            events[key]['delivery' if r['exit_reason']=='delivery' else 'open_exit']+=value
    result=[]
    for product in PRODUCTS:
        held=D(0);previous={m:D(0) for m in ('gross_pnl','future_tick_pnl')}
        group=sorted((r for r in daily if r['product']==product),key=lambda r:r['day'])
        for r in group:
            day=date.fromisoformat(str(r['day']));e=events[product,day]
            before=held
            after_open=before-e['open_exit']+e['entry']
            if after_open<0:raise ValueError('Invalid exit capital schedule')
            peak=max(before,after_open)
            held=after_open-e['delivery']
            if held<0:raise ValueError('Invalid delivery capital schedule')
            row=dict(product=product,day=day,capital_before_open=before,capital_after_open=after_open,
                     capital_daily_peak=peak,capital_end_day=held,entries=e['new'])
            for metric in previous:
                current=D(r[metric]);row[f'daily_{metric}']=current-previous[metric]
                row[f'cumulative_{metric}']=current;previous[metric]=current
            result.append(row)
    return result


def calculate(rows):
    summaries=[];yearly=[]
    for product in PRODUCTS:
        group=[r for r in rows if r['product']==product]
        if not group:continue
        start,end=group[0]['day'],group[-1]['day']
        capital=max(r['capital_daily_peak'] for r in group)
        if capital<=0:raise ValueError('No occupied capital')
        summary=dict(product=product,start=start,end=end,trading_days=len(group),calendar_days=(end-start).days+1,
                     fixed_initial_capital=capital,mean_occupied_capital=sum((r['capital_daily_peak'] for r in group),D(0))/len(group))
        for metric in ('gross_pnl','future_tick_pnl'):
            total=group[-1][f'cumulative_{metric}'];summary[metric]=total
            summary[f'total_return_{metric}']=total/capital
            summary[f'cagr_{metric}']=annualized(capital,capital+total,summary['calendar_days'])
            occupied=sum((r['capital_daily_peak'] for r in group),D(0))
            summary[f'capital_efficiency_annualized_{metric}']=total*D(252)/occupied if occupied else None
        summaries.append(summary)
        accumulated={m:D(0) for m in ('gross_pnl','future_tick_pnl')}
        for year in sorted({r['day'].year for r in group}):
            subset=[r for r in group if r['day'].year==year]
            peak=max(r['capital_daily_peak'] for r in subset)
            occupied=sum((r['capital_daily_peak'] for r in subset),D(0))
            item=dict(product=product,year=year,start=subset[0]['day'],end=subset[-1]['day'],
                      partial_year=(year==start.year and (start.month,start.day)>(1,10)) or (year==end.year and end.month<12),
                      trading_days=len(subset),annual_peak_capital=peak,mean_occupied_capital=occupied/len(subset),
                      entries=sum(r['entries'] for r in subset))
            for metric in accumulated:
                pnl=sum((r[f'daily_{metric}'] for r in subset),D(0));opening=capital+accumulated[metric]
                item[f'pnl_{metric}']=pnl;item[f'opening_equity_{metric}']=opening
                item[f'actual_return_{metric}']=pnl/opening if opening>0 else None
                item[f'return_on_annual_peak_{metric}']=pnl/peak if peak>0 else None
                item[f'capital_efficiency_annualized_{metric}']=pnl*D(252)/occupied if occupied>0 else None
                accumulated[metric]+=pnl
            yearly.append(item)
        for metric in accumulated:assert accumulated[metric]==group[-1][f'cumulative_{metric}']
    return summaries,yearly


def run(source='outputs/all_contract_cash_arbitrage',output='outputs/all_contract_cash_arbitrage_capital'):
    source=Path(source);out=Path(output);out.mkdir(parents=True,exist_ok=True)
    with (source/'cycles.csv').open(encoding='utf-8-sig') as f:cycles=list(csv.DictReader(f))
    with (source/'aggregate_daily.csv').open(encoding='utf-8-sig') as f:daily=list(csv.DictReader(f))
    rows=capacity_rows(cycles,daily);summaries,yearly=calculate(rows)
    csv_if(out/'capital_daily.csv',rows);csv_if(out/'returns.csv',summaries);csv_if(out/'yearly_returns.csv',yearly)
    dump(out/'config.json',dict(source_config=json.loads((source/'config.json').read_text(encoding='utf-8')),
        capital='1.5 * entry index open * multiplier per cycle; fixed through holding; overlap summed',
        daily_peak='max(capital before open exits, after open exits and entries); delivery releases after close',
        initial_capital='retrospective full-interval peak by product; no withdrawals or position changes',
        annual_return='calendar-day CAGR on fixed capital equity',year_return='annual PnL / beginning-of-year equity',
        efficiency='PnL *252 / sum trading-day peak occupied capital; not account return',
        hashes={n:hashlib.sha256((source/n).read_bytes()).hexdigest() for n in ('cycles.csv','aggregate_daily.csv')},
        source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()))
    lines=['# 理想期现研究：1.5倍现货金额资金口径','',
           '一份ETF解释为每手期货对应理想现货名义金额，不是100份ETF：入场指数开盘×期货乘数。每周期资金占用=该金额×1.5，持有期固定，多合约重叠累加。开盘先退出再入场，峰值比较开盘前/后；交割日资金收盘后释放。',
           '主口径：每品种全区间峰值占用作为固定初始本金，收益留在账户。年化为按自然日计算的CAGR，各年实际收益率=年度PnL/年初权益；首尾不完整年度不强行年化。峰值来自事后全历史，属于容量统计，不是当时可知的资金计划。',
           '另列年度峰值/日均占用及资金效率：年度PnL×252/累计交易日峰值占用。它反映使用效率，不是固定资金账户收益，不能与CAGR混用。',
           '沿用独立合约毛PnL和仅扣期货1tick的PnL，未计完整手续费、现货成本、融资及股息。指数不是真实可交易ETF总回报。1.5倍是用户资金估计，并非已核实保证金规则。','',
           '|品种|固定资金／全期峰值（万元）|平均占用（万元）|扣期货tick后PnL（万元）|全期收益率|年化CAGR|',
           '|---|---:|---:|---:|---:|---:|']
    for r in summaries:
        lines.append(f"|{r['product']}|{r['fixed_initial_capital']/10000:.2f}|{r['mean_occupied_capital']/10000:.2f}|{r['future_tick_pnl']/10000:.2f}|{r['total_return_future_tick_pnl']:.2%}|{r['cagr_future_tick_pnl']:.2%}|")
    lines+=['','## 各年实际收益率（扣期货tick后）','','|年份|IF|IH|IC|IM|','|---|---:|---:|---:|---:|']
    for year in sorted({r['year'] for r in yearly}):
        values=[]
        for product in PRODUCTS:
            r=next((r for r in yearly if r['year']==year and r['product']==product),None)
            values.append(f"{r['actual_return_future_tick_pnl']:.2%}" if r else '—')
        lines.append('|'+str(year)+'|'+'|'.join(values)+'|')
    lines+=['','2026为截至行情末日的年内实际收益率，上市首年也是部分年度。全年未持仓时收益与效率分开记录。',
            'returns.csv包含毛/扣tick的CAGR及资金效率，yearly_returns.csv包含逐年PnL、年初权益和占用分母；capital_daily.csv为逐日核对。']
    (out/'report.md').write_text('\n'.join(lines),encoding='utf-8');print(summaries,flush=True)
    return summaries,yearly
