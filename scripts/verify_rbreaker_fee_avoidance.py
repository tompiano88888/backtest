"""Independent actual inventory/funding and logical exposure routing audit."""
from collections import defaultdict,deque
from datetime import datetime,timedelta
from decimal import Decimal as D
import argparse
import hashlib
import json
from pathlib import Path

from backtest.factor_data import load_research_data
from backtest.rbreaker import DAILY,METADATA
from scripts.verify_rbreaker_exposure import RawPrices,rows
from scripts.verify_rbreaker_portfolio import run as verify_account
from scripts.summarize_rbreaker_stops import write


def run(root):
    root=Path(root)
    verify_account(root)
    summary=json.loads((root/'summary.json').read_text());assert summary['config']['fee_avoidance']
    _,_,raw,_,inst=load_research_data('data/index_futures',DAILY,METADATA)
    rate=D(summary['config']['margin_rate']);prices=RawPrices()
    settlements={(r['instrument'],str(r['trading_date'])):D(r['settlement_price']) for r in raw}
    inventory=defaultdict(deque);qty=defaultdict(int);basis=defaultdict(D)
    cash=D(summary['config']['initial_cash']);last_marks={}
    fills=list(rows(root/'fills.csv'));routes=list(rows(root/'routing_fills.csv'))
    guards=list(rows(root/'execution_guard.csv'));details=list(rows(root/'fee_details.csv'))
    assert len(fills)==len(routes)==len(details)
    fi=gi=observations=breaches=deferred=0
    max_ratio=max_gap=D(0);volume=defaultdict(int);today=defaultdict(int)
    daily={};cleanup_cash=D(0);cleanup_net=defaultdict(int)
    logical=defaultdict(deque);logical_realized=logical_fees=D(0)
    action_counts=defaultdict(int)
    def snapshot(marks):
        value=cash;sides=defaultdict(lambda:defaultdict(D));net=defaultdict(int)
        for (code,side),count in qty.items():
            if not count:continue
            mark=marks[code];i=inst[code]
            value+=side*(count*mark-basis[code,side])*i.multiplier
            sides[i.product][side]+=count*abs(mark)*i.multiplier*rate
            net[code]+=side*count
        margin=sum((max(s.values()) for s in sides.values()),D(0));exposure=defaultdict(D)
        for code,count in net.items():exposure[inst[code].product]+=abs(count)*abs(marks[code])*inst[code].multiplier*rate
        return value,margin,exposure,net
    def open_marks(at,phase,extra=None):
        source=at if phase=='auction' else at+timedelta(minutes=1)
        marks=dict(last_marks)
        codes={c for (c,s),n in qty.items() if n}
        if extra:codes.add(extra)
        for code in codes:marks[code]=D(prices.get(code,source)['open'])
        return marks
    def guard_before(g):
        at=datetime.fromisoformat(g['time']);marks=open_marks(at,g['phase'],g['instrument'])
        value,margin,_,_=snapshot(marks)
        assert value==D(g['equity_before']) and margin==D(g['margin_before'])
        if not int(g['allowed']):assert g['reason']!='accepted' and D(g['fee'])==0
        return marks
    for obs in rows(root/'funding_observations.csv'):
        at=datetime.fromisoformat(obs['time']);day=obs['time'][:10];stage=obs['stage'];phase=obs['phase']
        accepted=None
        if stage=='after_fill':
            while not int(guards[gi]['allowed']):guard_before(guards[gi]);gi+=1;deferred+=1
            accepted=guards[gi];gi+=1;marks=guard_before(accepted)
            f,r,d=fills[fi],routes[fi],details[fi]
            assert int(r['fill_index'])==fi
            assert all(f[k]==accepted[k] for k in ('time','instrument','group','side','effect'))
            assert f['time']==obs['time'] and f['execution_phase']==phase
            assert all(f[k]==r[k] for k in ('time','instrument','group','quantity','price','fee'))
            assert f['side']==r['physical_side'] and f['effect']==r['physical_effect']
            code=f['instrument'];side=int(f['side']);q=int(f['quantity']);price=D(f['price']);fee=D(f['fee']);i=inst[code]
            assert q==int(accepted['allowed']) and fee==D(accepted['fee'])
            source=at if phase=='auction' else at+timedelta(minutes=1)
            quote=prices.get(code,source)
            volume[code,source]+=q
            assert volume[code,source]<=int(D(quote['volume'])*D('.1'))
            if phase!='auction':assert D(quote['low'])<=price<=D(quote['high'])
            action=r['action'];logical_side=int(r['logical_side'])
            assert action==accepted['action'];action_counts[action]+=q
            assert D(r['today_balance_before'])==today[day,code,side]
            if f['effect']=='close':assert today[day,code,side]==0
            else:today[day,code,side]+=q
            assert int(d['close_today_quantity'])==0
            if action=='enter_unlock':
                assert f['effect']=='close' and side==-logical_side
                assert min(qty[code,1],qty[code,-1])>=q
            elif action=='enter_open':assert f['effect']=='open' and side==logical_side
            elif action=='exit_close':assert f['effect']=='close' and side==logical_side
            elif action=='exit_hedge':assert f['effect']=='open' and side==-logical_side
            else:assert action=='cleanup' and f['effect']=='close'
            key=code,side,f['group']
            if f['effect']=='open':
                inventory[key].append([q,price,day,price]);qty[code,side]+=q;basis[code,side]+=q*price
            else:
                left=q
                while left:
                    lot=inventory[key][0];used=min(left,lot[0]);left-=used
                    assert lot[2]<day
                    cash+=side*used*(price-lot[3])*i.multiplier;basis[code,side]-=used*lot[3]
                    lot[0]-=used
                    if not lot[0]:inventory[key].popleft()
                qty[code,side]-=q
            cash-=fee
            if action=='cleanup':
                delta=-side*q;cleanup_net[code]+=delta;cleanup_cash-=delta*price*i.multiplier+fee
            elif action.startswith('enter_'):
                logical[code,logical_side].append([q,price]);logical_fees+=fee
            else:
                left=q;logical_fees+=fee
                while left:
                    lot=logical[code,logical_side][0];used=min(left,lot[0]);left-=used
                    logical_realized+=used*logical_side*(price-lot[1])*i.multiplier
                    lot[0]-=used
                    if not lot[0]:logical[code,logical_side].popleft()
            fi+=1
            marks=open_marks(at,phase,code)
        elif stage=='open':marks=open_marks(at,phase)
        elif stage=='minute_close':
            while gi<len(guards):
                g=guards[gi];stamp=datetime.fromisoformat(g['time'])+(timedelta(minutes=1) if g['phase']=='continuous' else timedelta())
                if stamp!=at:break
                assert not int(g['allowed']);guard_before(g);gi+=1;deferred+=1
            marks=dict(last_marks)
            if at.hour!=9 or at.minute!=25:
                for (code,side),n in qty.items():
                    if n:marks[code]=D(prices.get(code,at)['close'])
        elif stage=='settlement':
            marks=dict(last_marks)
            for (code,side),n in qty.items():
                if not n:continue
                mark=settlements[code,day]
                cash+=side*(n*mark-basis[code,side])*inst[code].multiplier
                basis[code,side]=n*mark;marks[code]=mark
            for (code,side,group),lots in inventory.items():
                for lot in lots:lot[3]=settlements[code,day]
        else:raise AssertionError(stage)
        value,margin,exposure,net=snapshot(marks)
        assert value==D(obs['equity']) and margin==D(obs['margin']),(obs,value,margin)
        for p in ('IF','IH','IC','IM'):assert exposure[p]==D(obs[p+'_exposure'])
        ratio=margin/value if value>0 else None;breached=value<=0 or margin>value
        assert ratio==(D(obs['usage']) if obs['usage'] else None)
        assert breached==(obs['breached']=='True')
        observations+=1;breaches+=breached;max_ratio=max(max_ratio,ratio or D(0));max_gap=max(max_gap,margin-value)
        row=daily.setdefault(day,dict(day=day,max_actual_fund_usage=D(0),breach_observations=0))
        row['max_actual_fund_usage']=max(row['max_actual_fund_usage'],ratio or D(0));row['breach_observations']+=breached
        if accepted:
            assert value==D(accepted['equity_after']) and margin==D(accepted['margin_after'])
            if accepted['action'].startswith('enter_'):
                assert D(accepted['margin_before'])<D(accepted['equity_before'])*D('.9')
                assert margin<=value*D('.9')
                assert exposure[inst[accepted['instrument']].product]<=value*D('.5')
        logical_net=defaultdict(int)
        for (code,side),lots in logical.items():logical_net[code]+=side*sum(q for q,p in lots)
        for code in set(net)|set(logical_net)|set(cleanup_net):assert net[code]==logical_net[code]+cleanup_net[code]
        last_marks=marks
    assert fi==len(fills) and gi==len(guards)
    logical_pnl=logical_realized-logical_fees
    for (code,side),lots in logical.items():
        for q,entry in lots:logical_pnl+=q*side*(last_marks[code]-entry)*inst[code].multiplier
    adjustment=cleanup_cash+sum((q*last_marks[c]*inst[c].multiplier for c,q in cleanup_net.items() if q),D(0))
    assert logical_pnl==D(summary['logical_trade_pnl'])
    assert adjustment==D(summary['inventory_cleanup_adjustment'])
    assert logical_pnl+adjustment==D(summary['net_pnl'])
    assert cash==D(summary['equity'])
    expected=summary['funding_feasibility']
    assert observations==expected['observations'] and breaches==expected['breaches']
    assert max_ratio==D(expected['max_ratio']) and max_gap==D(expected['max_gap'])
    assert len(list(rows(root/'funding_breaches.csv')))==breaches
    sealed=Path('outputs/rbreaker_checkpoint_20261020_auction_atr20')
    manifest=json.loads((sealed/'manifest.json').read_text())
    assert all(hashlib.sha256((sealed/r['path']).read_bytes()).hexdigest()==r['sha256'] for r in manifest['files'])
    write(root/'funding_daily.csv',list(daily.values()))
    result=dict(accounting_status='passed',fee_avoidance_status='passed',entry_admission_status='passed',
                funding_feasibility='failed' if breaches else 'passed',observations=observations,
                actual_breaches=breaches,max_actual_fund_usage=str(max_ratio),max_funding_gap=str(max_gap),
                fills=fi,guard_deferred=deferred,close_today_quantity=0,route_lots=dict(action_counts),
                logical_trade_pnl=str(logical_pnl),inventory_cleanup_adjustment=str(adjustment),
                checkpoint_files=len(manifest['files']))
    (root/'routing_verification.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result,indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,default=Path('outputs/rbreaker_atr20_1pct_fee_safe_locks_historical_fees'))
    run(p.parse_args().root)
