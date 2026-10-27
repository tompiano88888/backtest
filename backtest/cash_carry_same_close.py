"""Explicit non-executable same-close signal/fill research, recent fee-covered interval."""
from datetime import date
from pathlib import Path
from dataclasses import asdict
import hashlib
import json

from .accounting import Ledger
from .cash_futures_basis import load_spot,PAIRS
from .cash_carry_signals import annualized_rate,stop_threshold
from .factor_data import load_research_data
from .expiry_spread_research import observations
from .factor_research import csv_if,dump
from .models import D,Instrument,Asset,Account,Instruction,Fill

PRODUCTS=('IF','IH','IM')
START=date(2022,7,22)


def exchange_fee(asset,price,quantity,multiplier):
    rate=D('.000023') if asset==Asset.FUTURE else D('.00004')
    return price*quantity*multiplier*rate


def simulate_close(features,prices,spot,instruments,days,start=START,stops=True):
    instruments=dict(instruments)
    indexed={(r['instrument'],r['trading_date']):r for r in spot}
    for product in PRODUCTS:
        code=PAIRS[product][1]
        instruments[code]=Instrument(code,Asset.FUND,Account.SECURITY,'SSE',product,D(1),D('.001'),100,sell_delay_days=1)
    grouped={(r['product'],r['day']):r for r in features}
    sums={p:D(0) for p in PRODUCTS};counts={p:0 for p in PRODUCTS}
    held={};closed=[];signals=[];daily=[];ledger=Ledger(instruments,'10000000');serial=0
    def trade(code,side,quantity,effect,group,day,raw):
        inst=instruments[code];order=Instruction(code,side,quantity,effect,group)
        fill_price=raw+(inst.tick if order.buy else -inst.tick)
        ledger.apply(Fill(day,day,order,fill_price,exchange_fee(inst.asset,fill_price,quantity,inst.multiplier)))
        return fill_price
    for day in days:
        for product in PRODUCTS:
            row=grouped.get((product,day))
            if row is None:continue
            near=row['near'];index,etf=PAIRS[product]
            f=prices.get((near,day));s=indexed.get((index,day));e=indexed.get((etf,day))
            inst=instruments.get(near)
            dte=(inst.expiry-day).days if inst else None
            rate=annualized_rate(f['close'] if f else None,s['close'] if s else None,dte or 0)
            mean=sums[product]/counts[product] if counts[product]>=252 else None
            decision='warmup' if mean is None else 'flat';mark_ratio=None;level=None;exited=False
            if day>=start and product in held:
                cycle=held[product];old=prices.get((cycle['future'],day));old_inst=instruments[cycle['future']]
                left=(old_inst.expiry-day).days
                if not old or not e or e.get('close') is None:raise ValueError('Missing held closing valuation')
                mark=(cycle['entry_future']-old['close'])*old_inst.multiplier+cycle['shares']*(e['close']-cycle['entry_etf'])-cycle['entry_fees']
                mark_ratio=mark/cycle['notional'];level=stop_threshold(left)
                old_rate=annualized_rate(old['close'],s['close'] if s else None,left)
                reason='expiry' if left==0 else 'stop_loss' if stops and mark_ratio<=-level else 'historical_mean' if old_rate is not None and mean is not None and old_rate<=mean else None
                if cycle.get('pending_exit') and left>0:reason=cycle['pending_exit']
                if reason:
                    eligible=e['ohlcv_complete'] and e.get('source_volume') is not None and e['source_volume']>0 and cycle['entry_day']<day
                    if not eligible:
                        cycle['pending_exit']=reason;decision='retry_exit'
                    else:
                        if reason!='expiry':trade(cycle['future'],-1,1,'close',cycle['id'],day,old['close'])
                        trade(etf,1,cycle['shares'],'close',cycle['id'],day,e['close'])
                        cycle.update(exit_day=day,exit_reason=reason,exit_mark_ratio=mark_ratio)
                        if reason=='expiry':
                            fee=old['settlement_price']*old_inst.multiplier*D('.0001')
                            ledger._cash(-fee,'delivery_fee',cycle['future'],cycle['id'],day)
                        closed.append(cycle);del held[product];exited=True;decision='exit_'+reason
                else:decision='hold'
            if day>=start and product not in held and not exited and mean is not None and rate is not None and rate>mean:
                if dte<5 and rate<0:decision='filtered_near_expiry_negative'
                elif dte<=0:decision='no_expiry_entry'
                elif not e or not e['ohlcv_complete'] or e.get('source_volume') is None or e['source_volume']<=0:decision='entry_quote_ineligible'
                else:
                    shares=int(s['close']*inst.multiplier/e['close']/100)*100
                    if shares<=0:raise ValueError('Invalid ETF quantity')
                    serial+=1;group=f'{product}_{serial}';first=len(ledger.fills)
                    fp=trade(near,-1,1,'open',group,day,f['close'])
                    ep=trade(etf,1,shares,'open',group,day,e['close'])
                    held[product]=dict(id=group,product=product,future=near,etf=etf,entry_day=day,
                        entry_rate=rate,entry_mean=mean,entry_future=fp,entry_etf=ep,shares=shares,
                        notional=shares*e['close'],entry_fees=sum((q.fee for q in ledger.fills[first:]),D(0)),
                        exit_day=None,exit_reason=None,exit_mark_ratio=None,pending_exit=None)
                    decision='entry_same_close'
            if day>=start:
                signals.append(dict(product=product,day=day,future=near,dte=dte,rate=rate,mean=mean,
                                    reference_count=counts[product],decision=decision,mark_ratio=mark_ratio,stop_level=level))
            if rate is not None:sums[product]+=rate;counts[product]+=1
        if day<start:continue
        for code in sorted({p.instrument for p in ledger.lots}):
            if instruments[code].asset==Asset.FUTURE:
                raw=prices.get((code,day))
                if not raw or raw.get('settlement_price') is None:raise ValueError('Missing futures settlement')
                ledger.settle(code,day,raw['settlement_price'])
            else:
                raw=indexed.get((code,day))
                if not raw:raise ValueError('Missing ETF close')
                ledger.marks[code]=raw['close']
        attribution=ledger.group_pnl()
        record=dict(day=day,cash=ledger.cash,equity=D(10000000)+sum(attribution.values(),D(0)))
        for product in PRODUCTS:record[f'pnl_{product}']=sum((v for k,v in attribution.items() if k.startswith(product+'_')),D(0))
        daily.append(record)
    for cycle in held.values():cycle['exit_reason']='open_at_end';closed.append(cycle)
    attribution=ledger.group_pnl();details=[]
    for cycle in closed:
        row={k:v for k,v in cycle.items() if k!='pending_exit'}
        fills=[f for f in ledger.fills if f.instruction.group==cycle['id']]
        fee=sum((f.fee for f in fills),D(0))+sum((D(e['amount'])*-1 for e in ledger.entries if e['group']==cycle['id'] and e['kind']=='delivery_fee'),D(0))
        row.update(pnl=attribution[cycle['id']],fees=fee)
        details.append(row)
    return dict(daily=daily,cycles=details,signals=signals,entries=ledger.entries,
                fills=[dict(day=f.trading_date,**asdict(f.instruction),price=f.price,fee=f.fee) for f in ledger.fills],
                terminal_positions=[asdict(p) for p in ledger.lots])


def run(output='outputs/index_futures_cash_carry_same_close'):
    out=Path(output);out.mkdir(parents=True,exist_ok=True)
    source=json.loads(Path('outputs/index_futures_cash_carry_signals/config.json').read_text(encoding='utf-8'))
    _,_,raw,facts,instruments=load_research_data('data/index_futures',source['daily_version'],source['metadata_version'])
    _,spot=load_spot(version=source['spot_version'])
    features,prices,calendars=observations(raw,facts,instruments);days=sorted({r['trading_date'] for r in raw})
    totals=[]
    for variant,stops in [('filter',False),('filter_stop',True)]:
        result=simulate_close(features,prices,spot,instruments,days,stops=stops)
        for kind in ('daily','cycles','signals','entries','fills'):csv_if(out/f'{variant}_{kind}.csv',result[kind])
        dump(out/f'{variant}_terminal_positions.json',result['terminal_positions'])
        for product in PRODUCTS:
            group=[r for r in result['cycles'] if r['product']==product]
            totals.append(dict(variant=variant,product=product,cycles=len(group),pnl=sum((r['pnl'] for r in group),D(0)),
                                fees=sum((r['fees'] for r in group),D(0)),stop_exits=sum(r['exit_reason']=='stop_loss' for r in group)))
    csv_if(out/'summary.csv',totals)
    dump(out/'config.json',dict(source_snapshots=source,start=str(START),excluded=['IC'],
        fill_timing='same close as signal; idealized non-executable hypothesis',slippage='adverse one tick',
        future_normal_fee='.000023',ETF_fee='.00004',future_delivery_fee='.0001',
        sources=['https://www.sse.com.cn/aboutus/mediacenter/hotandd/c/c_20210630_5508652.shtml',
                 'https://www.huajinqh.com/UserFiles/upload/file/20220314/手续费标准表20220315.pdf',
                 'https://www.cffex.com.cn/u/cms/www/202003/27161736r3ck.pdf'],
        limitations='Historical fee changes/waivers and order declaration charges not fully audited; no ETF dividends/actions; ETF rules assumptions',
        source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()))
    lines=['# 同收盘期现研究','',
        '区间2022-07-22至数据末日。IF/IH/IM，排除IC；此前历史仍用于均值预热。主配置为过滤＋期限止损，另列仅过滤对照。',
        '当日收盘生成信号、当日收盘成交；买入+1tick、卖出−1tick。一手期货空仓，ETF按当日指数名义对冲份额向下取100。属于理想化不可保证执行的同收盘假设。',
        '普通期货每腿每次成交金额×0.000023；ETF经手费×0.00004；到期未平期货另计交割金额×0.0001。无佣金加点、无印花税或非交易所项目。ETF T+1，因此新开仓日不同时退出；不产生期货平今仓。历史申报费/减免与全历史费率仍未完整核实，不能称费用全覆盖。',
        '止损用收盘价估值，包含已发生开仓费用与滑点，阈值/过滤沿用已确认规则。期货日终按结算价记账；到期现金结清，ETF收盘−1tick退出；期末估值。',
        '未计ETF分红、份额调整与融资成本；未补历史交易参数。本次是原价研究诊断，不是总回报或实际可执行净收益。早期全历史净手续费仍未核实，未套用现行费率回填。','',
        '|规则|品种|周期数|止损退出|原价PnL（元）|费用（元）|','|---|---|---:|---:|---:|---:|']
    for r in totals:lines.append(f"|{r['variant']}|{r['product']}|{r['cycles']}|{r['stop_exits']}|{r['pnl']:.2f}|{r['fees']:.2f}|")
    (out/'report.md').write_text('\n'.join(lines),encoding='utf-8');print(totals,flush=True)
    return totals
