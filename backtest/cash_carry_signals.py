"""Causal annualized basis signals; no claim of executable ETF total-return PnL."""
from collections import defaultdict
from pathlib import Path
import hashlib

from .cash_futures_basis import PAIRS, load_spot
from .factor_data import load_research_data
from .expiry_spread_research import observations
from .factor_research import csv_if, dump
from .futures_spread import PRODUCTS
from .models import D


def annualized_rate(future,index,days):
    if future is None or index is None or index<=0 or days<=0:
        return None
    return (future-index)/index*D(365)/days


def stop_threshold(days):
    return D('.01') if days>=15 else D('.005') if days>=5 else D('.0025')


def make_signals(features,prices,spot,instruments,min_history=252,near_expiry_filter=False,
                 use_stop=False,fee_rate='0.00005',ticks=1):
    spot_prices={(r['instrument'],r['trading_date']):r for r in spot}
    signals,cycles,issues=[],[],[]
    for product in PRODUCTS:
        total,count=D(0),0
        pending=None;held=None
        index_code,etf_code=PAIRS[product]
        group=sorted([r for r in features if r['product']==product],key=lambda r:r['day'])
        for r in group:
            day=r['day']; near=r['near'];inst=instruments.get(near)
            f=prices.get((near,day));s=spot_prices.get((index_code,day));e=spot_prices.get((etf_code,day))
            mean=total/count if count>=min_history else None
            event='';event_code='';decision='warmup' if mean is None else 'flat'
            if pending:
                kind,code,signal_day,signal_rate,signal_mean=pending
                q=prices.get((code,day))
                # These are signal-cycle eligibility checks, not production matching.
                valid=q and q.get('open') is not None and e and e.get('open') is not None and e['ohlcv_complete'] and e.get('source_volume') is not None and e['source_volume']>0
                if kind=='enter' and day>=instruments[code].expiry:
                    pending=None;event='entry_cancelled_expiry'
                elif kind=='enter' and near_expiry_filter and (instruments[code].expiry-day).days<5 and signal_rate<0:
                    pending=None;event='entry_cancelled_near_expiry_negative'
                elif not valid:
                    issues.append(dict(product=product,day=day,code=code,reason=f'{kind}_next_open_not_eligible'))
                    if kind=='enter': pending=None
                elif kind=='enter':
                    held=dict(product=product,future=code,etf=etf_code,signal_day=signal_day,
                              entry_day=day,entry_rate=signal_rate,entry_mean=signal_mean,
                              entry_future_open=q['open'],entry_etf_open=e['open'],
                              entry_signal_index_close=spot_prices[index_code,signal_day]['close'],
                              exit_day=None,exit_reason=None,exit_rate=None,exit_mean=None,
                              exit_future_price=None,exit_etf_price=None,held_calendar_days=None)
                    if use_stop:
                        b=spot_prices[etf_code,signal_day]
                        quantity=int(held['entry_signal_index_close']*instruments[code].multiplier/b['close']/100)*100
                        fp=q['open']-instruments[code].tick*ticks
                        ep=e['open']+D('.001')*ticks
                        held.update(stop_etf_quantity=quantity,stop_future_entry=fp,stop_etf_entry=ep,
                                    stop_open_fees=(fp*instruments[code].multiplier+ep*quantity)*D(fee_rate),
                                    stop_denominator=e['open']*quantity,stop_trigger_day=None,
                                    stop_trigger_pnl=None,stop_trigger_ratio=None,stop_trigger_threshold=None)
                    pending=None;event='eligible_entry';event_code=code
                else:
                    held.update(exit_day=day,exit_reason='stop_loss' if kind=='stop_loss' else 'historical_mean',exit_rate=signal_rate,exit_mean=signal_mean,
                                exit_future_price=q['open'],exit_etf_price=e['open'],held_calendar_days=(day-held['entry_day']).days)
                    cycles.append(held);held=None;pending=None;event='eligible_stop_exit' if kind=='stop_loss' else 'eligible_mean_exit';event_code=code
            expiry_held=held and day==instruments[held['future']].expiry
            if expiry_held:
                q=prices.get((held['future'],day))
                held.update(exit_day=day,exit_reason='expiry',exit_future_price=q.get('settlement_price') if q else None,
                            exit_etf_price=e.get('close') if e and e['ohlcv_complete'] else None,
                            held_calendar_days=(day-held['entry_day']).days)
                if held['exit_future_price'] is None or held['exit_etf_price'] is None:
                    issues.append(dict(product=product,day=day,code=held['future'],reason='expiry_exit_quote_incomplete'))
                cycles.append(held);held=None;pending=None;event='expiry_exit_signal';event_code=q['instrument'] if q and 'instrument' in q else near
            rate=annualized_rate(f.get('close') if f else None,s.get('close') if s else None,
                                 (inst.expiry-day).days if inst else 0)
            held_rate=None
            mark_pnl=mark_ratio=threshold=None
            if held:
                old=prices.get((held['future'],day))
                held_rate=annualized_rate(old.get('close') if old else None,s.get('close') if s else None,
                                          (instruments[held['future']].expiry-day).days)
                decision='hold'
                if use_stop:
                    days_left=(instruments[held['future']].expiry-day).days
                    threshold=stop_threshold(days_left)
                    if old and old.get('settlement_price') is not None and e and e.get('close') is not None:
                        mark_pnl=(held['stop_future_entry']-old['settlement_price'])*instruments[held['future']].multiplier+held['stop_etf_quantity']*(e['close']-held['stop_etf_entry'])-held['stop_open_fees']
                        mark_ratio=mark_pnl/held['stop_denominator']
                    else:
                        issues.append(dict(product=product,day=day,code=held['future'],reason='missing_stop_valuation'))
                if pending and pending[0]=='stop_loss':
                    decision='retry_stop_exit'
                elif use_stop and mark_ratio is not None and mark_ratio<=-threshold:
                    pending=('stop_loss',held['future'],day,held_rate,mean);decision='schedule_stop_exit'
                    held.update(stop_trigger_day=day,stop_trigger_pnl=mark_pnl,stop_trigger_ratio=mark_ratio,stop_trigger_threshold=threshold)
                elif held_rate is not None and mean is not None and held_rate<=mean:
                    pending=('exit',held['future'],day,held_rate,mean);decision='schedule_mean_exit'
            elif not event and pending is None and rate is not None and mean is not None and rate>mean:
                if near_expiry_filter and (inst.expiry-day).days<5 and rate<0:
                    decision='filtered_near_expiry_negative'
                elif r['execution_day'] is not None and r['execution_day']<inst.expiry:
                    pending=('enter',near,day,rate,mean);decision='schedule_entry'
                else:
                    decision='no_open_before_expiry_or_data_end'
            if pending and pending[0]=='exit':decision='schedule_mean_exit'
            record=dict(product=product,day=day,future=near,index=index_code,etf=etf_code,
                                dte=(inst.expiry-day).days if inst else None,
                                future_close=f.get('close') if f else None,index_close=s.get('close') if s else None,
                                etf_close=e.get('close') if e else None,annualized_basis_rate=rate,
                                historical_mean=mean,reference_count=count,held_future=held['future'] if held else None,
                                held_rate=held_rate,decision=decision,event=event,event_future=event_code)
            if use_stop:record.update(mark_pnl=mark_pnl,mark_pnl_ratio=mark_ratio,stop_threshold=threshold)
            signals.append(record)
            # Current observation enters only tomorrow's reference. Expiry-day rate is undefined.
            if rate is not None:
                total+=rate;count+=1
        if held:
            held['exit_reason']='open_at_data_end'
            cycles.append(held)
    return signals,cycles,issues


def run(output='outputs/index_futures_cash_carry_signals',min_history=252,daily_version=None,metadata_version=None,spot_version=None):
    if min_history<2:raise ValueError('min_history must be at least 2')
    out=Path(output);out.mkdir(parents=True,exist_ok=True)
    dv,mv,raw,facts,instruments=load_research_data('data/index_futures',daily_version,metadata_version)
    sv,spot=load_spot(version=spot_version)
    features,prices,calendars=observations(raw,facts,instruments)
    signals,cycles,issues=make_signals(features,prices,spot,instruments,min_history)
    csv_if(out/'daily_signals.csv',signals);csv_if(out/'signal_cycles.csv',cycles);csv_if(out/'issues.csv',issues)
    summaries=[]
    for product in PRODUCTS:
        group=[r for r in signals if r['product']==product]
        valid=[r for r in group if r['annualized_basis_rate'] is not None]
        eligible=[r for r in group if r['historical_mean'] is not None and r['annualized_basis_rate'] is not None]
        trades=[r for r in cycles if r['product']==product]
        summaries.append(dict(product=product,data_start=group[0]['day'],data_end=group[-1]['day'],
                              valid_days=len(valid),mean_annualized_rate=sum((r['annualized_basis_rate'] for r in valid),D(0))/len(valid),
                              above_mean_days=sum(r['annualized_basis_rate']>r['historical_mean'] for r in eligible),
                              eligible_entries=len(trades),negative_rate_entries=sum(r['entry_rate']<0 for r in trades),
                              mean_exits=sum(r['exit_reason']=='historical_mean' for r in trades),
                              expiry_exits=sum(r['exit_reason']=='expiry' for r in trades),
                              open_at_end=sum(r['exit_reason']=='open_at_data_end' for r in trades)))
    csv_if(out/'summary.csv',summaries)
    config=dict(daily_version=dv,metadata_version=mv,spot_version=sv,min_history=min_history,
                rate='(F-S)/S*365/calendar_days_to_expiry',reference='all strictly earlier valid observations per product',
                enter='rate > historical_mean (negative rate allowed)',exit='rate <= current historical_mean, or expiry',
                execution='next observed date open eligibility only; expiry futures settlement / ETF close; no PnL backtest',
                source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    dump(out/'config.json',config)
    plot(out,signals)
    text=['# 正向期现套利：历史均值信号分析','',
          '年化价差率 y=(F−S)/S×365/d，其中F为当月期货收盘、S为对应指数收盘，d为到期自然日数。d=0留空；贴水时为负。该指标不含股息、融资、费用和ETF跟踪误差，不是预期可得净收益率。',
          f'历史均值使用同品种此前所有有效日（包括ETF出现前的可观察期货/指数），至少{min_history}个样本；当天不进入均值，未使用未来数据。均值随过去数据更新。不同到期日数年化后合并统计，未另作到期桶匹配；接近到期时年化指标可能极端。',
          '空仓且y>历史均值则安排下一观察交易日买ETF、卖当月期货；负数但高于均值也开仓，不额外加正收益门槛。持有期间使用原实际合约价差率，y≤当时历史均值安排次日开盘退出；原近月到期则期货结算价、ETF收盘价作为退出观察。不展期、不当天平开反向。',
          'signal_cycles中的日期是日线行情资格满足的信号周期，不是Engine已成交回测。下一日两腿开盘必须存在，ETF OHLCV完整且成交量为正；开仓缺数据取消，平仓持续等候。没有模拟排队、参与率、真实时钟或部分成交状态机。',
          '本轮不输出ETF两腿PnL：ETF分红、份额调整及历史证券元数据尚未完整核实，不从价格跳变猜测公司行为。不把指数基差收敛冒充ETF实现收益。研究意图为一手期货、ETF名义对冲；实际ETF份额与成本留待执行研究配置。','',
          '|品种|合格开仓周期|其中负年化率开仓|均值退出|到期退出|期末未退出|',
          '|---|---:|---:|---:|---:|---:|']
    for r in summaries:
        text.append(f"|{r['product']}|{r['eligible_entries']}|{r['negative_rate_entries']}|{r['mean_exits']}|{r['expiry_exits']}|{r['open_at_end']}|")
    text += ['',f'固定快照：期货{dv}；合约{mv}；指数ETF{sv}。',
             'daily_signals.csv逐日保存价格、年化率、历史均值、样本数、持有实际合约和决定；signal_cycles.csv保留开平价格观察和原因；issues.csv列不完整观察；summary.csv汇总。']
    (out/'report.md').write_text('\n'.join(text),encoding='utf-8')
    print(summaries,flush=True)
    return summaries


def plot(out,signals):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig,axes=plt.subplots(4,1,figsize=(14,12),constrained_layout=True)
    for product,ax in zip(PRODUCTS,axes):
        group=[r for r in signals if r['product']==product]
        ax.plot([r['day'] for r in group],[float(r['annualized_basis_rate'])*100 if r['annualized_basis_rate'] is not None else float('nan') for r in group],label='Annualized basis (%)',linewidth=.6)
        ax.plot([r['day'] for r in group],[float(r['historical_mean'])*100 if r['historical_mean'] is not None else float('nan') for r in group],label='Past expanding mean',color='orange')
        ax.axhline(0,color='gray',linewidth=.6);ax.set_title(product,loc='left');ax.set_ylabel('Annualized %');ax.legend()
    fig.savefig(out/'annualized_basis_signals.png',dpi=150);plt.close(fig)
