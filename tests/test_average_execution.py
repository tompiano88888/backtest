"""Hand-reconciled average fills, partial capacity, fees and T+1."""
from dataclasses import replace
from datetime import timedelta

import pytest

from backtest.engine import ExecutionConfig, Frame
from backtest.models import Account, Asset, D, Fill, Instruction, Instrument
from test_market_price_clamp import T, bar, fixture, step


@pytest.mark.parametrize('side,effect', [(1,'open'),(-1,'open'),(1,'close'),(-1,'close')])
def test_average_all_directions_uses_turnover_multiplier_and_actual_fee(side,effect):
    e=fixture();e.config=replace(e.config,price_model='bar_average',participation=D('.5'),slippage_ticks=0)
    if effect=='close':
        e.ledger.apply(Fill(T-timedelta(days=1),T.date()-timedelta(days=1),Instruction('F',side,2,group='g'),D(100),D(0)))
    e.submit([Instruction('F',side,2,effect,'g')],T)
    snap=step(e,replace(bar(h='104',l='96',c='102',volume=10),turnover=D(10100)))
    fill=e.ledger.fills[-1]
    assert fill.price==101 and fill.time==T+timedelta(minutes=1)
    assert fill.fee==D('.202')
    assert e.execution_details[-1]['method']=='bar_vwap'
    assert snap.pnl==(D(20)*side if effect=='close' else D(20)*side)-D('.202')


@pytest.mark.parametrize('turnover,method',[(None,'bar_ohlc4'),(D(20000),'bar_ohlc4_invalid_vwap'),
                                         (D(0),'bar_ohlc4_invalid_vwap')])
def test_missing_or_invalid_vwap_falls_back_to_ohlc4(turnover,method):
    e=fixture();e.config=replace(e.config,price_model='bar_average')
    e.submit([Instruction('F',1,1)],T)
    step(e,replace(bar(o='100',h='104',l='96',c='102',volume=30),turnover=turnover))
    assert e.ledger.fills[0].price==D('100.5') and e.execution_details[0]['method']==method


def test_average_shared_half_volume_partial_remainder_and_final_pnl():
    e=fixture();e.config=replace(e.config,price_model='bar_average',participation=None)
    first=e.submit([Instruction('F',1,4,group='a')],T)
    second=e.submit([Instruction('F',1,4,group='b')],T)
    step(e,replace(bar(volume=10),turnover=D(10000)))
    assert [f.instruction.quantity for f in e.ledger.fills]==[4,1]
    assert first.state=='completed' and second.legs[0].quantity==3
    step(e,replace(bar(volume=10),turnover=D(10000),start=T+timedelta(minutes=1),end=T+timedelta(minutes=2)))
    assert second.state=='completed' and e.ledger.quantity('F',1,'b')==4
    assert sum(f.fee for f in e.ledger.fills)==D('.8')
    assert e.ledger.snapshot(e.margin_rules).pnl==D('-.8')


def test_average_etf_T1_lots_and_whole_odd_lot_close():
    e=fixture();e.config=replace(e.config,price_model='bar_average',participation=None)
    e.ledger.instruments['F']=Instrument('F',Asset.FUND,Account.SECURITY,'TEST','F',lot=100,sell_delay_days=1)
    e.margin_rules={}
    e.ledger.apply(Fill(T,T.date(),Instruction('F',1,200,group='g'),D(100),D(0)))
    op=e.submit([Instruction('F',1,200,'close','g')],T)
    step(e,bar(volume=1000));assert op.state=='pending' and len(e.ledger.fills)==1
    next_day=T+timedelta(days=1)
    e.cancel(op.id,e.now,'quantity_adjustment')
    e.ledger.corporate_action('adjustment','F',next_day.date(),D(0),D('1.25'))
    op=e.submit([Instruction('F',1,250,'close','g')],next_day)
    step(e,replace(bar(volume=350),start=next_day,end=next_day+timedelta(minutes=1),trading_date=next_day.date()))
    assert e.ledger.fills[-1].instruction.quantity==100 and op.legs[0].quantity==150
    step(e,replace(bar(volume=300),start=next_day+timedelta(minutes=1),end=next_day+timedelta(minutes=2),trading_date=next_day.date()))
    assert op.state=='completed' and e.ledger.fills[-1].instruction.quantity==150


def test_average_zero_volume_does_not_divide_or_fill():
    e=fixture();e.config=replace(e.config,price_model='bar_average')
    e.submit([Instruction('F',1,1)],T)
    step(e,replace(bar(volume=0),turnover=D(0)))
    assert not e.ledger.fills and e.execution_attempts[-1]['reason']=='NO_VOLUME'


def test_average_discrete_auction_retains_auction_price():
    from backtest.models import Auction
    e=fixture();e.config=replace(e.config,price_model='bar_average')
    e.submit([Instruction('F',1,1)],T-timedelta(seconds=1))
    e.step(Frame(T,{},frozenset({'F'}),auctions={'F':Auction('F',T,T.date(),D(100),100)}))
    assert e.ledger.fills[0].price==100 and e.execution_details[0]['method']=='auction'


def test_recurring_average_keeps_exact_cash_and_group_reconciliation():
    e=fixture();e.config=replace(e.config,price_model='bar_average',participation=D(1),fee_rate=D('.000023'))
    for group in ('a','b','c'):e.submit([Instruction('F',1,1,group=group)],T)
    step(e,replace(bar(h='104',l='96',volume=3),turnover=D(3010)))
    assert all(f.price==D('100.333333333333') for f in e.ledger.fills)
    assert sum(e.ledger.group_pnl().values(),D(0))==e.ledger.snapshot(e.margin_rules).pnl
    for group in ('a','b','c'):e.submit([Instruction('F',1,1,'close',group)],e.now)
    step(e,replace(bar(h='104',l='96',volume=3),turnover=D(3020),start=e.now,end=e.now+timedelta(minutes=1)))
    assert sum(e.ledger.group_pnl().values(),D(0))==e.ledger.snapshot(e.margin_rules).pnl
