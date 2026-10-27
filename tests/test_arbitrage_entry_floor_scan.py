from datetime import date
from types import SimpleNamespace

import pytest

from backtest.models import D
from backtest.pooled_rolling_arbitrage import Portfolio,replay_history
from scripts.scan_arbitrage_entry_floor import threshold_grid,FrozenChoices,entry_signature


def test_grid_is_exact_decimal_and_covers_all_41_registered_trials():
    grid=threshold_grid()
    assert len(grid)==41 and grid[0]==D(0) and grid[-1]==D('.04')
    assert grid[15]==D('.015')
    assert all(b-a==D('.001') for a,b in zip(grid,grid[1:]))
    for args in [('0','.04','0'),('0','.04','.003'),('NaN','.04','.001')]:
        with pytest.raises(ValueError):threshold_grid(*args)


def test_cached_choices_require_exact_product_and_date_no_future_fallback():
    before,after=date(2025,1,2),date(2025,1,3)
    market=SimpleNamespace(product='IF',dates=[before,after])
    choice=dict(decision_day=after,entry_threshold=D('.02'),exit_threshold=D('-.04'))
    frozen=FrozenChoices(market,{('IF',after):choice})
    with pytest.raises(KeyError):frozen.select(0,None)
    selected=frozen.select(1,None)
    selected['entry_threshold']=D('.9')
    assert choice['entry_threshold']==D('.02')


def test_shared_replay_keeps_calendar_repo_and_market_day_clock():
    friday,monday=date(2025,1,3),date(2025,1,6)
    fund=Portfolio({}, {}, {}, {},initial='10000000')
    replay_history(fund,[friday,monday],friday,monday,None)
    cash=D('10000000')
    for row,market in zip(fund.daily,[True,False,False,True]):
        interest=(cash*D('.015')/365).quantize(D('.00000001'))
        cash+=interest
        assert row['equity']==cash and row['repo_interest']==interest
        assert row['is_market_day']==market and row['positions']==0
    assert fund.ledger.cash==cash


def test_identical_signal_reuse_respects_raw_rate_floor_and_inclusive_boundary():
    days=[date(2023,1,2),date(2025,1,2),date(2025,1,3)]
    market=SimpleNamespace(product='IF',dates=days,front=['F']*3,
        instruments={'F':SimpleNamespace(expiry=date(2025,1,20))},rates=[None,D('.015'),D('.005')])
    choices={('IF',d):dict(entry_threshold=D(0),exit_threshold=D('-.04')) for d in days[1:]}
    args=(market,choices)
    assert entry_signature(*args,D('.015'),days[1],days[2])==(days[1],)
    assert entry_signature(*args,D('.016'),days[1],days[2])==()
    assert entry_signature(*args,D('.006'),days[1],days[2])==entry_signature(*args,D('.014'),days[1],days[2])
