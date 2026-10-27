from datetime import date
from types import SimpleNamespace
from backtest.models import D
from backtest.nav_arbitrage_signal import adjusted_rate,NAVSignal
from backtest.rolling_arbitrage_optimization import ProductMarket


def test_nav_discount_increases_rate_and_premium_reduces_it():
    base=adjusted_rate(D(101),D(100),D(1),D(1),10)
    assert base==D('.365')
    assert adjusted_rate(D(101),D(100),D('.99'),D(1),10)==D('.73')
    assert adjusted_rate(D(101),D(100),D('1.02'),D(1),10)==D('-.365')


def test_missing_nav_and_expiry_do_not_generate_signal():
    assert adjusted_rate(D(101),D(100),D(1),None,10) is None
    assert adjusted_rate(D(101),D(100),D(1),D(1),0) is None
    assert adjusted_rate(D(101),D(100),D(1),D(0),10) is None


def test_training_indices_use_adjusted_rate_provider():
    days=[date(2025,1,i) for i in (2,3,6)]
    code='IF2501.CFE';mapping={('IF',d):code for d in days}
    instruments={code:SimpleNamespace(expiry=date(2025,1,17))}
    prices={(code,d):dict(close=D(100)) for d in days}
    spot={('000300.SH',d):dict(close=D(100)) for d in days}
    rates={days[0]:D('.03'),days[1]:D('-.03'),days[2]:None}
    market=ProductMarket('IF',days,mapping,prices,spot,instruments,lambda p,c,d:rates[d])
    assert market.entry_indices[D('.02')]==[0]
    assert market.exit_indices[D('-.02')]==[1]


def test_same_date_nav_only_and_future_changes_do_not_change_earlier_rate():
    day=date(2025,1,2);future_day=date(2025,1,3);code='IF2501.CFE'
    instruments={code:SimpleNamespace(expiry=date(2025,1,12))}
    prices={(code,day):dict(close=D(101)),(code,future_day):dict(close=D(1000))}
    spot={('000300.SH',day):dict(close=D(100)),('510300.SH',day):dict(close=D('.99'))}
    nav={('510300.SH',day):dict(unit_nav=D(1)),('510300.SH',future_day):dict(unit_nav=D(100))}
    a=NAVSignal(prices,spot,instruments,nav).rate('IF',code,day)
    nav['510300.SH',future_day]['unit_nav']=D(10000)
    assert NAVSignal(prices,spot,instruments,nav).rate('IF',code,day)==a
    del nav['510300.SH',day]
    assert NAVSignal(prices,spot,instruments,nav).rate('IF',code,day) is None
