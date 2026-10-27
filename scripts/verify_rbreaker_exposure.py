"""Independent raw-price replay of every funding observation and entry guard."""
from collections import defaultdict,deque,OrderedDict
import csv
from datetime import datetime,timedelta
from decimal import Decimal as D
import hashlib
import json
from pathlib import Path

import duckdb
import pyarrow.parquet as pq
from backtest.factor_data import load_research_data
from backtest.rbreaker import DAILY,METADATA,MINUTE


def rows(path):
    with Path(path).open(encoding='utf-8-sig',newline='') as handle:
        yield from csv.DictReader(handle)


class RawPrices:
    def __init__(self):
        with duckdb.connect('data/index_futures_minute/metadata.duckdb',read_only=True) as db:
            report=json.loads(db.execute('SELECT report FROM futures_minute_snapshots WHERE version=?',[MINUTE]).fetchone()[0])
        self.items=defaultdict(list);self.files={};self.cache=OrderedDict()
        for item in report['manifest']:self.items[item['instrument']].append(item)

    def get(self,code,stamp):
        stamp=stamp.replace(tzinfo=None)
        key=code,stamp.date()
        if key not in self.files:
            self.files[key]=next(i['path'] for i in self.items[code] if i['first']<=str(stamp)<=i['last'])
        path=self.files[key]
        if path not in self.cache:
            data=pq.read_table(Path('data/index_futures_minute')/path,columns=['source_timestamp','open','close','high','low','volume']).to_pylist()
            self.cache[path]={r['source_timestamp']:r for r in data}
            while len(self.cache)>16:self.cache.popitem(last=False)
        self.cache.move_to_end(path)
        return self.cache[path][stamp]


def run(root):
    root=Path(root)
    summary=json.loads((root/'summary.json').read_text())
    config=summary['config'];assert config['net_exposure']
    _,_,daily_raw,_,inst=load_research_data('data/index_futures',DAILY,METADATA)
    rate=D(config['margin_rate']);prices=RawPrices()
    settlements={(r['instrument'],str(r['trading_date'])):D(r['settlement_price']) for r in daily_raw}
    atr={};history_by_code=defaultdict(list)
    for r in daily_raw:history_by_code[r['instrument']].append(r)
    for code,history in history_by_code.items():
        tr=deque(maxlen=20);previous=None
        for r in sorted(history,key=lambda x:x['trading_date']):
            if len(tr)==20:atr[code,str(r['trading_date'])]=sum(tr,D(0))/20
            high,low,close=(D(r[k]) for k in ('high','low','close'))
            tr.append(high-low if previous is None else max(high-low,abs(high-previous),abs(low-previous)))
            previous=close
    daily=list(rows(root/'daily.csv'));prev={};equity=D(config['initial_cash'])
    for r in daily:prev[r['day']]=equity;equity=D(r['equity'])
    intents={int(r['id']):r for r in rows(root/'logical_batches.csv')}
    components=defaultdict(list)
    routing=list(rows(root/'routing_audit.csv'))
    for r in routing:components[int(r['intent_id'])].append(r)
    seen=set()
    for r in rows(root/'allocation_audit.csv'):
        stamp=datetime.fromisoformat(r['time']);code=r['instrument'];iid=int(r['intent_id'])
        assert (r['time'],code) not in seen;seen.add((r['time'],code))
        assert D(r['reference_equity'])==prev[r['time'][:10]]
        assert D(r['margin_budget'])==D(r['reference_equity'])*D('.05')
        close=D(prices.get(code,stamp)['close'])
        maximum=int(D(r['margin_budget'])/((close+inst[code].tick)*inst[code].multiplier*rate))
        assert int(r['requested_lots'])==maximum and 0<int(r['new_lots'])<=maximum
        assert sum(int(p['quantity']) for p in components[iid])==int(r['new_lots'])
        assert D(r['atr'])==atr[code,r['time'][:10]]
        assert D(r['total_used'])<D(r['equity'])*D('.9')
        assert D(r['product_used'])<D(r['equity'])*D('.5')
        assert D(r['projected_used'])<=(D(r['equity'])-D(r['fee_reserve']))*D('.9')
        assert D(r['projected_product_used'])<=(D(r['equity'])-D(r['fee_reserve']))*D('.5')
    inventory=defaultdict(deque)
    quantity=defaultdict(int);basis_total=defaultdict(D);group_qty=defaultdict(int)
    cash=D(config['initial_cash']);last_marks={}
    volume_used=defaultdict(int)
    logical_basis=defaultdict(lambda:[0,D(0)])
    fill_list=list(rows(root/'fills.csv'));fill_index=0
    guards=list(rows(root/'execution_guard.csv'));guard_index=0
    events=defaultdict(list)
    for e in rows(root/'stop_events.csv'):events[e['trigger_time']].append(e)
    stress=iter(rows(root/'intrabar_stress.csv'));stress_count=0
    breaches=0;count=0;max_ratio=D(0);max_gap=D(0);closed_today=0
    by_day={};by_stage=defaultdict(int);stress_breaches=0
    positive_guards=0;denied=0
    def snapshot(marks):
        value=cash;sides=defaultdict(lambda:{1:D(0),-1:D(0)});net=defaultdict(int)
        for (code,side),q in quantity.items():
            if not q:continue
            mark=marks[code];i=inst[code]
            value+=side*(q*mark-basis_total[code,side])*i.multiplier
            sides[i.product][side]+=q*abs(mark)*i.multiplier*rate
            net[code]+=side*q
        margin=sum((max(v.values()) for v in sides.values()),D(0))
        exposure=defaultdict(D)
        for code,q in net.items():exposure[inst[code].product]+=abs(q)*abs(marks[code])*inst[code].multiplier*rate
        return value,margin,exposure,net
    def opening_marks(at,phase):
        stamp=at if phase=='auction' else at+timedelta(minutes=1)
        result=dict(last_marks)
        for code,side in quantity:
            if quantity[code,side]:result[code]=D(prices.get(code,stamp)['open'])
        return result
    def guard_before(r):
        at=datetime.fromisoformat(r['time']);marks=opening_marks(at,r['phase'])
        code=r['instrument'];source=at if r['phase']=='auction' else at+timedelta(minutes=1)
        marks[code]=D(prices.get(code,source)['open'])
        value,margin,exposure,_=snapshot(marks)
        assert value==D(r['equity_before']) and margin==D(r['margin_before'])
        assert exposure[inst[code].product]==D(r['exposure_before'])
        assert marks[code]==D(r['mark'])
        if r['increasing']=='True':
            assert D(r['batch_budget'])==D(intents[int(r['intent_id'])]['margin_budget'])
        if not int(r['allowed']):
            assert r['reason']!='accepted' and D(r['fee'])==0
        return marks
    for observed in rows(root/'funding_observations.csv'):
        at=datetime.fromisoformat(observed['time']);stage=observed['stage'];phase=observed['phase']
        day=observed['time'][:10]
        accepted=None;fill=None
        if stage=='after_fill':
            while not int(guards[guard_index]['allowed']):
                guard_before(guards[guard_index]);guard_index+=1;denied+=1
            accepted=guards[guard_index];guard_index+=1;positive_guards+=1
            marks=guard_before(accepted)
            fill=fill_list[fill_index];fill_index+=1
            assert fill['time']==observed['time'] and fill['execution_phase']==phase
            assert all(fill[k]==accepted[k] for k in ('time','group','effect','side'))
            assert int(fill['quantity'])==int(accepted['allowed'])
            code=fill['instrument'];side=int(fill['side']);q=int(fill['quantity']);group=fill['group']
            price=D(fill['price']);fee=D(fill['fee']);i=inst[code]
            source=at if phase=='auction' else at+timedelta(minutes=1)
            raw=prices.get(code,source)
            buy=(side==1)==(fill['effect']=='open')
            expected=D(raw['open'])+(D(0) if phase=='auction' else (1 if buy else -1)*i.tick)
            assert price==expected
            volume_used[code,source]+=q
            assert volume_used[code,source]<=int(D(raw['volume'])*D('.1'))
            key=code,side,group
            today=0
            if fill['effect']=='open':
                inventory[key].append([q,price,day,price])
                quantity[code,side]+=q;basis_total[code,side]+=q*price;group_qty[group]+=q
            else:
                left=q
                while left:
                    lot=inventory[key][0];used=min(left,lot[0]);left-=used
                    today+=used if lot[2]==day else 0
                    cash+=side*used*(price-lot[3])*i.multiplier
                    basis_total[code,side]-=used*lot[3]
                    lot[0]-=used
                    if not lot[0]:inventory[key].popleft()
                quantity[code,side]-=q;group_qty[group]-=q
                if not quantity[code,side]:del quantity[code,side]
            expected_fee=q*price*i.multiplier*D('.000023')
            if config['direct_close_control']:expected_fee+=today*price*i.multiplier*(D('.00023')-D('.000023'))
            else:assert today==0
            assert fee==expected_fee
            closed_today+=today;cash-=fee
            marks=opening_marks(at,phase);marks[code]=D(raw['open'])
            if accepted['increasing']=='True':
                iid=int(accepted['intent_id']);n,total=logical_basis[iid]
                logical_basis[iid]=[n+q,total+q*price]
        elif stage=='open':
            marks=opening_marks(at,phase)
        elif stage=='minute_close':
            # Deferred candidates after the last real fill still observe opens.
            while guard_index<len(guards) and guards[guard_index]['frame_time']==observed['time']:
                r=guards[guard_index];assert not int(r['allowed'])
                guard_before(r);guard_index+=1;denied+=1
            marks=dict(last_marks)
            if at.time().isoformat()!='09:25:00':
                for code,side in quantity:
                    if quantity[code,side]:marks[code]=D(prices.get(code,at)['close'])
        elif stage=='settlement':
            marks=dict(last_marks)
            for (code,side),q in list(quantity.items()):
                if not q:continue
                price=settlements[code,day]
                cash+=side*(q*price-basis_total[code,side])*inst[code].multiplier
                basis_total[code,side]=q*price;marks[code]=price
            for (code,side,group),lots in inventory.items():
                for lot in lots:lot[3]=settlements[code,day]
        else:raise AssertionError(stage)
        value,margin,exposure,net=snapshot(marks)
        assert value==D(observed['equity']), (observed['time'],stage,value,observed['equity'])
        assert margin==D(observed['margin'])
        for p in ('IF','IH','IC','IM'):assert exposure[p]==D(observed[p+'_exposure'])
        ratio=margin/value if value>0 else None
        assert ratio==(D(observed['usage']) if observed['usage'] else None)
        breached=value<=0 or margin>value
        assert breached==(observed['breached']=='True')
        breaches+=breached;count+=1;max_ratio=max(max_ratio,ratio or D(0));max_gap=max(max_gap,margin-value)
        stats=by_day.setdefault(day,dict(day=day,max_actual_margin_ratio=D(0),actual_breaches=0,observations=0,
                                         max_product_net_ratio=D(0),max_funding_gap=D(0)))
        stats['max_actual_margin_ratio']=max(stats['max_actual_margin_ratio'],ratio or D(0))
        stats['actual_breaches']+=breached;stats['observations']+=1
        stats['max_product_net_ratio']=max(stats['max_product_net_ratio'],max(exposure.values(),default=D(0))/value if value>0 else D(0))
        stats['max_funding_gap']=max(stats['max_funding_gap'],margin-value)
        by_stage[stage]+=breached
        if accepted:
            assert value==D(accepted['equity_after']) and margin==D(accepted['margin_after'])
            product=inst[accepted['instrument']].product
            assert exposure[product]==D(accepted['exposure_after'])
            assert D(accepted['fee'])==D(fill['fee'])
            if accepted['increasing']=='True':
                assert D(accepted['margin_before'])<D(accepted['equity_before'])*D('.9')
                assert D(accepted['exposure_before'])<D(accepted['equity_before'])*D('.5')
                assert margin<=value*D('.9') and exposure[product]<=value*D('.5')
                iid=int(accepted['intent_id']);intent=intents[iid];owned=0
                for part in components[iid]:
                    owned+=int(intent['side'])*(group_qty[part['group']+'-long']-group_qty[part['group']+'-short'])
                assert owned==int(accepted['logical_quantity_after'])
                unit=max(abs(D(accepted['mark'])),abs(D(fill['price'])))*inst[accepted['instrument']].multiplier*rate
                assert owned*unit<=D(intent['margin_budget'])
        if stage=='minute_close':
            for event in events.get(observed['time'],[]):
                iid=int(event['intent_id']);n,total=logical_basis[iid];average=total/n
                assert abs(average-D(event['entry_average']))<D('1e-20')
                assert D(event['distance'])==D(intents[iid]['atr'])
                code=event['instrument'];quote=prices.get(code,at)
                extreme=D(quote['low'] if int(event['side'])>0 else quote['high'])
                threshold=average-int(event['side'])*D(event['distance'])
                assert abs(threshold-D(event['stop_price']))<D('1e-20')
                assert extreme<=threshold if int(event['side'])>0 else extreme>=threshold
            if at.time().isoformat() not in ('09:25:00','09:30:00'):
                stress_row=next(stress);stress_count+=1;assert stress_row['time']==observed['time']
                low_equity=value;upper_sides=defaultdict(lambda:{1:D(0),-1:D(0)})
                for code,q in net.items():
                    raw=prices.get(code,at);adverse=D(raw['low'] if q>0 else raw['high'] if q<0 else raw['close'])
                    low_equity+=q*inst[code].multiplier*(adverse-marks[code])
                for (code,side),q in quantity.items():
                    if q:upper_sides[inst[code].product][side]+=q*D(prices.get(code,at)['high'])*inst[code].multiplier*rate
                upper_margin=sum((max(v.values()) for v in upper_sides.values()),D(0))
                assert low_equity==D(stress_row['equity_lower']) and upper_margin==D(stress_row['margin_upper'])
                stressed=low_equity<=0 or upper_margin>low_equity
                assert stressed==(stress_row['stressed']=='True')
                stress_breaches+=stressed
        last_marks=marks
    assert fill_index==len(fill_list) and guard_index==len(guards)
    assert count==summary['funding_feasibility']['observations']
    assert breaches==summary['funding_feasibility']['breaches']
    assert max_ratio==D(summary['funding_feasibility']['max_ratio'])
    assert max_gap==D(summary['funding_feasibility']['max_gap'])
    assert ('failed' if breaches else 'passed')==summary['funding_feasibility']['status']
    assert cash==D(summary['equity'])
    for iid,(n,total) in logical_basis.items():
        assert n==int(intents[iid]['acquired_quantity'])
        assert abs(total/n-D(intents[iid]['basis']))<D('1e-20')
    group_cash=defaultdict(D)
    for row in rows(root/'cash_entries.csv'):group_cash[row['group'].rsplit('-',1)[0]]+=D(row['amount'])
    for row in rows(root/'trades.csv'):assert group_cash[row['group']]==D(row['net_pnl'])
    checkpoint=Path('outputs/rbreaker_checkpoint_20261020_auction_atr20')
    manifest=json.loads((checkpoint/'manifest.json').read_text(encoding='utf-8'))
    for item in manifest['files']:assert hashlib.sha256((checkpoint/item['path']).read_bytes()).hexdigest()==item['sha256']
    result=dict(accounting_status='passed',exposure_admission_status='passed',
                funding_feasibility='failed' if breaches else 'passed',observations=count,actual_breaches=breaches,
                max_actual_margin_ratio=str(max_ratio),max_funding_gap=str(max_gap),
                fills=fill_index,guard_deferred=denied,logical_batches=len(intents),routing_components=len(routing),
                same_day_closed_lots=closed_today,intrabar_stress_rows=stress_count,checkpoint_files=len(manifest['files']),
                actual_breaches_by_stage=dict(by_stage),intrabar_stress_possible_breaches=stress_breaches,
                checks=['raw opening/auction prices, adverse ticks, per-frame participation and FIFO today fees',
                        'all opening/after-fill/close/settlement funding observations independently replayed',
                        'every admitted exposure increase obeys batch 5%, product net 50%, actual margin 90%',
                        'decision reference equity and multi-container shared budget',
                        'logical exposure entry/unlock basis and frozen ATR stops','intrabar conservative bound separately replayed',
                        'per-container cash attribution and unchanged checkpoint'])
    (root/'exposure_verification.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    with (root/'funding_daily.csv').open('w',encoding='utf-8-sig',newline='') as handle:
        writer=csv.DictWriter(handle,fieldnames=list(next(iter(by_day.values()))))
        writer.writeheader();writer.writerows(by_day.values())
    print(json.dumps(result,indent=2))


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',default='outputs/rbreaker_lock_net_exposure_5pct_atr20_auction')
    run(parser.parse_args().root)
