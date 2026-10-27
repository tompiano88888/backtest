from datetime import date
from types import SimpleNamespace
from backtest.cash_carry_signals import annualized_rate,make_signals
from backtest.models import D


def fixture(expiry=date(2025,1,10)):
    days=[date(2025,1,i) for i in range(1,7)]
    rows=[dict(product='IF',day=d,near='IF2501.CFE',execution_day=days[i+1] if i+1<len(days) else None)
          for i,d in enumerate(days)]
    rates=['-.2','-.2','-.1','-.05','-.3','-.3']
    prices={('IF2501.CFE',d):dict(close=D(100)*(1+D(rate)*D((expiry-d).days)/365),
                                open=D(100),settlement_price=D(100)) for d,rate in zip(days,rates)}
    spot=[dict(instrument=c,trading_date=d,close=D(v),open=D(v),ohlcv_complete=True,source_volume=D(10000))
          for d in days for c,v in [('000300.SH',100),('510300.SH',2)]]
    inst={'IF2501.CFE':SimpleNamespace(expiry=expiry,multiplier=D(300))}
    return rows,prices,spot,inst,days


def test_annualization_sign_and_expiry_day():
    assert annualized_rate(D(101),D(100),10)==D('.365')
    assert annualized_rate(D(99),D(100),10)==D('-.365')
    assert annualized_rate(D(99),D(100),0) is None


def test_negative_rate_above_past_mean_enters_next_day_and_mean_exit_is_next_day():
    rows,prices,spot,inst,days=fixture()
    signals,cycles,issues=make_signals(rows,prices,spot,inst,min_history=2)
    assert len(cycles)==1
    cycle=cycles[0]
    assert cycle['entry_rate']<0
    assert cycle['entry_rate']>cycle['entry_mean']
    assert cycle['signal_day']==days[2] and cycle['entry_day']==days[3]
    assert cycle['exit_day']==days[5] and cycle['exit_reason']=='historical_mean'
    assert signals[2]['reference_count']==2
    assert abs(signals[2]['historical_mean']-D('-.2'))<D('1e-23')


def test_future_price_change_preserves_earlier_signals_and_missing_etf_open_blocks_entry():
    rows,prices,spot,inst,days=fixture()
    original,_,_=make_signals(rows,prices,spot,inst,2)
    prices['IF2501.CFE',days[-1]]['close']=D(1000)
    later,_,_=make_signals(rows,prices,spot,inst,2)
    assert original[:-1]==later[:-1]
    spot[7]['open']=None
    _,cycles,issues=make_signals(rows,prices,spot,inst,2)
    assert all(r['entry_day']!=days[3] for r in cycles)
    assert any(r['reason']=='enter_next_open_not_eligible' for r in issues)


def test_expiry_exit_does_not_annualize_zero_days_or_roll_contract():
    rows,prices,spot,inst,days=fixture(expiry=date(2025,1,6))
    prices['IF2501.CFE',days[4]]['close']=D('99.999')
    signals,cycles,_=make_signals(rows,prices,spot,inst,2)
    assert cycles[0]['exit_reason']=='expiry'
    assert cycles[0]['exit_day']==days[-1]
    assert cycles[0]['future']=='IF2501.CFE'
    assert signals[-1]['annualized_basis_rate'] is None
