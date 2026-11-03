"""Hand-reconciled reference-price touch, next-open escalation and persistence."""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from backtest.accounting import Ledger
from backtest.engine import Engine, ExecutionConfig, Frame
from backtest.models import Account, Asset, Bar, D, Instrument, Instruction, MarginRule
from backtest.runner import engine_from_config, result

T=datetime(2020,1,2,9,30,tzinfo=ZoneInfo('Asia/Shanghai'))


def make_engine(*,participation=1,attempts=None,guard=None):
    instruments={c:Instrument(c,Asset.FUTURE,Account.FUTURES,'TEST',c,multiplier=D(10),tick=D(1),
                              expiry=date(2020,1,31),delivery=date(2020,1,31)) for c in ('A','B')}
    return Engine(Ledger(instruments,10000),{c:MarginRule('future',D('.1')) for c in instruments},
                  ExecutionConfig(participation=participation,slippage_ticks=1,fee_rate=D('.0001'),
                                  price_model='previous_close_then_open'),
                  combination_max_attempts=attempts,execution_guard=guard)


def frame(n,prices=None,missing=(),volume=100):
    bars={}
    for code in ('A','B'):
        if code in missing:continue
        o,h,l,c=(prices or {}).get(code,(100,110,90,100))
        bars[code]=Bar(code,T+timedelta(minutes=n-1),T+timedelta(minutes=n),T.date(),
                       *(D(v) for v in (o,h,l,c)),volume)
    return Frame(T+timedelta(minutes=n),bars,frozenset({'A','B'}))


@pytest.mark.parametrize('side,expected',[(1,D(101)),(-1,D(99))])
def test_reference_touch_uses_previous_completed_close_and_end_timestamp(side,expected):
    e=make_engine();e.step(frame(0))
    e.submit([Instruction('A',side,2)],T)
    e.step(frame(1,{'A':(105,109,95,108)}))
    fill=e.ledger.fills[0]
    assert fill.price==expected and fill.time==T+timedelta(minutes=1)
    assert fill.fee==expected*20*D('.0001')
    detail=e.execution_details[0]
    assert detail['reference_close']==100 and detail['reference_time']==T
    assert detail['method']=='previous_close'
    assert detail['bar_start']==T and detail['bar_end']==T+timedelta(minutes=1)


def test_failed_passive_reference_escalates_to_following_slipped_open_not_same_bar():
    e=make_engine();e.step(frame(0));op=e.submit([Instruction('A',1,1)],T)
    e.step(frame(1,{'A':(105,107,104,106)}))
    assert not e.ledger.fills and op.attempts==1
    assert e.execution_attempts[-1]['reason']=='REFERENCE_PRICE_NOT_TOUCHED'
    e.step(frame(2,{'A':(120,120,120,120)}))
    assert e.ledger.fills[0].price==120  # slipped 121 is capped at flat Bar high 120
    assert e.ledger.fills[0].time==T+timedelta(minutes=1)
    assert e.execution_details[0]['method']=='market_open' and op.state=='completed'


def test_missing_first_quote_and_no_reference_are_diagnostic_then_market():
    e=make_engine();op=e.submit([Instruction('A',1,1)],T)
    e.step(frame(1))
    assert not e.ledger.fills and e.execution_attempts[-1]['reason']=='NO_REFERENCE_CLOSE'
    e.step(frame(2,{'A':(105,105,105,105)}))
    assert op.state=='completed' and e.ledger.fills[0].price==105
    e=make_engine();e.step(frame(0));op=e.submit([Instruction('A',1,1)],T)
    e.step(frame(1,missing=('A',)))
    e.step(frame(2))
    assert op.state=='completed' and e.execution_details[0]['method']=='market_open'


def test_combination_keeps_remainder_after_three_attempts_without_unwind():
    e=make_engine();e.step(frame(0))
    op=e.submit([Instruction('A',1,1,group='pair'),Instruction('B',-1,4,group='pair')],T,True)
    for n in (1,2,3):e.step(frame(n,volume=1))
    assert op.state=='pending' and op.attempts==3
    assert e.ledger.quantity('A',1,'pair')==1 and e.ledger.quantity('B',-1,'pair')==3
    assert all(f.instruction.effect=='open' for f in e.ledger.fills)
    e.step(frame(4,volume=1))
    assert op.state=='completed' and e.ledger.quantity('B',-1,'pair')==4
    assert not any(w['code']=='COMBINATION_TIMEOUT' for w in e.warnings)
    # Long 1 at 101: -10. Four shorts at 99: -40.
    # Fees .101+4*.099=.497; PnL=-50.497, margin=500.
    snap=e.ledger.snapshot(e.margin_rules)
    assert snap.pnl==D('-50.497') and snap.cash==D('9999.503') and snap.margin==500


def test_market_fallback_keeps_limits_and_volume_constraints():
    e=make_engine();e.step(frame(0));op=e.submit([Instruction('A',1,1)],T)
    e.step(frame(1,missing=('A',)))
    from dataclasses import replace
    f=frame(2);f=replace(f,bars={**f.bars,'A':replace(f.bars['A'],upper_limit=D(100))})
    e.step(f)
    assert not e.ledger.fills and e.execution_attempts[-1]['reason']=='UPPER_LIMIT'
    e.step(frame(3,volume=0))
    assert not e.ledger.fills and e.execution_attempts[-1]['reason']=='NO_VOLUME'
    e.step(frame(4));assert op.state=='completed'


def test_market_open_precedes_other_operation_intrabar_cashflow():
    observed=[]
    def guard(ctx,fill):
        observed.append((fill.instruction.group,ctx.time,len(ctx.positions)))
        return fill.instruction.quantity
    e=make_engine(guard=guard);e.step(frame(0))
    old=e.submit([Instruction('A',1,1,group='fallback')],T)
    e.step(frame(1,missing=('A',)))
    e.submit([Instruction('B',1,1,group='new_touch')],e.now)
    e.step(frame(2))
    assert [r[0] for r in observed]==['fallback','new_touch']
    assert observed[0][1]<observed[1][1] and observed[0][2]==0
    assert old.state=='completed'


def test_valuation_reference_does_not_replace_last_completed_close():
    from dataclasses import replace
    e=make_engine();e.step(frame(0))
    e.step(Frame(T+timedelta(seconds=30),{},frozenset(),valuation_marks={'A':D(500)}))
    e.submit([Instruction('A',1,1)],e.now)
    # The overlapping [T,T+1m) bar is ineligible; first eligible bar starts T+1m.
    e.step(frame(1));assert not e.ledger.fills
    e.step(frame(2));assert e.ledger.fills[0].price==101


def test_runner_exposes_opt_in_and_preserves_default_timeout():
    from backtest.cli import demo_config
    cfg=demo_config();assert engine_from_config(cfg).combination_max_attempts==3
    cfg['combination_max_attempts']=None
    cfg['execution']['price_model']='previous_close_then_open'
    e=engine_from_config(cfg)
    assert e.combination_max_attempts is None
    assert e.config.price_model=='previous_close_then_open'
    assert 'execution_details' in result(e,cfg)
    with pytest.raises(ValueError):make_engine(attempts=0)
    with pytest.raises(ValueError):ExecutionConfig(price_model='unknown')
