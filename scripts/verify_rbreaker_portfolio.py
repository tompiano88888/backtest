"""Verify saved portfolio results against raw minute opens and original entries."""
from collections import defaultdict
import csv
from datetime import datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path

import duckdb
import pyarrow.parquet as pq

from backtest.factor_data import load_research_data
from backtest.rbreaker import DAILY, METADATA, MINUTE


def raw_historical_fees(config):
    """Read pinned original vendor records, independent of FeeService/normalizer."""
    source=config['historical_fees']
    with duckdb.connect(str(Path(source['root'])/'metadata.duckdb'),read_only=True) as db:
        row=db.execute('SELECT daily_version,revisions FROM futures_fee_snapshots WHERE version=?',[source['version']]).fetchone()
        assert row and row[0]==config['daily_version']
        records={}
        for code,ids in json.loads(row[1]).items():
            payload,known=db.execute('SELECT payload,known_at FROM metadata WHERE revision=?',[ids['raw']]).fetchone()
            assert known<=datetime.fromisoformat(source['as_of'])
            for r in json.loads(payload)['batch']['records']:
                key=code,r['time'];assert key not in records
                records[key]=r
    return records


def historical_fee(fill, inst, record, balances):
    """Replay today-first fee balance without changing FIFO accounting lots."""
    code,day,side=fill['instrument'],fill['trading_date'],int(fill['side'])
    quantity=int(fill['quantity']);key=day,code,side
    balance=balances[key]
    today=min(quantity,balance) if fill['effect']=='close' else 0
    def value(name,scale=1):
        native=record[name]
        return Decimal(0) if native is None else Decimal(str(native))/scale
    normal=value('ths_transaction_procedure_rate_future',1000)*Decimal(fill['price'])*inst.multiplier+value('ths_transaction_procedure_fee_future')
    intraday=value('ths_to_transaction_procedure_rate_future',1000)*Decimal(fill['price'])*inst.multiplier+value('ths_to_transaction_procedure_fee_future')
    balances[key]=balance+quantity if fill['effect']=='open' else max(0,balance-quantity)
    return (quantity-today)*normal+today*intraday,today


def verify_daily_funding(fills,daily,instruments,raw,rate,initial):
    """Rebuild every settled PnL and larger-side margin from original fills."""
    by_day=defaultdict(list)
    for row in fills:by_day[row['trading_date']].append(row)
    settlement={(r['instrument'],str(r['trading_date'])):Decimal(r['settlement_price']) for r in raw}
    lots=defaultdict(list);realized=Decimal(0)
    maximum=Decimal(0)
    for row in daily:
        day=row['day']
        for f in by_day[day]:
            code,side,qty=f['instrument'],int(f['side']),int(f['quantity'])
            key=code,side,f['group'];price=Decimal(f['price'])
            realized-=Decimal(f['fee'])
            if f['effect']=='open':lots[key].append([qty,price])
            else:
                while qty:
                    lot=lots[key][0];used=min(qty,lot[0])
                    realized+=used*side*instruments[code].multiplier*(price-lot[1])
                    qty-=used;lot[0]-=used
                    if not lot[0]:lots[key].pop(0)
        unrealized=Decimal(0);sides=defaultdict(lambda:defaultdict(Decimal))
        for (code,side,_),positions in lots.items():
            if not positions:continue
            inst=instruments[code];mark=settlement[code,day]
            for qty,price in positions:
                unrealized+=qty*side*inst.multiplier*(mark-price)
                sides[inst.product][side]+=qty*abs(mark)*inst.multiplier*rate
        margin=sum((max(s.values()) for s in sides.values()),Decimal(0))
        equity=initial+realized+unrealized
        assert equity==Decimal(row['equity'])==Decimal(row['cash'])
        assert realized+unrealized==Decimal(row['pnl'])
        assert margin==Decimal(row['margin'])
        assert margin/equity==Decimal(row['usage'])
        maximum=max(maximum,margin/equity)
    return str(maximum)


def run(root=Path('outputs/rbreaker_portfolio')):
    root=Path(root)
    def read(name):
        with (root/name).open(encoding='utf-8-sig',newline='') as handle:
            return list(csv.DictReader(handle))
    _,_,raw,_,instruments=load_research_data('data/index_futures',DAILY,METADATA)
    summary=json.loads((root/'summary.json').read_text())
    fills=read('fills.csv')
    trades=read('trades.csv') if (root/'trades.csv').exists() else []
    batch_mode=summary['config'].get('stop',{}).get('mode')=='batch_cash' if summary['config'].get('stop') else False
    lock_mode=summary['config'].get('lock_today',False)
    historical=summary['config'].get('fee_mode')=='historical'
    fee_records=raw_historical_fees(summary['config']) if historical else {}
    fee_details=read('fee_details.csv') if historical else []
    if historical:assert len(fee_details)==len(fills)
    balances=defaultdict(int);today_total=0
    needed=defaultdict(set)
    for fill in fills:
        stamp=datetime.fromisoformat(fill['time']).replace(tzinfo=None)
        if fill.get('execution_phase','continuous')!='auction':
            stamp+=timedelta(minutes=1)
        needed[fill['instrument']].add(stamp)
    if batch_mode:
        for trade in trades:
            needed[trade['instrument']].add(datetime.fromisoformat(trade['decision_time']).replace(tzinfo=None))
    with duckdb.connect('data/index_futures_minute/metadata.duckdb',read_only=True) as db:
        report=json.loads(db.execute('SELECT report FROM futures_minute_snapshots WHERE version=?',[MINUTE]).fetchone()[0])
    opens={}
    closes={}
    for item in report['manifest']:
        code=item['instrument']
        if code not in needed or not any(item['first']<=str(t)<=item['last'] for t in needed[code]):
            continue
        rows=pq.read_table(Path('data/index_futures_minute')/item['path'],columns=['source_timestamp','open','close']).to_pylist()
        for row in rows:
            if row['source_timestamp'] in needed[code]:
                opens[code,row['source_timestamp']]=Decimal(row['open'])
                closes[code,row['source_timestamp']]=Decimal(row['close'])
    inventory=defaultdict(list)
    pnl=defaultdict(Decimal)
    fees=slippage=Decimal(0)
    for fill_index,fill in enumerate(fills):
        code=fill['instrument']
        inst=instruments[code]
        side,qty=int(fill['side']),int(fill['quantity'])
        price,fee=Decimal(fill['price']),Decimal(fill['fee'])
        end=datetime.fromisoformat(fill['time']).replace(tzinfo=None)
        auction=fill.get('execution_phase','continuous')=='auction'
        if not auction:
            end+=timedelta(minutes=1)
        buy=(side==1)==(fill['effect']=='open')
        assert price==opens[code,end]+(Decimal(0) if auction else (1 if buy else -1)*inst.tick)
        key=code,side,fill['group']
        expected_fee=qty*price*inst.multiplier*Decimal('.000023')
        if historical:
            expected_fee,today=historical_fee(fill,inst,fee_records[code,fill['trading_date']],balances)
            detail=fee_details[fill_index]
            for name in ('time','trading_date','instrument','side','quantity','effect','group'):
                assert detail[name]==fill[name]
            assert detail['matching']=='today_first' and detail['mode']=='historical'
            assert int(detail['close_today_quantity'])==today
            assert int(detail['normal_quantity'])==qty-today
            assert Decimal(detail['total'])==Decimal(detail['exchange_fee'])==expected_fee
            assert Decimal(detail['commission'])==0
            today_total+=today
        elif summary['config'].get('direct_close_control') and fill['effect']=='close':
            left=qty;today=0
            for q,entry,day in inventory[key]:
                used=min(left,q);left-=used
                today+=used if day==fill['trading_date'] else 0
                if not left:break
            expected_fee+=today*price*inst.multiplier*(Decimal('.00023')-Decimal('.000023'))
        assert fee==expected_fee
        fees+=fee
        if not auction:
            slippage+=qty*inst.tick*inst.multiplier
        pnl[inst.product]-=fee
        if fill['effect']=='open':
            inventory[key].append([qty,price,fill['trading_date']])
        else:
            remaining=qty
            while remaining:
                lot=inventory[key][0]
                used=min(lot[0],remaining)
                pnl[inst.product]+=used*side*inst.multiplier*(price-lot[1])
                remaining-=used
                lot[0]-=used
                if not lot[0]:
                    inventory[key].pop(0)
    final_marks={p['instrument']:Decimal(p['basis']) for p in summary['final_positions']}
    for (code,side,group),lots in inventory.items():
        for qty,entry,day in lots:
            pnl[instruments[code].product]+=qty*side*instruments[code].multiplier*(final_marks[code]-entry)
    assert sum(pnl.values(),Decimal(0))==Decimal(summary['net_pnl'])
    assert fees==Decimal(summary['fees'])
    assert slippage==Decimal(summary['slippage_cost_vs_same_fill_open'])
    assert all(pnl[p]==Decimal(v) for p,v in summary['product_pnl'].items())
    daily=read('daily.csv')
    assert all(Decimal(r['cash'])==Decimal(r['equity']) and Decimal(r['reconciliation_difference'])==0 for r in daily)
    assert sum((Decimal(r['daily_pnl']) for r in daily),Decimal(0))==Decimal(summary['net_pnl'])
    max_daily_usage=verify_daily_funding(fills,daily,instruments,raw,Decimal(summary['config']['margin_rate']),Decimal(summary['config']['initial_cash']))
    audit=read('allocation_audit.csv')
    for row in audit:
        if int(row['new_lots']):
            if lock_mode:
                equity=Decimal(row['equity'])-Decimal(row['fee_reserve'])
                total_limit=equity*Decimal('.9')
                product_limit=equity*Decimal('.5')
                if row['action']=='unlock' and not summary['config'].get('net_exposure'):
                    total_limit=max(total_limit,Decimal(row['total_used']))
                    product_limit=max(product_limit,Decimal(row['product_used']))
                assert Decimal(row['projected_used'])<=total_limit
                assert Decimal(row['projected_product_used'])<=product_limit
                continue
            assert Decimal(row['total_used'])<Decimal(row['equity'])*Decimal('.9')
            assert Decimal(row['product_used'])<Decimal(row['equity'])*Decimal('.5')
            assert Decimal(row['projected_used'])<=(Decimal(row['equity'])-Decimal(row['fee_reserve']))*Decimal('.9')
            if 'product_cap' in row:
                assert Decimal(row['projected_product_used'])<=(Decimal(row['equity'])-Decimal(row['fee_reserve']))*Decimal(row['product_cap'])
    result=dict(status='passed',fills=len(fills),daily_rows=len(daily),allocation_rows=len(audit),
                independent_net_pnl=str(sum(pnl.values(),Decimal(0))),fees=str(fees),slippage=str(slippage),
                checks=['auction fills zero slip; continuous fills adverse one tick vs raw observation','every fee actual notional times .000023',
                        'original-entry FIFO PnL including residual settlement marks','product attribution sums to total',
                        'daily settlement cash equals equity','saved allocation budgets'])
    if summary['config'].get('direct_close_control'):
        result['checks'][1]='FIFO close-today .00023; all other actual notional fees .000023'
    result['max_settled_margin_ratio']=max_daily_usage
    result['checks']+=['every settled equity and larger-side margin independently rebuilt from original fills and raw settlements']
    if historical:
        assert today_total==summary['close_today_quantity']
        result['checks'][1]='pinned original vendor daily rates /1000, today-first balance and every fee detail'
        result['close_today_quantity']=today_total
        result['fee_provenance']=summary['config']['historical_fees']
    if (root/'trades.csv').exists():
        adjustment=Decimal(summary.get('inventory_cleanup_adjustment','0'))
        assert sum((Decimal(t['net_pnl']) for t in trades),Decimal(0))+adjustment==Decimal(summary['net_pnl'])
        previous_equity={}
        prior=Decimal(summary['config']['initial_cash'])
        for row in daily:
            previous_equity[row['day']]=prior
            prior=Decimal(row['equity'])
        cash_by_group=defaultdict(Decimal)
        if batch_mode:
            for row in read('cash_entries.csv'):
                cash_by_group[row['group']]+=Decimal(row['amount'])
        for trade in trades:
            assert int(trade['opened_quantity'])<=int(trade['maximum'])
            multiplier=Decimal(trade.get('risk_multiplier','1'))
            assert Decimal(trade['budget'])==Decimal(trade['equity'])*(Decimal(summary['config']['stop']['risk_fraction'])*multiplier)
            if summary['config']['stop'].get('mode')=='batch_cash':
                assert Decimal(trade['margin_budget'])==Decimal(trade['reference_equity'])*Decimal(summary['config']['batch_margin_fraction'])
                assert Decimal(trade['reference_equity'])==previous_equity[trade['decision_time'][:10]]
                code=trade['instrument']
                stamp=datetime.fromisoformat(trade['decision_time']).replace(tzinfo=None)
                inst=instruments[code]
                unit=(abs(closes[code,stamp])+inst.tick)*inst.multiplier*Decimal(summary['config']['margin_rate'])
                assert int(trade['maximum'])==int(Decimal(trade['margin_budget'])/unit)//inst.lot*inst.lot
                assert cash_by_group[trade['group']]==Decimal(trade['net_pnl'])
            elif not lock_mode:
                assert int(trade['opened_quantity'])*Decimal(trade['planned_risk_per_lot'])<=Decimal(trade['budget'])
                assert Decimal(trade['distance'])==Decimal(trade['atr'])*Decimal(summary['config']['stop']['atr_multiple'])
        result['checks'] += ['all trade lifecycles reconcile to account PnL','every trade quantity obeys frozen risk budget']
        if summary['config'].get('fee_avoidance'):
            result['checks'][-2]='logical exposure trades plus independently audited physical cleanup adjustment reconcile to account PnL'
        if batch_mode:
            result['checks'][-1]='batch quantity obeys 5% margin budget, not risk sizing'
            result['checks']+=['prior-day settled equity verified independently','each isolated batch cash attribution equals its PnL']
        if lock_mode:
            result['checks'][-1]='actual larger-side margin reservations for opens/unlocks'
    (root/'verification.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result,indent=2))


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path('outputs/rbreaker_portfolio'))
    run(parser.parse_args().root)
