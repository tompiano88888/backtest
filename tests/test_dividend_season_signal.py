from datetime import date
from types import SimpleNamespace

import pytest

from backtest.dividend_season_signal import SeasonalForecast, DividendSeasonSignal, contributions, CODES
from backtest.cash_futures_basis import PAIRS
from backtest.models import D


def history(last=2022):
    return [dict(product=p, day=date(y, m, d), contribution=D('.003') if m == 6 else D(0))
            for p in CODES for y in range(2017, last+1)
            for m in range(1, 13) for d in (1, 28)]


def test_horizon_months_leap_year_and_completed_year_only():
    rows=history()
    model=SeasonalForecast(rows)
    # Two historical payments per June, total 0.6%; ten of thirty days.
    assert model.predict('IF',date(2020,6,10),date(2020,6,20)) == D('.002')
    assert model.predict('IF',date(2020,7,1),date(2020,7,20)) == 0
    assert model.predict('IF',date(2019,6,1),date(2019,6,20)) is None
    assert model.predict('IF',date(2020,6,20),date(2020,6,20)) is None
    changed=[dict(r,contribution=D('100')) if r['day'].year>=2020 else r for r in rows]
    assert SeasonalForecast(changed).predict('IF',date(2020,6,10),date(2020,6,20)) == D('.002')
    assert model.predict('IF',date(2020,2,28),date(2020,3,1)) == 0


def test_missing_month_and_negative_forecast_are_not_zero():
    rows=history()
    missing=[r for r in rows if not (r['day'].year==2019 and r['day'].month==2)]
    assert SeasonalForecast(missing).predict('IF',date(2020,6,1),date(2020,6,20)) is None
    negative=[dict(r,contribution=-r['contribution']) for r in rows]
    assert SeasonalForecast(negative).predict('IF',date(2020,6,1),date(2020,6,20)) is None


def test_total_return_difference_identity_and_gap_failure():
    spot={}; totals=[]
    for p,code in CODES.items():
        # Cash dividend 2 on prior index 100: price drops to 98, TR remains 100.
        for day,price in ((date(2019,6,3),D(100)),(date(2019,6,4),D(98))):
            spot[PAIRS[p][0],day]={'close':price}
            totals.append(dict(instrument=code,trading_date=day,close=D(100)))
    assert all(r['contribution']==D('.02') for r in contributions(spot,totals))
    totals.append(dict(instrument=CODES['IF'],trading_date=date(2019,6,5),close=D(100)))
    totals.append(dict(instrument=CODES['IF'],trading_date=date(2019,6,7),close=D(100)))
    spot[PAIRS['IF'][0],date(2019,6,6)]={'close':D(98)}
    spot[PAIRS['IF'][0],date(2019,6,7)]={'close':D(98)}
    with pytest.raises(ValueError,match='calendars differ'):
        contributions(spot,totals)


def test_adjustment_direction_and_annualization_without_booking_cash():
    signal=DividendSeasonSignal.__new__(DividendSeasonSignal)
    day=date(2020,6,10); expiry=date(2020,6,20)
    signal.prices={('F',day):{'close':D(3990)}}
    signal.spot={(PAIRS['IF'][0],day):{'close':D(4000)}}
    signal.instruments={'F':SimpleNamespace(expiry=expiry)}
    signal.forecast=SeasonalForecast(history()); signal.audit={}
    expected=(D('-.0025')+D('.002'))*D(365)/D(10)
    assert signal.rate('IF','F',day)==expected
    assert signal.audit['IF','F',day]['forecast_year_end']==2019


def test_may_august_only_counts_future_overlap_without_redistributing_other_months():
    rows=[dict(r,contribution=D('.001')) for r in history()]
    model=SeasonalForecast(rows,active_months=(5,6,7,8))
    assert model.predict('IF',date(2020,4,25),date(2020,5,10)) == D('.002')*10/31
    assert model.predict('IF',date(2020,8,25),date(2020,9,10)) == D('.002')*6/31
    assert model.predict('IF',date(2020,4,30),date(2020,8,31)) == D('.008')
    assert model.predict('IF',date(2020,9,1),date(2020,9,20)) == 0
    assert SeasonalForecast([],active_months=(5,6,7,8)).predict('IF',date(2020,9,1),date(2020,9,20)) == 0
    assert SeasonalForecast([],active_months=(5,6,7,8)).predict('IF',date(2020,5,1),date(2020,5,20)) is None


@pytest.mark.parametrize('months',[(5,5),(),(0,5),(5,13),(True,5)])
def test_invalid_season_months_rejected(months):
    with pytest.raises(ValueError,match='Active months'):
        SeasonalForecast([],active_months=months)
