from datetime import date,timedelta
from types import SimpleNamespace
import numpy as np

from backtest.continuous_spread import normalized, transform, EntryPolicy, experiments
from backtest.factor_simulation import simulate
from backtest.models import D
from test_expiry_spread_research import fixture


def test_normalization_and_zero_variance():
    assert abs(normalized(.03,5,{5:dict(mean=.01,std=.02)}) - 1) < 1e-12
    assert np.isnan(normalized(.03,5,{5:dict(mean=.01,std=0)}))
    assert np.isnan(normalized(.03,4,{5:dict(mean=.01,std=.02)}))


def synthetic():
    start=date(2020,1,1)
    historical=date(2019,12,1)
    instruments={c:SimpleNamespace(expiry=e,multiplier=D(10)) for c,e in
                 [('OLD',historical),('A',date(2020,1,3)),('B',date(2020,2,3)),('C',date(2020,3,3))]}
    days=[start+timedelta(days=i) for i in range(6)]
    old=[dict(product='IF',day=historical-timedelta(days=40-i),near='OLD',far='A',expiry_days=40-i,
              relative_spread=(-1 if i%2 else 1)*.01,execution_day=None) for i in range(41)]
    rows=old+[dict(product='IF',day=d,near='A' if i<3 else 'B',far='B' if i<3 else 'C',
                   expiry_days=(instruments['A' if i<3 else 'B'].expiry-d).days,
                   relative_spread=.01 if i<3 else .08,execution_day=days[i+1] if i+1<len(days) else None)
              for i,d in enumerate(days)]
    prices={(c,d):dict(close=D(v),open=D(v)) for d in days for c,v in [('A',100),('B',101),('C','109.08')]}
    calendars={'IF':sorted([r['day'] for r in rows]),'IH':[],'IC':[],'IM':[]}
    return rows,prices,calendars,instruments,days


def test_roll_bridge_uses_new_pair_previous_quote_and_does_not_add_level_jump():
    rows,prices,calendars,instruments,days=synthetic()
    result,_=transform(rows,prices,calendars,instruments,radius=100,min_samples=2,min_contracts=1)
    before=next(r for r in result if r['day']==days[2])
    after=next(r for r in result if r['day']==days[3])
    assert after['roll']
    assert abs(after['linked_delta'])<1e-12
    assert abs(after['linked']-before['linked'])<1e-12
    assert abs(after['expiry_z']-before['expiry_z'])>1


def test_future_changes_cannot_change_earlier_chain_or_factors_and_gap_resets():
    rows,prices,calendars,instruments,days=synthetic()
    original,_=transform(rows,prices,calendars,instruments,radius=100,min_samples=2,min_contracts=1)
    changed=[r|dict(relative_spread=1000) if r['day']==days[-1] else r for r in rows]
    result,_=transform(changed,prices,calendars,instruments,radius=100,min_samples=2,min_contracts=1)
    for a,b in zip(original,result):
        if a['day']<days[-1]:
            for name in ('expiry_z','linked','momentum_5','reversion_5'):
                assert (np.isnan(a[name]) and np.isnan(b[name])) or a[name]==b[name]
    prices['B',days[2]]['close']=None
    gap,_=transform(rows,prices,calendars,instruments,radius=100,min_samples=2,min_contracts=1)
    after=next(r for r in gap if r['day']==days[3])
    assert after['linked']==0 and after['segment']>1
    assert np.isnan(after['momentum_5'])  # only four live residual observations so far


def test_entry_is_next_open_fixed_integer_pair_and_no_expiry_crossing():
    inst,days,prices,rows,_=fixture()
    for r in rows:
        r['momentum_5']=3
    policy=EntryPolicy({'IF':dict(family='momentum',n=5,threshold=1)},inst,{'IF':days})
    # The near contract does not have five sessions left: no fabricated extension/roll.
    result=simulate(rows,prices,{'IF':days},inst,{'IF':{}},days[1],days[-1],sizing='one',details=True,target_policy=policy)
    assert result['fills']==[]
    assert any(r['reason']=='insufficient_contract_life' for r in result['decisions'])
    assert len(experiments())==21


def test_primary_residual_window_remains_available_across_unbridgeable_roll():
    rows,prices,calendars,instruments,days=synthetic()
    prices.pop(('C',days[2]))
    result,_=transform(rows,prices,calendars,instruments,radius=100,min_samples=2,min_contracts=1)
    last=next(r for r in result if r['day']==days[-1])
    assert np.isfinite(last['momentum_5'])
    assert last['segment']>1


def test_five_day_actual_contract_pnl_and_future_quote_changes_leave_entry_unchanged():
    from dataclasses import replace
    inst,days,prices,rows,_=fixture()
    inst['IF2501.CFE']=replace(inst['IF2501.CFE'],expiry=date(2025,1,7),delivery=date(2025,1,7))
    days.append(date(2025,1,7))
    for code in inst:
        prices[code,days[-1]]=dict(open=D(100 if code=='IF2501.CFE' else 120),
                                  close=D(100 if code=='IF2501.CFE' else 120),
                                  settlement_price=D(100 if code=='IF2501.CFE' else 120))
    for r in rows:
        r['momentum_5']=3
    spec=dict(family='momentum',n=5,threshold=1)
    def replay():
        return simulate(rows,prices,{'IF':days},inst,{'IF':{}},days[0],days[-1],sizing='one',
                        fee_rate=0,slip=0,details=True,target_policy=EntryPolicy({'IF':spec},inst,{'IF':days}))
    first=replay()
    assert len(first['fills'])==4
    assert [f['day'] for f in first['fills']]==[days[1],days[1],days[-1],days[-1]]
    assert all(f['quantity']==1 for f in first['fills'])
    assert first['daily'][-1]['equity']==D('10000100')
    prices['IF2502.CFE',days[-1]]['open']=D(130)
    later=replay()
    assert first['fills'][:2]==later['fills'][:2]
    assert first['daily'][:-1]==later['daily'][:-1]
