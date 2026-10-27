from datetime import date,timedelta
from types import SimpleNamespace
from backtest.rolling_arbitrage_optimization import ProductMarket,walk_forward,year_before
from backtest.models import D
from test_all_contract_arbitrage import fixture


def market_fixture():
    days,inst,prices,spot=fixture()
    instruments={'IF2501.CFE':inst}
    mapping={('IF',d):'IF2501.CFE' for d in days}
    return ProductMarket('IF',days,mapping,prices,spot,instruments),days,prices


def test_window_terminal_marks_do_not_borrow_next_day_or_final_delivery():
    market,days,prices=market_fixture()
    short=market.evaluate(days[0],1,D('.04'),D('-.02'),D(10000))
    assert short['open_at_end'] and short['cycles']==1
    # Entry future103, current settlement100 -> 30 less one tick1, no future exit cost yet.
    assert short['arbitrage_pnl']==D(29)
    prices['IF2501.CFE',days[-2]]['settlement_price']=D(1000)
    other,_days,_prices=market_fixture()
    for key,row in prices.items():other.prices[key]=row
    assert other.evaluate(days[0],1,D('.04'),D('-.02'),D(10000))==short


def test_historical_candidate_pnl_and_calendar_repo_match_hand_example():
    market,days,_=market_fixture()
    result=market.evaluate(days[0],3,D('.04'),D('-.02'),D(10000))
    assert result['arbitrage_pnl']==D(58)
    assert not result['open_at_end']
    expected_repo=D(10000)*D('.015')/365
    assert abs(result['estimated_repo_interest']-expected_repo)<D('1e-20')


def test_pending_last_day_entry_does_not_trade_in_unseen_future():
    market,days,_=market_fixture()
    result=market.evaluate(days[0],0,D('.04'),D('-.02'),D(10000))
    assert result['cycles']==0 and result['arbitrage_pnl']==0


def test_calendar_year_leap_day_boundary():
    assert year_before(date(2024,2,29))==date(2023,2,28)
    assert year_before(date(2025,2,28))==date(2024,2,28)


def test_future_quotes_cannot_change_earlier_full_grid_parameter_selection():
    market,days,prices=market_fixture()
    original=market.select(2)
    prices['IF2501.CFE',days[3]].update(open=D(1000),close=D(1000),settlement_price=D(1000))
    market2,_,_=market_fixture()
    market2.prices=prices
    # Rebuild rates/threshold indices with the changed future observations.
    market2=ProductMarket(market2.product,market2.dates,market2.mapping,prices,market2.spot,market2.instruments)
    assert original==market2.select(2)


def test_actual_position_exit_threshold_frozen_after_selection():
    history=[date(2024,1,1)+timedelta(days=i) for i in range(365)]
    dates=history+[date(2025,1,i) for i in range(1,6)]
    inst=SimpleNamespace(product='IF',multiplier=D(10),tick=D('.1'),expiry=date(2026,1,1),delivery=date(2026,1,1))
    prices={('IFX',d):dict(open=D(103),close=D(103),settlement_price=D(103)) for d in dates}
    prices['IFX',date(2025,1,3)]['close']=D('97.5') # below -2 but above frozen -4
    prices['IFX',date(2025,1,4)]['close']=D(95)
    spot={('000300.SH',d):dict(open=D(100),close=D(100)) for d in dates}
    market=ProductMarket('IF',dates,{('IF',d):'IFX' for d in dates},prices,spot,{'IFX':inst})
    calls=[]
    def selector(i):
        calls.append(i)
        return dict(entry_threshold=D('.02'),exit_threshold=D('-.04'))
    result=walk_forward(market,selector=selector,progress=lambda *a,**k:None)
    cycle=result['cycles'][0]
    assert cycle['exit_day']==date(2025,1,5)
    assert cycle['exit_threshold']==D('-.04')
    assert len(calls)==1  # no reoptimization during the held cycle or on its exit date
