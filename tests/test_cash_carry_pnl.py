from datetime import date
from backtest.cash_carry_pnl import replay
from backtest.models import Instrument,Asset,Account,D


def fixture(reason='historical_mean'):
    days=[date(2025,1,i) for i in (2,3,6)]
    inst={'IF2501.CFE':Instrument('IF2501.CFE',Asset.FUTURE,Account.FUTURES,'CFFEX','IF',D(10),D('.1'),1,
                                 expiry=days[-1],delivery=days[-1])}
    prices={('IF2501.CFE',d):dict(open=D(110 if i==0 else 108),settlement_price=D(109 if i==0 else 108)) for i,d in enumerate(days)}
    spot=[dict(instrument=c,trading_date=d,close=D(v),open=D(v),ohlcv_complete=True,source_volume=D(10000))
          for i,d in enumerate(days) for c,v in [('000300.SH',100),('510300.SH',2 if i==0 else '2.04')]]
    cycles=[dict(id='IF_0',product='IF',future='IF2501.CFE',etf='510300.SH',signal_day=days[0],
                 entry_day=days[0],exit_day=days[-1] if reason=='expiry' else days[1],exit_reason=reason,entry_rate='0.1')]
    return cycles,prices,spot,inst,days


def test_two_leg_pnl_fund_cash_and_futures_settlement_not_duplicated():
    args=fixture()
    result=replay(*args,fee_rate=0,ticks=0,initial_cash=10000)
    assert result['cycles'][0]['etf_quantity']==500
    assert result['cycles'][0]['raw_price_pnl']==D(40)
    assert result['daily'][-1]['equity']==D(10040)
    assert result['terminal_positions']==[]
    assert sum(f['fee'] for f in result['fills'])==0


def test_expiry_cash_settlement_has_no_fake_future_close_fee_and_cost_identity():
    args=fixture('expiry')
    zero=replay(*args,fee_rate=0,ticks=0,initial_cash=10000)
    result=replay(*args,fee_rate='.00005',ticks=1,initial_cash=10000)
    assert len(result['fills'])==3
    assert result['terminal_positions']==[]
    r=result['cycles'][0]
    assert r['zero_cost_pnl']==zero['cycles'][0]['raw_price_pnl']==D(40)
    assert r['raw_price_pnl']+r['fees']+r['slippage']==D(40)
    assert any(e['kind']=='delivery' for e in result['entries'])


def test_held_end_is_valued_and_future_prices_do_not_change_entry():
    cycles,prices,spot,inst,days=fixture()
    cycles[0]['exit_day']=None;cycles[0]['exit_reason']='open_at_data_end'
    original=replay(cycles,prices,spot,inst,days,fee_rate=0,ticks=0,initial_cash=10000)
    prices['IF2501.CFE',days[-1]]['settlement_price']=D(105)
    later=replay(cycles,prices,spot,inst,days,fee_rate=0,ticks=0,initial_cash=10000)
    assert original['fills']==later['fills']
    assert original['daily'][:-1]==later['daily'][:-1]
    assert len(later['terminal_positions'])==1  # expired future settles; ETF remains without a scheduled exit
