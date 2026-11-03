"""Hand-reconciled capped market slippage, directions, fees and shared volume."""
from dataclasses import replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from backtest.accounting import Ledger
from backtest.engine import Engine, ExecutionConfig, Frame
from backtest.models import Account, Asset, Bar, D, Fill, Instrument, Instruction, MarginRule

T=datetime(2020,1,2,9,30,tzinfo=ZoneInfo('Asia/Shanghai'))


def fixture():
    inst=Instrument('F',Asset.FUTURE,Account.FUTURES,'TEST','F',multiplier=D(10),tick=D(1),
                    expiry=date(2020,1,31),delivery=date(2020,1,31))
    return Engine(Ledger({'F':inst},10000),{'F':MarginRule('future',D('.1'))},
                  ExecutionConfig(participation=D('.1'),slippage_ticks=1,fee_rate=D('.0001')))


def bar(*,o='100',h='100.4',l='99.6',c='100',volume=30):
    return Bar('F',T,T+timedelta(minutes=1),T.date(),*(D(v) for v in (o,h,l,c)),volume)


def step(engine,b):
    return engine.step(Frame(b.end,{'F':b},frozenset({'F'})))


@pytest.mark.parametrize('side,effect,buy',[(1,'open',True),(-1,'open',False),
                                         (1,'close',False),(-1,'close',True)])
def test_market_clamps_high_or_low_full_quantity_and_fee_cash_pnl(side,effect,buy):
    e=fixture()
    if effect=='close':
        e.ledger.apply(Fill(T-timedelta(days=1),T.date()-timedelta(days=1),
                            Instruction('F',side,3,'open','g'),D(100),D(0)))
    op=e.submit([Instruction('F',side,3,effect,'g')],T)
    snapshot=step(e,bar())
    fill=e.ledger.fills[-1]
    expected=D('100.4') if buy else D('99.6')
    fee=expected*D(30)*D('.0001')
    assert op.state=='completed' and fill.instruction.quantity==3
    assert fill.price==expected and fill.time==T and fill.fee==fee
    # Each direction pays 0.4*3*10=12 adverse movement, plus the actual-price fee.
    assert snapshot.pnl==-D(12)-fee
    assert snapshot.cash==D(10000)-fee-(D(12) if effect=='close' else D(0))
    assert not any(w.get('reason')=='PRICE_OUTSIDE_BAR' for w in e.warnings)


@pytest.mark.parametrize('side,expected',[(1,D(101)),(-1,D(99))])
def test_market_inside_range_keeps_unclipped_tick_price(side,expected):
    e=fixture();e.submit([Instruction('F',side,1)],T)
    step(e,bar(h='105',l='95'))
    assert e.ledger.fills[0].price==expected


@pytest.mark.parametrize('side',[1,-1])
def test_flat_bar_clamps_both_market_directions_to_only_observed_price(side):
    e=fixture();e.submit([Instruction('F',side,1)],T)
    step(e,bar(h='100',l='100'))
    assert e.ledger.fills[0].price==100 and e.ledger.fills[0].fee==D('.1')


def test_clamping_does_not_expand_shared_volume_budget():
    e=fixture()
    first=e.submit([Instruction('F',1,2,group='first')],T)
    second=e.submit([Instruction('F',1,2,group='second')],T)
    step(e,bar())  # 30 * 10% = 3 contracts across both orders
    assert first.state=='completed' and second.state=='pending'
    assert [f.instruction.quantity for f in e.ledger.fills]==[2,1]
    assert [f.price for f in e.ledger.fills]==[D('100.4'),D('100.4')]
    assert second.legs[0].quantity==1


@pytest.mark.parametrize('side,limit_field,limit_value,reason',[
    (1,'upper_limit','100','UPPER_LIMIT'),(-1,'lower_limit','100','LOWER_LIMIT')])
def test_clamping_does_not_bypass_locked_opening_limit(side,limit_field,limit_value,reason):
    e=fixture();e.submit([Instruction('F',side,1)],T)
    step(e,replace(bar(),**{limit_field:D(limit_value)}))
    assert not e.ledger.fills
    assert any(w.get('reason')==reason for w in e.warnings)


def test_clamping_still_respects_exchange_price_limit_after_slippage():
    e=fixture();e.submit([Instruction('F',1,1)],T)
    step(e,replace(bar(),upper_limit=D('100.2')))
    assert not e.ledger.fills
    assert any(w.get('reason')=='PRICE_OUTSIDE_LIMITS' for w in e.warnings)


def test_aggressive_previous_close_quote_fills_at_capped_slipped_open():
    e=fixture();e.config=replace(e.config,price_model='previous_close_then_open')
    seed=replace(bar(h='100',l='100'),start=T-timedelta(minutes=1),end=T)
    step(e,seed)
    e.submit([Instruction('F',1,1)],T)
    step(e,bar())  # buy quote 101 exceeds high 100.4: slipped open capped at high
    assert e.ledger.fills[0].price==D('100.4') and e.ledger.fills[0].time==T
    assert e.execution_details[0]['method']=='previous_close_marketable'
