"""Causal one-calendar-year threshold selection; actual held exit thresholds frozen."""
from bisect import bisect_left,bisect_right
from collections import defaultdict
from datetime import date,timedelta
from pathlib import Path
import csv
import hashlib
import json

from .arbitrage_repo_returns import apply_repo,repo_rate
from .cash_carry_signals import annualized_rate
from .cash_futures_basis import load_spot,PAIRS
from .factor_data import load_research_data
from .front_contract_arbitrage import front_map,THRESHOLDS
from .factor_research import csv_if,dump
from .models import D

PRODUCTS=('IF','IH','IC','IM')
ENTRIES=tuple(map(D,('0','.01','.02','.03','.04','.06','.08')))
EXITS=tuple(map(D,('-.08','-.06','-.04','-.02','0')))


def year_before(day):
    try:return day.replace(year=day.year-1)
    except ValueError:return day.replace(year=day.year-1,day=28)


def cash_growth(balance,begin,end):
    """Decimal calendar compound factor; annual rates change on Jan 1."""
    if balance<=0 or end<begin:return balance
    while begin<=end:
        finish=min(end,date(begin.year,12,31))
        balance*= (1+repo_rate(begin)/D(365))**((finish-begin).days+1)
        begin=finish+timedelta(days=1)
    return balance


class ProductMarket:
    def __init__(self,product,dates,mapping,prices,spot,instruments,rate_provider=None):
        self.product,self.dates,self.mapping=product,dates,mapping
        self.prices,self.spot,self.instruments=prices,spot,instruments
        self.rate_provider=rate_provider
        self.index=PAIRS[product][0]
        self.front=[mapping[product,d] for d in dates]
        self.rates=[]
        for day,code in zip(dates,self.front):
            f=prices.get((code,day));s=spot.get((self.index,day))
            self.rates.append(rate_provider(product,code,day) if rate_provider else
                              annualized_rate(f['close'] if f else None,s['close'] if s else None,
                              (instruments[code].expiry-day).days if code else 0))
        self.entry_indices={t:[i for i,y in enumerate(self.rates) if y is not None and y>t] for t in ENTRIES}
        self.exit_indices={t:[i for i,y in enumerate(self.rates) if y is not None and y<t] for t in EXITS}
        self.pnl_cache={}

    def eligible_open(self,code,i):
        if i>=len(self.dates):return False
        day=self.dates[i];f=self.prices.get((code,day));s=self.spot.get((self.index,day))
        return bool(f and s and f.get('open') is not None and s.get('open') is not None)

    def pnl(self,entry,finish,kind):
        key=entry,finish,kind
        if key not in self.pnl_cache:
            code=self.front[entry];inst=self.instruments[code]
            a=self.prices[code,self.dates[entry]];b=self.prices[code,self.dates[finish]]
            s=self.spot[self.index,self.dates[entry]];e=self.spot[self.index,self.dates[finish]]
            fprice=b['open'] if kind=='open' else b['settlement_price']
            sprice=e['open'] if kind=='open' else e['close']
            gross=(a['open']-fprice+sprice-s['open'])*inst.multiplier
            self.pnl_cache[key]=(gross,gross-inst.tick*inst.multiplier*(2 if kind=='open' else 1))
        return self.pnl_cache[key]

    def evaluate(self,begin,asof,entry_threshold,exit_threshold,capital):
        lower=bisect_left(self.dates,begin);limit=asof
        entries=self.entry_indices[entry_threshold];exits=self.exit_indices[exit_threshold]
        pointer=bisect_left(entries,lower);cursor=lower;balance=capital;flat_begin=begin
        count=0;pnl_total=D(0);open_at_end=False
        while pointer<len(entries):
            sig=entries[pointer];pointer+=1
            if sig<cursor:continue
            if sig+1>limit:break # today's pending entry cannot use tomorrow's open
            entry=sig+1;code=self.front[sig];inst=self.instruments[code]
            if self.front[entry]!=code or self.dates[entry]>=inst.expiry or not self.eligible_open(code,entry):continue
            expiry=bisect_left(self.dates,inst.expiry)
            x=bisect_left(exits,entry);exit_index=None;reason=None
            while x<len(exits) and exits[x]<=limit:
                signal=exits[x];x+=1
                if signal>=expiry:break
                target=signal+1
                # Pending exit persists across quote gaps; never inspect beyond known window.
                while target<=limit and target<expiry and not self.eligible_open(code,target):target+=1
                if target<=limit and target<=expiry and self.eligible_open(code,target):
                    exit_index=target;reason='open';break
                break
            if exit_index is None:
                if expiry<=limit:exit_index=expiry;reason='delivery'
                else:exit_index=limit;reason='mark';open_at_end=True
            balance=cash_growth(balance,flat_begin,self.dates[entry]-timedelta(days=1))
            _,pnl=self.pnl(entry,exit_index,reason)
            balance+=pnl;pnl_total+=pnl;count+=1
            flat_begin=self.dates[exit_index]+timedelta(days=1)
            cursor=exit_index+1 # do not signal a reentry on the exit date
            if reason=='mark':break
        if not open_at_end:balance=cash_growth(balance,flat_begin,self.dates[limit])
        return dict(account_return=(balance-capital)/capital,arbitrage_pnl=pnl_total,
                    estimated_repo_interest=balance-capital-pnl_total,cycles=count,open_at_end=open_at_end)

    def select(self,i,writer=None):
        asof=self.dates[i];begin=year_before(asof)+timedelta(days=1)
        j=bisect_left(self.dates,begin)
        capital=max(self.spot[self.index,d]['close'] for d in self.dates[j:i+1])*self.instruments[self.front[i]].multiplier*D('1.5')
        best=None
        for enter in ENTRIES:
            for exit_ in EXITS:
                record=self.evaluate(begin,i,enter,exit_,capital)
                # ROI rounded at 1e-8; daily repo cash rounding is insignificant below this score resolution.
                score=record['account_return'].quantize(D('.00000001'))
                key=(score,-record['cycles'],enter,exit_)
                row=dict(product=self.product,decision_day=asof,window_start=begin,window_end=asof,
                         common_capital=capital,entry_threshold=enter,exit_threshold=exit_,score=score,**record)
                if writer is not None:writer.writerow(row)
                if best is None or key>best[0]:best=(key,row)
        return best[1]


def walk_forward(market,selector=None,fixed=None,progress=print):
    dates=market.dates
    try:eligible_after=dates[0].replace(year=dates[0].year+1)
    except ValueError:eligible_after=dates[0].replace(year=dates[0].year+1,day=28)
    start=bisect_left(dates,eligible_after)
    if start>=len(dates):raise ValueError('Less than one calendar year of observations')
    held=pending=None;realized=gross_realized=D(0);cycles=[];daily=[];selections=[];decisions=[]
    for i in range(start,len(dates)):
        day=dates[i];code=market.front[i];inst=market.instruments[code]
        event='';exited=False
        if pending:
            kind,signal_i,code_,params=pending
            if kind=='entry':
                if market.front[i]==code_ and day<market.instruments[code_].expiry and market.eligible_open(code_,i):
                    a=market.prices[code_,day];s=market.spot[market.index,day]
                    held=dict(contract=code_,product=market.product,signal_day=dates[signal_i],entry_day=day,
                              entry_index=s['open'],entry_future=a['open'],multiplier=market.instruments[code_].multiplier,
                              entry_i=i,entry_threshold=params[0],exit_threshold=params[1],selection_day=dates[signal_i],
                              exit_day=None,exit_reason=None,gross_pnl=None,future_tick_pnl=None)
                    event='entry'
                pending=None
            elif market.eligible_open(code_,i):
                gross,pnl=market.pnl(held['entry_i'],i,'open')
                held.update(exit_day=day,exit_reason='threshold',gross_pnl=gross,future_tick_pnl=pnl)
                cycles.append(held);held=None;pending=None;realized+=pnl;gross_realized+=gross;exited=True;event='exit'
        if held and day==market.instruments[held['contract']].delivery:
            gross,pnl=market.pnl(held['entry_i'],i,'delivery')
            held.update(exit_day=day,exit_reason='delivery',gross_pnl=gross,future_tick_pnl=pnl)
            cycles.append(held);held=None;pending=None;realized+=pnl;gross_realized+=gross;exited=True;event='delivery'
        if held:
            y=market.rates[i]
            if pending is None and y is not None and y<held['exit_threshold']:
                pending=('exit',i,held['contract'],(held['entry_threshold'],held['exit_threshold']))
        elif not exited and day<inst.expiry:
            if fixed is None:
                choice=selector(i);selections.append(choice)
                params=choice['entry_threshold'],choice['exit_threshold']
            else:params=fixed
            y=market.rates[i]
            if y is not None and y>params[0]:pending=('entry',i,code,params)
            decisions.append(dict(product=market.product,day=day,rate=y,entry_threshold=params[0],exit_threshold=params[1],
                                  schedule_entry=bool(pending)))
        g,n=gross_realized,realized
        if held:
            extra_g,extra_n=market.pnl(held['entry_i'],i,'mark');g+=extra_g;n+=extra_n
        daily.append(dict(product=market.product,day=day,gross_pnl=g,future_tick_pnl=n,event=event,holding=bool(held)))
        if i%500==0:progress(f'{market.product}: {day}, cycles={len(cycles)}',flush=True)
    if held:
        g,n=market.pnl(held['entry_i'],len(dates)-1,'mark')
        held.update(exit_reason='open_at_data_end',gross_pnl=g,future_tick_pnl=n);cycles.append(held)
    for cycle in cycles:cycle.pop('entry_i',None)
    pending_signal=None if pending is None else dict(product=market.product,kind=pending[0],signal_day=dates[pending[1]],
                    contract=pending[2],entry_threshold=pending[3][0],exit_threshold=pending[3][1])
    return dict(cycles=cycles,daily=daily,selections=selections,decisions=decisions,start=dates[start],pending_signal=pending_signal)


def run(output='outputs/rolling_front_arbitrage',skip_charts=False):
    out=Path(output);out.mkdir(parents=True,exist_ok=True)
    source=json.loads(Path('outputs/front_contract_cash_arbitrage/config.json').read_text(encoding='utf-8'))
    _,_,raw,facts,instruments=load_research_data('data/index_futures',source['daily_version'],source['metadata_version'])
    _,spot_rows=load_spot(version=source['spot_version'])
    prices={(r['instrument'],r['trading_date']):r for r in raw};spot={(r['instrument'],r['trading_date']):r for r in spot_rows}
    calendars={p:sorted({r['trading_date'] for r in raw if r['product']==p}) for p in PRODUCTS}
    first={c:date.fromisoformat(f['first_trade_date']) for c,f in facts.items()};mapping=front_map(instruments,first,calendars)
    fieldnames=['product','decision_day','window_start','window_end','common_capital','entry_threshold','exit_threshold','score',
                'account_return','arbitrage_pnl','estimated_repo_interest','cycles','open_at_end']
    summaries=[];all_yearly=[];all_calendars=[];all_cycles=[];all_selections=[];all_decisions=[];pending=[]
    with (out/'all_trials.csv').open('w',newline='',encoding='utf-8-sig') as handle:
        writer=csv.DictWriter(handle,fieldnames=fieldnames);writer.writeheader()
        for product in PRODUCTS:
            market=ProductMarket(product,calendars[product],mapping,prices,spot,instruments)
            print(f'{product}: optimizing 35 pairs over trailing one calendar year',flush=True)
            rolling=walk_forward(market,selector=lambda i,m=market:m.select(i,writer))
            baseline=walk_forward(market,fixed=THRESHOLDS[product])
            common_capital=max(spot[market.index,r['day']]['close'] for r in rolling['daily'])*instruments[market.front[-1]].multiplier*D('1.5')
            for label,result in [('rolling',rolling),('fixed',baseline)]:
                trading=[dict(day=r['day'],cumulative_future_tick_pnl=r['future_tick_pnl']) for r in result['daily']]
                summary,yearly,calendar=apply_repo(common_capital,trading,result['cycles'])
                summaries.append(dict(product=product,strategy=label,cycles=len(result['cycles']),**summary))
                all_yearly.extend(dict(product=product,strategy=label,**r) for r in yearly)
                all_calendars.extend(dict(product=product,strategy=label,**r) for r in calendar)
                all_cycles.extend(dict(strategy=label,**r) for r in result['cycles'])
                if result['pending_signal'] is not None:pending.append(dict(strategy=label,**result['pending_signal']))
                print(product,label,'CAGR',summary['combined_cagr'],'PnL',summary['combined_pnl'],flush=True)
            all_selections.extend(rolling['selections']);all_decisions.extend(rolling['decisions'])
    csv_if(out/'summary.csv',summaries);csv_if(out/'yearly.csv',all_yearly);csv_if(out/'calendar_daily.csv',all_calendars)
    csv_if(out/'cycles.csv',all_cycles);csv_if(out/'selections.csv',all_selections);csv_if(out/'decisions.csv',all_decisions)
    csv_if(out/'pending_signals.csv',pending)
    dump(out/'config.json',dict(source_snapshots=source,entries=ENTRIES,exits=EXITS,
        lookback='one calendar anniversary through completed decision date, start flat; terminal MTM only',
        objective='maximum past-window account return incl estimated flat-date repo',
        update='every eligible flat decision close; pair fixed during actual holding',
        selection_capital='same for all 35 trials: trailing-year index close peak*multiplier*1.5; known at decision',
        evaluation_capital='same rolling/fixed reserve: evaluation index close peak*multiplier*1.5; retrospective reporting only',
        tie_break='ROI quantized1e-8 then fewer cycles then higher entry then higher exit',
        training_repo='Decimal compounded factors without daily cash rounding; final account repo rounded1e-8 CNY/day',
        source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()))
    if not skip_charts:plot(out,all_calendars,all_cycles,all_selections)
    text=['# 当月期现滚动一年参数优化','',
          '每品种独立35组：开仓0/1/2/3/4/6/8%，平仓−8/−6/−4/−2/0%。严格大于开仓、严格低于平仓。目标过去一年账户收益率最高，包含此前确认的完全空仓自然日回购估算。',
          '空仓合格决策收盘重新选择，以该已完成日为窗口终点、上一年同日次日起为起点（闰年按周年）。每候选独立从空仓重放，只使用窗口内信号，最后未平仓按当时价格估值，不借未来交割结果，不强平。当前尚待次日开盘的信号不计未来成交。',
          '35候选共用当时窗口内指数close峰值×乘数×1.5本金；ROI保留1e−8评分，相同优先少交易、较高开仓阈值、较高平仓阈值。训练回购用Decimal复利因子，最终账户逐自然日保留1e−8元，微小结算舍入不作精细择优。',
          '实际收盘信号次日开盘，已入场的开平阈值冻结直至退出/交割，不自动展期。各品种开始先观察满一年，之后才统计滚动表现。固定参数对照在相同起止从空仓开始；双方共用评估期单份指数close名义峰值×1.5本金（事后报告尺度，未进入参数选择）。',
          '回购、理想指数对冲及期货tick口径沿用；完整手续费、ETF成本、融资/股息仍未计。该历史已多次用于研究，滚动因果检验不是全新独立保留样本，也不能因择优保证未来有效。','',
          '|品种|策略|评估起始|周期数|套利PnL（万元）|回购（万元）|合计（万元）|CAGR|',
          '|---|---|---|---:|---:|---:|---:|---:|']
    for r in summaries:text.append(f"|{r['product']}|{r['strategy']}|{r['start']}|{r['cycles']}|{r['arbitrage_pnl']/10000:.2f}|{r['repo_interest']/10000:.2f}|{r['combined_pnl']/10000:.2f}|{r['combined_cagr']:.2%}|")
    text+=['','all_trials.csv保留每次全部35候选；selections为选择，decisions为信号，cycles保留实际冻结阈值；yearly与calendar_daily拆解套利和回购。图给出同口径权益、开仓标记和选择阈值变动。']
    (out/'report.md').write_text('\n'.join(text),encoding='utf-8')
    return summaries


def plot(out,calendar,cycles,selections):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig,axes=plt.subplots(4,1,figsize=(14,12),constrained_layout=True)
    for product,ax in zip(PRODUCTS,axes):
        for strategy in ('rolling','fixed'):
            group=[r for r in calendar if r['product']==product and r['strategy']==strategy]
            initial=group[0]['opening_equity']
            ax.plot([r['day'] for r in group],[float(r['equity']/initial) for r in group],label=strategy)
            if strategy=='rolling':
                values={r['day']:float(r['equity']/initial) for r in group}
                entries=[r['entry_day'] for r in cycles if r['product']==product and r['strategy']==strategy]
                ax.scatter(entries,[values[d] for d in entries],s=9,marker='^',color='red',label='all rolling entries')
        ax.set_title(product,loc='left');ax.set_ylabel('Account NAV incl estimated repo');ax.legend()
    fig.savefig(out/'rolling_nav.png',dpi=150);plt.close(fig)
    fig,axes=plt.subplots(4,1,figsize=(14,10),constrained_layout=True)
    for product,ax in zip(PRODUCTS,axes):
        group=[r for r in selections if r['product']==product]
        ax.step([r['decision_day'] for r in group],[100*float(r['entry_threshold']) for r in group],label='entry threshold')
        ax.step([r['decision_day'] for r in group],[100*float(r['exit_threshold']) for r in group],label='exit threshold')
        ax.set_title(product,loc='left');ax.set_ylabel('%');ax.legend()
    fig.savefig(out/'selected_thresholds.png',dpi=150);plt.close(fig)
