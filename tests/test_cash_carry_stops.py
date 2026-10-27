from datetime import date
from types import SimpleNamespace
from backtest.cash_carry_signals import make_signals,stop_threshold
from backtest.models import D
from test_cash_carry_signals import fixture


def stop_fixture():
    rows,prices,spot,inst,days=fixture()
    inst['IF2501.CFE']=SimpleNamespace(expiry=date(2025,1,10),multiplier=D(300),tick=D('.2'))
    prices['IF2501.CFE',days[3]]['settlement_price']=D('100.6')
    return rows,prices,spot,inst,days


def test_stop_threshold_boundaries():
    assert stop_threshold(15)==D('.01')
    assert stop_threshold(14)==stop_threshold(5)==D('.005')
    assert stop_threshold(4)==D('.0025')


def test_negative_rate_filter_checks_actual_entry_day_after_five_day_signal():
    rows,prices,spot,inst,days=fixture(expiry=date(2025,1,8))
    signals,cycles,_=make_signals(rows,prices,spot,inst,2,near_expiry_filter=True)
    assert signals[2]['decision']=='schedule_entry'  # exactly five days: still permitted at decision
    assert signals[3]['event']=='entry_cancelled_near_expiry_negative'
    assert cycles==[]


def test_stop_uses_completed_day_mark_and_exits_next_open_and_persists_on_missing_quote():
    rows,prices,spot,inst,days=stop_fixture()
    signals,cycles,_=make_signals(rows,prices,spot,inst,2,True,True)
    assert cycles[0]['exit_reason']=='stop_loss'
    assert cycles[0]['stop_trigger_day']==days[3] and cycles[0]['exit_day']==days[4]
    assert cycles[0]['stop_trigger_ratio']<=-D('.005')
    assert cycles[0]['stop_denominator']==D(30000)
    spot[9]['open']=None
    signals,cycles,issues=make_signals(rows,prices,spot,inst,2,True,True)
    assert cycles[0]['exit_reason']=='stop_loss' and cycles[0]['exit_day']==days[5]
    assert signals[4]['decision']=='retry_stop_exit'
    assert any(r['reason']=='stop_loss_next_open_not_eligible' for r in issues)


def test_future_price_changes_do_not_change_earlier_stop_signals():
    rows,prices,spot,inst,days=stop_fixture()
    original,_,_=make_signals(rows,prices,spot,inst,2,True,True)
    prices['IF2501.CFE',days[-1]]['settlement_price']=D(1000)
    future,_,_=make_signals(rows,prices,spot,inst,2,True,True)
    assert original[:-1]==future[:-1]
