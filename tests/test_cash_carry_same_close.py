from datetime import date,timedelta
from backtest.cash_carry_same_close import simulate_close,exchange_fee
from backtest.models import D,Instrument,Asset,Account


def test_exchange_fee_units():
    assert exchange_fee(Asset.FUTURE,D(5000),1,D(300))==D('34.5')
    assert exchange_fee(Asset.FUND,D(5),100000,D(1))==D(20)


def test_same_close_fill_direction_and_independent_two_leg_pnl():
    history=[date(2024,1,1)+timedelta(days=i) for i in range(252)]
    actual=[date(2025,1,2),date(2025,1,3)]
    days=history+actual
    inst={'IF2501.CFE':Instrument('IF2501.CFE',Asset.FUTURE,Account.FUTURES,'CFFEX','IF',D(300),D('.2'),1,
                                 expiry=date(2025,1,10),delivery=date(2025,1,10))}
    rows=[dict(product='IF',day=d,near='IF2501.CFE') for d in days]
    prices={('IF2501.CFE',d):dict(close=D(90 if d in history else 105 if d==actual[0] else 99),
                                settlement_price=D(90 if d in history else 105 if d==actual[0] else 99)) for d in days}
    spot=[dict(instrument=c,trading_date=d,close=D(v),ohlcv_complete=True,source_volume=D(1000))
          for d in days for c,v in [('000300.SH',100),('510300.SH',2)]]
    result=simulate_close(rows,prices,spot,inst,days,start=actual[0],stops=False)
    assert len(result['fills'])==4
    assert result['fills'][0]['day']==actual[0]
    assert result['fills'][0]['price']==D('104.8')
    assert result['fills'][1]['price']==D('2.001')
    assert result['fills'][2]['price']==D('99.2')
    assert result['fills'][3]['price']==D('1.999')
    assert result['cycles'][0]['pnl']==D('1646.1924')
    assert result['daily'][-1]['equity']==D('10001646.1924')
    assert result['terminal_positions']==[]
