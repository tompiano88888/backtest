from datetime import date
from decimal import Decimal

import pytest

from backtest.performance import statistics


def test_yearly_pnl_and_drawdown_use_opening_equity():
    rows=[dict(day='2020-12-30',equity='120'),dict(day='2020-12-31',equity='110'),
          dict(day='2021-01-04',equity='99'),dict(day='2021-01-05',equity='121')]
    m=statistics(rows,'100',date(2020,1,1))
    assert Decimal(m['pnl'])==21
    assert [Decimal(r['pnl']) for r in m['annual']]==[10,11]
    assert Decimal(m['annual'][1]['return_rate'])==Decimal('.1')
    assert Decimal(m['annual'][1]['max_drawdown'])==Decimal('.1')
    assert Decimal(m['max_drawdown'])==Decimal('.175')


def test_nonmarket_income_is_carried_to_next_market_return():
    rows=[dict(day='2020-01-02',equity='100',is_market_day='True'),
          dict(day='2020-01-03',equity='110',is_market_day='False'),
          dict(day='2020-01-06',equity='121',is_market_day='True')]
    m=statistics(rows,'100',date(2020,1,1))
    # Returns are 0 and 21%, not 0 and 10% nor a three-day sample.
    assert m['volatility']==pytest.approx((.21/(2**.5))*252**.5)
