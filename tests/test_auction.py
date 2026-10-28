from dataclasses import replace
from datetime import date, datetime, timedelta
from types import MappingProxyType
from zoneinfo import ZoneInfo

import pytest

from backtest.accounting import Ledger
from backtest.engine import Engine, ExecutionConfig, Frame
from backtest.models import Auction, Bar, D, Instruction, MarginRule
from backtest.runner import run_scenario
from test_accounting import future

T=datetime(2026,9,21,9,30,tzinfo=ZoneInfo('Asia/Shanghai'))


def make_engine():
    instruments={c:future(c) for c in ('A','B')}
    return Engine(Ledger(instruments,1000),{c:MarginRule('future',D('.1')) for c in instruments},
                  ExecutionConfig(participation=D(1),slippage_ticks=1,fee_rate=D('.000023')))


def auction_frame(stamp=T,volume=100):
    return Frame(stamp,{},frozenset({'A'}),auctions={'A':Auction('A',stamp,stamp.date(),D(100),volume)})


def normal_frame(stamp=T+timedelta(minutes=1),volume=100):
    b=Bar('A',stamp-timedelta(minutes=1),stamp,stamp.date(),D(105),D(110),D(100),D(106),volume)
    return Frame(stamp,{'A':b},frozenset({'A'}))


def test_auction_zero_slippage_both_sides_still_pay_fees():
    engine=make_engine()
    engine.submit([Instruction('A',1,2),Instruction('A',-1,3)],T-timedelta(minutes=1))
    engine.step(auction_frame())
    assert [f.price for f in engine.ledger.fills]==[D(100),D(100)]
    assert sum(f.fee for f in engine.ledger.fills)==D('.115')
    assert all(f.time==T for f in engine.ledger.fills)


def test_auction_result_callback_cannot_trade_same_auction_and_normal_slippage_remains():
    engine=make_engine()
    def callback(ctx):
        assert ctx.bars=={}
        assert ctx.auctions['A'].price==100
        assert isinstance(ctx.auctions,MappingProxyType)
        ctx.target('A',1)
    engine.step(auction_frame(),on_bar=callback)
    assert not engine.ledger.fills
    engine.step(normal_frame())
    assert engine.ledger.fills[0].price==106  # continuous price 105 + tick 1


def test_instruction_stamped_at_auction_is_too_late():
    engine=make_engine()
    engine.target('A',1,T)
    engine.step(auction_frame())
    assert not engine.ledger.fills


def test_auction_partial_combination_does_not_consume_three_bar_clock():
    engine=make_engine()
    op=engine.submit([Instruction('A',1,2,group='pair'),Instruction('B',-1,2,group='pair')],T-timedelta(minutes=1),combination=True)
    f=auction_frame(volume=1)
    f=replace(f,open_instruments=frozenset({'A','B'}),auctions={**f.auctions,'B':Auction('B',T,T.date(),D(100),0)})
    engine.step(f)
    assert op.attempts==0 and op.state=='pending'
    assert engine.ledger.quantity('A',1)==1
    engine.step(replace(normal_frame(),open_instruments=frozenset({'A','B'})))
    assert op.attempts==1 and engine.ledger.quantity('A',1)==2


def test_mixed_asset_auction_and_bar_frame_is_not_a_common_continuous_bar():
    engine=make_engine()
    op=engine.submit([Instruction('A',1,2,group='pair'),Instruction('B',-1,2,group='pair')],T-timedelta(minutes=1),combination=True)
    b=replace(normal_frame(stamp=T).bars['A'],instrument='B')
    f=auction_frame(volume=1)
    engine.step(replace(f,bars={'B':b},open_instruments=frozenset({'A','B'})))
    assert op.attempts==0 and op.state=='pending'
    assert engine.ledger.quantity('A',1)==1 and engine.ledger.quantity('B',-1)==2


@pytest.mark.parametrize('field,value',[('volume',0),('suspended',True),('upper_limit',D(100))])
def test_auction_does_not_bypass_actual_constraints(field,value):
    engine=make_engine()
    engine.target('A',1,T-timedelta(seconds=1))
    f=auction_frame()
    engine.step(replace(f,auctions={'A':replace(f.auctions['A'],**{field:value})}))
    assert not engine.ledger.fills


def test_auction_close_after_daily_settlement_does_not_double_count():
    engine=make_engine()
    engine.target('A',2,T-timedelta(seconds=1))
    engine.step(auction_frame())
    engine.step(Frame(T.replace(hour=15),{},frozenset(),frozenset({'A'}),(('A',T.date(),D(110)),)))
    engine.target('A',0,T.replace(hour=15))
    next_time=T+timedelta(days=1)
    engine.step(Frame(next_time,{},frozenset({'A'}),auctions={'A':Auction('A',next_time,next_time.date(),D(105),100)}))
    fees=sum(f.fee for f in engine.ledger.fills)
    assert not engine.ledger.lots
    assert engine.ledger.snapshot(engine.margin_rules).pnl==D(100)-fees


def test_auction_identity_validation_and_runner_json():
    engine=make_engine()
    f=auction_frame()
    with pytest.raises(ValueError,match='both'):
        engine.step(replace(normal_frame(),auctions={'A':Auction('A',T+timedelta(minutes=1),T.date(),D(100),1)}))
    with pytest.raises(ValueError,match='identity/time'):
        engine.step(replace(f,auctions={'A':replace(f.auctions['A'],time=T+timedelta(minutes=1))}))
    config=dict(initial_cash='1000',instruments=[dict(id='A',asset='future',account='futures',exchange='TEST',product='P',
                multiplier='10',tick='1',expiry='2026-09-23',delivery='2026-09-23')],
                margin_rules={'A':dict(model='future',rate='.1')},execution=dict(slippage_ticks=5,participation='1'),
                instructions=[dict(at=(T-timedelta(minutes=1)).isoformat(),instrument='A',quantity=1)],
                frames=[dict(time=T.isoformat(),open_instruments=['A'],auctions=[dict(instrument='A',time=T.isoformat(),
                             trading_date=str(T.date()),price='100',volume=100)])])
    result=run_scenario(config)
    assert result.ledger.fills[0].price==100
