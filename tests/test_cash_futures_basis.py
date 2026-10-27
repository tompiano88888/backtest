from datetime import date
from types import SimpleNamespace
from backtest.cash_futures_basis import pair_rows
from backtest.models import D


def data():
    days=[date(2025,1,i) for i in (2,3,6)]
    rows=[dict(product='IF',day=d,near='IF2501.CFE') for d in days]
    prices={('IF2501.CFE',d):dict(close=D(1020),settlement_price=D(1021)) for d in days}
    inst={'IF2501.CFE':SimpleNamespace(expiry=days[-1],multiplier=D(300))}
    spot=[dict(instrument=c,trading_date=d,close=D(v),ohlcv_complete=True)
          for d in days for c,v in [('000300.SH',1000),('510300.SH',2)]]
    return rows,prices,spot,inst


def test_decimal_basis_anchor_tracking_identity_and_expiry_annualization():
    rows,prices,spot,inst=data()
    spot[3]['close']=D('2.02')
    result=pair_rows(rows,prices,spot,inst)
    assert result[0]['etf_scale']==D(500)
    assert result[0]['etf_spread']==result[0]['index_basis']==D(20)
    assert result[1]['etf_spread']==D(10)
    assert result[1]['etf_tracking_points']==D(10)
    assert result[1]['etf_spread']==result[1]['index_basis']-result[1]['etf_tracking_points']
    assert result[0]['etf_shares_exact']==D(150000)
    assert result[-1]['index_basis_annualized'] is None


def test_missing_prices_are_not_filled_and_future_price_cannot_change_anchor():
    rows,prices,spot,inst=data()
    original=pair_rows(rows,prices,spot,inst)
    spot[-1]['close']=D(100)
    changed=pair_rows(rows,prices,spot,inst)
    assert original[:2]==changed[:2]
    assert changed[-1]['etf_scale']==D(500)
    spot[1]['close']=None
    result=pair_rows(rows,prices,spot,inst)
    assert result[0]['etf_spread'] is None
    assert result[1]['anchor_date']==rows[1]['day']
    spot[3]['ohlcv_complete']=False
    result=pair_rows(rows,prices,spot,inst)
    assert 'analysis_only' in result[1]['issues']
    assert result[1]['etf_spread'] is not None


def test_suspected_structural_break_invalidates_anchor_without_modifying_raw_price():
    rows,prices,spot,inst=data()
    spot[3]['close']=spot[5]['close']=D(4)
    result=pair_rows(rows,prices,spot,inst)
    assert result[0]['etf_spread']==D(20)
    assert result[1]['raw_etf_spread']==D(-980)
    assert result[1]['etf_spread'] is None and result[2]['etf_spread'] is None
    assert result[1]['index_basis']==result[2]['index_basis']==D(20)
    assert result[1]['etf_close']==D(4)
    assert 'jump_gt5pct' in result[1]['issues']
