"""Independent raw-price/FIFO/calendar-cash reconciliation; never invokes Engine."""
from bisect import bisect_right
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
import argparse
import csv
import json

from backtest.cash_futures_basis import PAIRS,load_spot
from backtest.factor_data import load_research_data
from backtest.front_contract_arbitrage import front_map
from backtest.models import D
from backtest.research_dividends import load_events


def read(path):
    with path.open(encoding='utf-8-sig',newline='') as stream:
        return list(csv.DictReader(stream))


def run(root):
    saved = json.loads((root/'result.json').read_text('utf-8'))
    config = json.loads((root/'config.json').read_text('utf-8'))['node_config']
    v = config['source_snapshots']
    _,_,raw,facts,inst = load_research_data('data/index_futures',v['daily_version'],v['metadata_version'])
    calendars={p:sorted({r['trading_date'] for r in raw if r['product']==p}) for p in PAIRS}
    mapping=front_map(inst,{c:date.fromisoformat(f['first_trade_date']) for c,f in facts.items()},calendars)
    _,spots = load_spot(version=v['spot_version'])
    quotes = {(r['instrument'],r['trading_date']):r for r in raw+spots}
    daily = read(root/'daily.csv')
    market = sorted(date.fromisoformat(r['day']) for r in daily if r['is_market_day']=='True')
    fills = defaultdict(list)
    volume = defaultdict(int)
    audit = read(root/'decisions.csv')
    opens = {r['group']:r for r in audit if r['action']=='open'}
    closes = defaultdict(list)
    operations={op['id']:op for op in saved['operations']}
    timeouts=defaultdict(list)
    for warning in saved['warnings']:
        if warning['code']=='COMBINATION_TIMEOUT':
            op=operations[warning['operation']]
            for leg in op['filled']+op['legs']:
                timeouts[leg['group']].append(datetime.fromisoformat(warning['time']).date())
    for r in audit:
        if r['action']=='close':
            closes[r['group']].append(date.fromisoformat(r['day']))
    for fill in saved['fills']:
        day = date.fromisoformat(fill['trading_date'])
        leg = fill['instruction'];code=leg['instrument'];q=leg['quantity']
        quote = quotes[code,day]
        future = code in inst
        buy = (leg['side']==1)==(leg['effect']=='open')
        expected = quote['open']+((1 if buy else -1)*inst[code].tick if future else D(0))
        assert D(fill['price'])==expected and quote['low']<=expected<=quote['high']
        assert datetime.fromisoformat(fill['time']).time().isoformat()=='09:30:00'
        assert day>date.fromisoformat(opens[leg['group']]['day'])
        if leg['effect']=='close':
            assert any(d<day for d in closes[leg['group']]+timeouts[leg['group']]), fill
        else:
            future_code=opens[leg['group']]['future']
            product=opens[leg['group']]['product']
            assert day<inst[future_code].expiry and mapping[product,day]==future_code
        if leg['effect']=='open' and not future:
            assert q%100==0
            assert q<=int(opens[leg['group']]['quantity'])
        assert D(fill['fee'])==0
        volume[code,day]+=q
        assert volume[code,day]<=int(D('.1')*quote.get('volume',quote.get('source_volume')))
        fills[day].append(fill)
    events = load_events(config['dividend_version'])
    lots=[];claims=[];locks=[];cash=D(config['initial']);margin_rate=D(config['margin_rate']);last_close={}
    observed_claims=[];interest=D(0)
    for row in daily:
        day=date.fromisoformat(row['day'])
        for _,code in PAIRS.values():
            if (code,day) in quotes:
                last_close[code]=quotes[code,day]['close']
        locks=[lock for lock in locks if lock['release'] is None or lock['release']>day]
        for claim in claims:
            if claim['payment_date']==day:
                cash+=claim['amount']
        for fill in fills[day]:
            leg=fill['instruction'];code=leg['instrument'];q=leg['quantity'];price=D(fill['price'])
            multiplier=inst[code].multiplier if code in inst else D(1)
            if leg['effect']=='open':
                if code not in inst:
                    cash-=q*price
                lots.append(dict(code=code,group=leg['group'],side=leg['side'],q=q,basis=price,day=day))
            else:
                left=q
                for lot in lots:
                    if (lot['code'],lot['group'],lot['side'])!=(code,leg['group'],leg['side']) or left==0:
                        continue
                    count=min(left,lot['q'])
                    if code in inst:
                        cash+=count*lot['side']*(price-lot['basis'])*multiplier
                    else:
                        assert day>lot['day'], 'ETF T1 selling'
                        cash+=count*price
                    lot['q']-=count;left-=count
                assert left==0
                lots=[lot for lot in lots if lot['q']]
                n=bisect_right(market,day)
                locks.append(dict(release=market[n] if n<len(market) else None,
                                  amount=q*price*multiplier*(margin_rate if code in inst else 1)))
        if row['is_market_day']=='True':
            for lot in lots:
                code=lot['code']
                if code in inst:
                    settlement=quotes[code,day]['settlement_price']
                    cash+=lot['q']*lot['side']*(settlement-lot['basis'])*inst[code].multiplier
                    lot['basis']=settlement
                    if inst[code].delivery==day:
                        lot['q']=0
            lots=[lot for lot in lots if lot['q']]
        for event in events:
            if event['record_date']!=day:
                continue
            for lot in lots:
                if lot['code']==event['instrument'] and lot['side']==1:
                    claim=dict(group=lot['group'],instrument=lot['code'],q=lot['q'],
                               amount=lot['q']*event['cash_per_unit'],
                               ex_date=event['exchange_ex_date'],payment_date=event['payment_date'])
                    claims.append(claim);observed_claims.append(claim)
        margin=sum((lot['q']*lot['basis']*inst[lot['code']].multiplier*margin_rate for lot in lots if lot['code'] in inst),D(0))
        locked=sum((r['amount'] for r in locks),D(0))
        amount=(max(cash-margin-locked,D(0))*(D('.02') if day.year==2020 else D('.015'))/365).quantize(D('.00000001'))
        cash+=amount;interest+=amount
        value=D(0)
        for lot in lots:
            code=lot['code']
            if code not in inst:
                price=last_close[code]
                value+=lot['q']*price
        receivable=sum((c['amount'] for c in claims if c['ex_date']<=day<c['payment_date']),D(0))
        assert cash==D(row['cash']), (day,'cash',cash,row['cash'])
        assert margin==D(row['margin']) and locked==D(row['locked_T1']),day
        assert cash+value+receivable==D(row['equity']), (day,'equity')
    assert cash+value+receivable-D(config['initial'])==D(saved['final']['pnl'])
    assert interest==D(saved['income']['idle_cash_income'])
    assert sum((D(v) for v in saved['group_pnl'].values()),D(0))==D(saved['final']['pnl'])
    assert len(observed_claims)==len(saved['accounting']['claims'])
    assert sum((c['amount'] for c in claims if c['ex_date']<=day),D(0))==D(saved['income']['dividend_gross'])
    check=dict(status='passed',fills=len(saved['fills']),calendar_days=len(daily),
               market_days=len(market),dividend_claims=len(claims),net_pnl=saved['final']['pnl'],
               raw_open_slippage_volume=True,decision_before_fill=True,ETF_T1=True,
               independent_FIFO_daily_settlement_cash_equity_dividends_and_reserved_interest=True,
               group_attribution=True)
    (root/'verification.json').write_text(json.dumps(check,indent=2),'utf-8')
    print(json.dumps(check,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path('outputs/arbitrage_node_engine_20261022'))
    run(parser.parse_args().root)
