"""Replay recorded signal cycles with Decimal Ledger; raw ETF price PnL only."""
from collections import defaultdict
from dataclasses import asdict
from datetime import date
from pathlib import Path
import csv
import hashlib
import json

from .accounting import Ledger
from .cash_futures_basis import PAIRS,load_spot
from .factor_data import load_research_data
from .factor_research import csv_if,dump
from .factor_simulation import execute,summary
from .futures_spread import PRODUCTS
from .models import D,Instrument,Asset,Account


def read_cycles(path):
    rows=list(csv.DictReader(Path(path).open(encoding='utf-8-sig')))
    for i,r in enumerate(rows):
        r['id']=f"{r['product']}_{i}"
        for key in ('signal_day','entry_day','exit_day'):
            r[key]=date.fromisoformat(r[key]) if r[key] else None
    return rows


def replay(cycles,prices,spot,instruments,days,fee_rate='0.00005',ticks=1,initial_cash='10000000'):
    instruments=dict(instruments)
    for product,(_,etf) in PAIRS.items():
        instruments[etf]=Instrument(etf,Asset.FUND,Account.SECURITY,'SSE',product,D(1),D('.001'),100,sell_delay_days=1)
    indexed={(r['instrument'],r['trading_date']):r for r in spot}
    events=defaultdict(list);quantities={};trial_details=[]
    for cycle in cycles:
        product=cycle['product'];index,etf=PAIRS[product];signal=cycle['signal_day']
        a,b=indexed.get((index,signal)),indexed.get((etf,signal))
        if not a or not b or b['close']<=0:raise ValueError('Missing sizing observation')
        q=int(a['close']*instruments[cycle['future']].multiplier/b['close']/100)*100
        if q<=0:raise ValueError('Nonpositive ETF hedge quantity')
        quantities[cycle['id']]=q
        events[cycle['entry_day']].append(('open',cycle))
        if cycle['exit_day']:events[cycle['exit_day']].append(('close',cycle))
        trial_details.append(dict(cycle_id=cycle['id'],product=product,future=cycle['future'],etf=etf,
                                  signal_day=signal,entry_day=cycle['entry_day'],exit_day=cycle['exit_day'],
                                  exit_reason=cycle['exit_reason'],etf_quantity=q,future_quantity=1,
                                  entry_rate=cycle['entry_rate'],raw_price_pnl=None,fees=None,slippage=None,
                                  zero_cost_pnl=None,suspected_price_jump=False))
    ledger=Ledger(instruments,initial_cash);daily=[];last=D(initial_cash);previous_spot={};suspects=set()
    for day in days:
        first_fill=len(ledger.fills)
        for effect,cycle in sorted(events.get(day,[]),key=lambda x:x[0]=='open'):
            code,etf,group=cycle['future'],cycle['etf'],cycle['id']
            f=prices.get((code,day));e=indexed.get((etf,day))
            expiry=effect=='close' and cycle['exit_reason']=='expiry'
            if not f or not e:raise ValueError(f'Missing scheduled cycle quote {group}/{day}')
            if not e['ohlcv_complete'] or e.get('source_volume') is None or e['source_volume']<=0:
                raise ValueError(f'ETF execution observation incomplete {group}/{day}')
            quote='close' if expiry else 'open'
            if e.get(quote) is None:raise ValueError('Missing ETF execution price')
            if not expiry:
                if f.get('open') is None:raise ValueError('Missing futures execution price')
                execute(ledger,code,-1,1,effect,group,day,f['open'],D(fee_rate),ticks)
            execute(ledger,etf,1,quantities[group],effect,group,day,e[quote],D(fee_rate),ticks)
        for product,(index,etf) in PAIRS.items():
            a,b=indexed.get((index,day)),indexed.get((etf,day))
            if a and b:
                if product in previous_spot:
                    old_a,old_b=previous_spot[product]
                    jump=(b['close']/old_b)/(a['close']/old_a)-1
                    if abs(jump)>D('.05'):
                        suspects.update(p.group for p in ledger.lots if p.instrument==etf)
                        suspects.update(c['id'] for effect,c in events.get(day,[]) if c['product']==product)
                previous_spot[product]=(a['close'],b['close'])
        for code in sorted({p.instrument for p in ledger.lots}):
            if instruments[code].asset==Asset.FUTURE:
                q=prices.get((code,day))
                if not q or q.get('settlement_price') is None:raise ValueError(f'Missing held futures settlement {code}/{day}')
                ledger.settle(code,day,q['settlement_price'])
            else:
                q=indexed.get((code,day))
                if not q or q.get('close') is None:raise ValueError(f'Missing held ETF valuation {code}/{day}')
                ledger.marks[code]=q['close']
        attribution=ledger.group_pnl()
        equity=D(initial_cash)+sum(attribution.values(),D(0))
        assert equity==ledger.cash+sum((p.quantity*ledger.marks[p.instrument] for p in ledger.lots if instruments[p.instrument].asset==Asset.FUND),D(0))
        fills=ledger.fills[first_fill:]
        record=dict(day=day,cash=ledger.cash,equity=equity,return_=((equity-last)/last if last>0 else D(0)),
                    fees=sum((f.fee for f in fills),D(0)),fills=len(fills),cash_deficit=max(-ledger.cash,D(0)),
                    nominal=sum((p.quantity*instruments[p.instrument].multiplier*ledger.marks[p.instrument] for p in ledger.lots),D(0)),
                    traded_notional=sum((f.price*f.instruction.quantity*instruments[f.instruction.instrument].multiplier for f in fills),D(0)))
        record['return']=record.pop('return_')
        for product in PRODUCTS:
            record[f'cumulative_pnl_{product}']=sum((v for k,v in attribution.items() if k.startswith(product+'_')),D(0))
        daily.append(record);last=equity
    attribution=ledger.group_pnl()
    for r in trial_details:
        group=r['cycle_id'];fills=[f for f in ledger.fills if f.instruction.group==group]
        fees=sum((f.fee for f in fills),D(0))
        slip=sum((instruments[f.instruction.instrument].tick*ticks*f.instruction.quantity*instruments[f.instruction.instrument].multiplier for f in fills),D(0))
        net=attribution.get(group,D(0))
        r.update(raw_price_pnl=net,fees=fees,slippage=slip,zero_cost_pnl=net+fees+slip,
                 suspected_price_jump=group in suspects)
    return dict(daily=daily,cycles=trial_details,fills=[dict(day=f.trading_date,**asdict(f.instruction),price=f.price,fee=f.fee) for f in ledger.fills],
                entries=ledger.entries,terminal_positions=[asdict(p) for p in ledger.lots],metrics=summary(daily,D(initial_cash)))


def run(output='outputs/index_futures_cash_carry_pnl',source='outputs/index_futures_cash_carry_signals'):
    out=Path(output);out.mkdir(parents=True,exist_ok=True);source=Path(source)
    config=json.loads((source/'config.json').read_text(encoding='utf-8'))
    _,_,raw,_,inst=load_research_data('data/index_futures',config['daily_version'],config['metadata_version'])
    _,spot=load_spot(version=config['spot_version'])
    cycles=read_cycles(source/'signal_cycles.csv')
    prices={(r['instrument'],r['trading_date']):r for r in raw}
    days=sorted({r['trading_date'] for r in raw})
    totals=[]
    for scenario,rate,ticks in [('base','0.00005',1),('stress','0.0001',2)]:
        result=replay(cycles,prices,spot,inst,days,rate,ticks)
        for kind in ('daily','cycles','fills','entries'):csv_if(out/f'{scenario}_{kind}.csv',result[kind])
        dump(out/f'{scenario}_terminal_positions.json',result['terminal_positions'])
        for product in PRODUCTS:
            group=[r for r in result['cycles'] if r['product']==product]
            closed=[r for r in group if r['exit_day'] is not None]
            totals.append(dict(scenario=scenario,product=product,cycles=len(group),closed_cycles=len(closed),
                               wins=sum(r['raw_price_pnl']>0 for r in closed),
                               net_raw_price_pnl=sum((r['raw_price_pnl'] for r in group),D(0)),
                               zero_cost_pnl=sum((r['zero_cost_pnl'] for r in group),D(0)),
                               fees=sum((r['fees'] for r in group),D(0)),slippage=sum((r['slippage'] for r in group),D(0)),
                               flagged_cycles=sum(r['suspected_price_jump'] for r in group),
                               flagged_pnl=sum((r['raw_price_pnl'] for r in group if r['suspected_price_jump']),D(0))))
        if scenario=='base':base=result
    csv_if(out/'summary.csv',totals)
    dump(out/'config.json',dict(signal_config=config,signal_cycles_sha256=hashlib.sha256((source/'signal_cycles.csv').read_bytes()).hexdigest(),
                                initial_cash='10000000',ETF_lot_assumption=100,ETF_tick_assumption='.001',ETF_T1_assumption=True,
                                sizing='one short future; ETF shares floor100(signal index close * multiplier / signal ETF close)',
                                source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()))
    report=['# 正向期现信号盈亏测试（ETF原价诊断）','',
            '固定此前信号周期和快照；一手期货空仓，ETF份额按信号日指数名义金额/ETF收盘价向下取100份，持有不调仓。统一1000万元现金，不因现金不足缩量。',
            '日期型研究：信号周期次日开盘，均值退出次日开盘；到期期货以最终结算价现金结清，ETF以当日收盘退出。期货逐日Ledger结算，ETF按收盘估值；期末不强平。',
            '研究成本基准每腿每次0.5bp＋1tick（ETF假设tick=0.001元）；压力1bp＋2tick。ETF lot100、T+1为研究执行假设，历史规则尚未加载。全部腿分别记费和不利滑点。',
            '**结果为未计ETF现金分红、份额调整、融资成本与闲置现金利息的原价盈亏诊断，不是已验收的总回报或实际套利净收益。** 不伪造公司行为；疑似ETF/指数单日增长比偏离超过5%的持有周期标记，原结果保留。排除标记周期的事后数值不作为可实现策略收益。日线无法确认开盘成交容量。','',
            '|成本|品种|周期|已平胜率|扣研究成本原价PnL|费用|滑点|异常周期数|',
            '|---|---|---:|---:|---:|---:|---:|---:|']
    for r in totals:
        report.append(f"|{r['scenario']}|{r['product']}|{r['cycles']}|{r['wins']/r['closed_cycles']:.1%}|{r['net_raw_price_pnl']:.2f}|{r['fees']:.2f}|{r['slippage']:.2f}|{r['flagged_cycles']}|")
    report+=['','逐周期盈亏、每腿成交、资金账务、每日权益和期末持仓均已导出。初期共同观察价格不证明上市边界已验收；ETF报价突变仍待独立核对。']
    (out/'report.md').write_text('\n'.join(report),encoding='utf-8')
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig,ax=plt.subplots(figsize=(13,5),constrained_layout=True)
    for product in PRODUCTS:ax.plot([r['day'] for r in base['daily']],[float(r[f'cumulative_pnl_{product}']) for r in base['daily']],label=product)
    ax.axhline(0,color='gray');ax.set_ylabel('Raw-price PnL after assumed costs (CNY)');ax.legend()
    fig.savefig(out/'pnl.png',dpi=150);plt.close(fig)
    print(totals,flush=True)
    return totals
