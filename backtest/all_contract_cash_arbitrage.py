"""Independent actual-contract ideal index hedge, +4% entry/-2% exit."""
from collections import defaultdict
from datetime import date
from pathlib import Path
import hashlib

from .cash_futures_basis import PAIRS,load_spot
from .cash_carry_signals import annualized_rate
from .factor_data import load_research_data
from .factor_research import csv_if,dump
from .models import D


def analyze_contract(code,instrument,prices,spot,calendar,first_trade,
                     entry_threshold=D('.04'),exit_threshold=D('-.02'),entry_allowed=None):
    index_code=PAIRS[instrument.product][0];mult=instrument.multiplier
    held=None;pending=None;realized=D(0);gross_realized=D(0);cycles=[];daily=[];issues=[];signals=[]
    for i,day in enumerate(calendar):
        if day<first_trade or day>instrument.expiry:continue
        f=prices.get((code,day));s=spot.get((index_code,day))
        event='';did_exit=False
        # Prior completed close signal is attempted on the next observed market date.
        if pending:
            kind,signal_day,signal_rate=pending
            if kind=='entry' and day>=instrument.expiry:
                pending=None;event='entry_cancelled_expiry'
            elif kind=='entry' and entry_allowed is not None and not entry_allowed(day):
                pending=None;event='entry_cancelled_not_front'
            elif not f or not s or f.get('open') is None or s.get('open') is None:
                issues.append(dict(contract=code,day=day,reason=f'{kind}_missing_open'))
                if kind=='entry':pending=None
            elif kind=='entry':
                held=dict(contract=code,product=instrument.product,signal_day=signal_day,entry_day=day,
                          entry_rate=signal_rate,entry_future=f['open'],entry_index=s['open'],
                          multiplier=mult,exit_signal_day=None,exit_day=None,exit_reason=None,
                          exit_rate=None,exit_future=None,exit_index=None,gross_pnl=None,future_tick_pnl=None)
                pending=None;event='entry'
            else:
                gross=(held['entry_future']-f['open']+s['open']-held['entry_index'])*mult
                pnl=gross-2*instrument.tick*mult
                held.update(exit_signal_day=signal_day,exit_day=day,exit_reason='rate_below_minus2' if exit_threshold==D('-.02') else 'rate_below_threshold',exit_rate=signal_rate,
                            exit_future=f['open'],exit_index=s['open'],gross_pnl=gross,future_tick_pnl=pnl)
                cycles.append(held);held=None;pending=None;realized+=pnl;gross_realized+=gross;event='exit';did_exit=True
        if held and day==instrument.delivery:
            if not f or f.get('settlement_price') is None or not s or s.get('close') is None:
                raise ValueError(f'Missing delivery quote {code}/{day}')
            gross=(held['entry_future']-f['settlement_price']+s['close']-held['entry_index'])*mult
            pnl=gross-instrument.tick*mult  # cash settlement has no fictitious closing trade slippage
            held.update(exit_day=day,exit_reason='delivery',exit_future=f['settlement_price'],exit_index=s['close'],
                        gross_pnl=gross,future_tick_pnl=pnl)
            cycles.append(held);held=None;pending=None;realized+=pnl;gross_realized+=gross;event='delivery';did_exit=True
        rate=annualized_rate(f.get('close') if f else None,s.get('close') if s else None,(instrument.expiry-day).days)
        if held and pending is None and rate is not None and rate<exit_threshold:
            pending=('exit',day,rate)
            signals.append(dict(contract=code,product=instrument.product,day=day,kind='exit',rate=rate))
        elif held is None and not did_exit and pending is None and rate is not None and rate>entry_threshold and (entry_allowed is None or entry_allowed(day)):
            pending=('entry',day,rate)
            signals.append(dict(contract=code,product=instrument.product,day=day,kind='entry',rate=rate))
        gross_mark=gross_realized;mark=realized
        if held:
            if not f or f.get('settlement_price') is None or not s or s.get('close') is None:
                mark=gross_mark=None;issues.append(dict(contract=code,day=day,reason='held_valuation_missing'))
            else:
                value=(held['entry_future']-f['settlement_price']+s['close']-held['entry_index'])*mult
                gross_mark+=value;mark+=value-instrument.tick*mult
        daily.append(dict(contract=code,product=instrument.product,day=day,rate=rate,event=event,
                          holding=held is not None,gross_pnl=gross_mark,future_tick_pnl=mark))
    if held:
        held.update(exit_reason='open_at_data_end',gross_pnl=(daily[-1]['gross_pnl']-gross_realized) if daily[-1]['gross_pnl'] is not None else None,
                    future_tick_pnl=(daily[-1]['future_tick_pnl']-realized) if daily[-1]['future_tick_pnl'] is not None else None)
        cycles.append(held)
    if pending:
        issues.append(dict(contract=code,day=calendar[-1],reason=f'pending_{pending[0]}_at_data_end'))
    last=daily[-1] if daily else None
    summary=dict(contract=code,product=instrument.product,first_trade=first_trade,expiry=instrument.expiry,
                 observations=len(daily),cycles=len(cycles),entry_signals=sum(r['kind']=='entry' for r in signals),
                 rate_exits=sum(r['exit_reason'] in ('rate_below_minus2','rate_below_threshold') for r in cycles),
                 delivery_exits=sum(r['exit_reason']=='delivery' for r in cycles),
                 open_at_end=sum(r['exit_reason']=='open_at_data_end' for r in cycles),
                 wins=sum(r['future_tick_pnl'] is not None and r['future_tick_pnl']>0 for r in cycles if r['exit_day'] is not None),
                 gross_pnl=last['gross_pnl'] if last else D(0),future_tick_pnl=last['future_tick_pnl'] if last else D(0))
    return summary,cycles,daily,signals,issues


def run(output='outputs/all_contract_cash_arbitrage',daily_version=None,metadata_version=None,spot_version=None,skip_charts=False):
    out=Path(output);out.mkdir(parents=True,exist_ok=True)
    dv,mv,raw,facts,instruments=load_research_data('data/index_futures',daily_version,metadata_version)
    sv,spot_rows=load_spot(version=spot_version)
    prices={(r['instrument'],r['trading_date']):r for r in raw}
    spot={(r['instrument'],r['trading_date']):r for r in spot_rows}
    calendar=defaultdict(set)
    for r in raw:calendar[r['product']].add(r['trading_date'])
    summaries=[];cycles=[];daily=[];signals=[];issues=[]
    for code,inst in sorted(instruments.items()):
        result=analyze_contract(code,inst,prices,spot,sorted(calendar[inst.product]),date.fromisoformat(facts[code]['first_trade_date']))
        summaries.append(result[0]);cycles.extend(result[1]);daily.extend(result[2]);signals.extend(result[3]);issues.extend(result[4])
    csv_if(out/'contracts.csv',summaries);csv_if(out/'cycles.csv',cycles);csv_if(out/'contract_daily.csv',daily)
    csv_if(out/'signals.csv',signals);csv_if(out/'issues.csv',issues)
    aggregate=[];totals=[]
    for product in ('IF','IH','IC','IM'):
        records=[r for r in summaries if r['product']==product]
        totals.append(dict(product=product,contracts=len(records),traded_contracts=sum(r['cycles']>0 for r in records),
                           entries=sum(r['cycles'] for r in records),gross_pnl=sum((r['gross_pnl'] for r in records if r['gross_pnl'] is not None),D(0)),
                           future_tick_pnl=sum((r['future_tick_pnl'] for r in records if r['future_tick_pnl'] is not None),D(0)),
                           incomplete_terminal_marks=sum(r['future_tick_pnl'] is None for r in records)))
        changes=defaultdict(lambda:[D(0),D(0)])
        bycode=defaultdict(list)
        for r in daily:
            if r['product']==product:bycode[r['contract']].append(r)
        for code,rows in bycode.items():
            last_g=last_p=D(0)
            for r in rows:
                if r['future_tick_pnl'] is None:raise ValueError('Incomplete mark prevents complete aggregate; see issues.csv')
                changes[r['day']][0]+=r['gross_pnl']-last_g;changes[r['day']][1]+=r['future_tick_pnl']-last_p
                last_g,last_p=r['gross_pnl'],r['future_tick_pnl']
        value_g=value_p=D(0)
        for day in sorted(calendar[product]):
            value_g+=changes[day][0];value_p+=changes[day][1]
            aggregate.append(dict(product=product,day=day,gross_pnl=value_g,future_tick_pnl=value_p))
        assert value_p==totals[-1]['future_tick_pnl']
    csv_if(out/'product_summary.csv',totals);csv_if(out/'aggregate_daily.csv',aggregate)
    config=dict(daily_version=dv,metadata_version=mv,spot_version=sv,entry_rate='.04',exit_rate='-.02',
                sizing='independent one short future and multiplier units of theoretical long index per actual contract',
                timing='completed close signal -> next observed market date open; no reentry on exit date',
                costs='gross and actual future tick slippage only; historical exchange fees, ETF costs, financing/dividends not modeled',
                delivery='future final settlement; ideal spot index close; no forced zero terminal basis',
                source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    dump(out/'config.json',config)
    if not skip_charts:plot(out,daily,aggregate,cycles)
    text=['# 全实际股指合约：固定阈值正向套利诊断','',
          '四品种全部实际月份合约独立研究，包括远月/季月，每个合约一手期货空仓和乘数单位的理想指数多头，可能同时持有同品种多月份。指数仅是理论对冲参照，不进入可交易资产或实际Ledger成交。忽略ETF跟踪度依用户指令，不伪造ETF份额/价格。',
          '年化升水=(Fclose−Sclose)/Sclose×365/距expiry自然日数；严格>4%次日开盘入场，严格<−2%次日开盘退出，否则持有至现金交割。到期日分母0留空。缺开盘不假定成交，开仓取消，退出重试。退出日不重新开仓，期末未到期仅估值，不强平。',
          '每日持有估值使用期货结算价与指数收盘；退出采用两腿实际开盘，到期采用期货最终结算和指数收盘。最终结算与指数收盘的取样口径不同，未强行置到期基差为0。',
          '主结果为毛PnL及仅扣期货1tick不利滑点PnL；现金交割不虚构平仓交易滑点。全历史手续费、交割费、ETF交易成本、融资与股息账务未核实，不报告完整净收益。指数原价并非可交易篮子总回报。各独立合约PnL之和不是固定资金、限额下的可执行组合收益。', '',
          '|品种|实际合约数|有入场合约|开仓次数|毛PnL（元）|扣期货tick后（元）|',
          '|---|---:|---:|---:|---:|---:|']
    for r in totals:text.append(f"|{r['product']}|{r['contracts']}|{r['traded_contracts']}|{r['entries']}|{r['gross_pnl']:.2f}|{r['future_tick_pnl']:.2f}|")
    text+=['','contracts.csv逐实际合约汇总，cycles.csv逐开平周期，contract_daily.csv逐合约PnL，signals.csv全部信号日期，aggregate_daily.csv独立结果加总。',
           'pnl_entries.png绘制四品种累计PnL并标记每笔实际候选开仓；contract_charts每个有入场合约一张图，标记该合约全部入场。未入场合约PnL为0，仍在contracts.csv列明。历史元数据公告可知时间与独立交易日历未全面核实。']
    (out/'report.md').write_text('\n'.join(text),encoding='utf-8');print(totals,flush=True)
    return totals


def plot(out,daily,aggregate,cycles,title='All actual contracts independently: entry >4%, exit <-2% or delivery',main_only=False):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    folder=out/'contract_charts';folder.mkdir(exist_ok=True)
    fig,axes=plt.subplots(4,1,figsize=(15,12),constrained_layout=True)
    for product,ax in zip(('IF','IH','IC','IM'),axes):
        group=[r for r in aggregate if r['product']==product]
        values={r['day']:float(r['future_tick_pnl'])/10000 for r in group}
        ax.plot(list(values),list(values.values()),linewidth=1,label='Cumulative ideal hedge PnL; future tick only')
        entries=[r['entry_day'] for r in cycles if r['product']==product]
        ax.scatter(entries,[values[d] for d in entries],marker='^',s=10,color='#ef4444',alpha=.6,label=f'All entries: {len(entries)}')
        ax.axhline(0,color='gray',linewidth=.6);ax.set_title(product,loc='left');ax.set_ylabel('PnL (CNY 10,000)');ax.legend()
    fig.suptitle(title)
    fig.savefig(out/'pnl_entries.png',dpi=160);plt.close(fig)
    if main_only:return
    bycode=defaultdict(list);trades=defaultdict(list)
    for r in daily:bycode[r['contract']].append(r)
    for r in cycles:trades[r['contract']].append(r)
    for code,entries in trades.items():
        group=bycode[code];values={r['day']:float(r['future_tick_pnl']) if r['future_tick_pnl'] is not None else float('nan') for r in group}
        fig,ax=plt.subplots(figsize=(10,4),constrained_layout=True)
        ax.plot(list(values),list(values.values()),linewidth=1)
        ax.scatter([r['entry_day'] for r in entries],[values[r['entry_day']] for r in entries],marker='^',color='#ef4444',s=28,label='All entries')
        ax.axhline(0,color='gray',linewidth=.6);ax.set_title(code+' ideal index hedge; future tick only');ax.set_ylabel('PnL (CNY)');ax.legend()
        fig.savefig(folder/f'{code}.png',dpi=120);plt.close(fig)
