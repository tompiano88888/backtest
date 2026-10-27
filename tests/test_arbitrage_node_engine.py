from datetime import date, datetime
from dataclasses import replace
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from backtest.arbitrage_node_engine import Liquidity, NodeCallback, raw_bar, valid_open
from backtest.engine import Engine, ExecutionConfig, Frame
from backtest.accounting import Ledger
from backtest.models import Account, Asset, D, Fill, Instrument, Instruction, MarginRule


ZONE=ZoneInfo('Asia/Shanghai')


def setup(rate=D('.038')):
    instruments={
        'IF2001.CFE':Instrument('IF2001.CFE',Asset.FUTURE,Account.FUTURES,'CFE','IF',D(10),D(1),expiry=date(2020,1,17),delivery=date(2020,1,17)),
        '000300.SH':Instrument('000300.SH',Asset.INDEX,Account.SECURITY,'SSE','IF'),
        '510300.SH':Instrument('510300.SH',Asset.FUND,Account.SECURITY,'SSE','IF',lot=100,sell_delay_days=1),
    }
    parameters=dict(schema_version=1,parameters=[dict(product='IF',decision_day='2020-01-02',
        window_start='2019-01-02',window_end='2020-01-02',entry_threshold='.01',exit_threshold='0')])
    liquidity=Liquidity([date(2020,1,2),date(2020,1,3),date(2020,1,6)],instruments,D('.15'))
    callback=NodeCallback(parameters,instruments,{('IF',date(2020,1,2)):'IF2001.CFE'},
                          lambda *args:rate,{'IF':D('.038')},liquidity)
    engine=Engine(Ledger(instruments,1200),{'IF2001.CFE':MarginRule('future',D('.15'))},ExecutionConfig(participation=D(1)))
    return engine,callback,liquidity


def frame(day, future='110', etf='1'):
    bars={c:raw_bar(c,dict(open=D(p),high=D(p),low=D(p),close=D(p),volume=100000),day,c=='000300.SH')
          for c,p in [('IF2001.CFE',future),('000300.SH','100'),('510300.SH',etf)]}
    return Frame(datetime(day.year,day.month,day.day,15,tzinfo=ZONE),bars,frozenset(bars))


def test_floor_is_inclusive_and_quantities_do_not_look_at_future_open():
    engine,callback,_=setup()
    engine.step(frame(date(2020,1,2)),on_bar=callback)
    assert not engine.ledger.fills
    assert len(callback.groups)==1
    engine.step(frame(date(2020,1,3),etf='.9'))
    security=next(f for f in engine.ledger.fills if f.instruction.instrument=='510300.SH')
    assert security.instruction.quantity==1000  # close sizing, not next-open 1100
    assert security.price==D('.9')
    below,cb,_=setup(D('.037999'))
    below.step(frame(date(2020,1,2)),on_bar=cb)
    assert not below.operations


def test_T1_reserve_uses_actual_fills_and_next_observed_trading_day():
    engine,_,liquidity=setup()
    assert liquidity.locked==0
    fill=Fill(datetime(2020,1,3,9,30,tzinfo=ZONE),date(2020,1,3),Instruction('510300.SH',1,100,'close','g'),D(2),D(0))
    ledger=SimpleNamespace(fills=[fill])
    liquidity.sync(ledger,date(2020,1,3))
    assert liquidity.locked==200
    liquidity.sync(ledger,date(2020,1,4))
    assert liquidity.locked==200
    liquidity.sync(ledger,date(2020,1,6))
    assert liquidity.locked==0 and len(liquidity.audit)==1


def test_missing_volume_is_not_a_tradable_bar():
    row=dict(open=D(1),high=D(1),low=D(1),close=D(1),source_volume=None,ohlcv_complete=False)
    assert raw_bar('510300.SH',row,date(2020,1,2)) is None
    assert raw_bar('000300.SH',row,date(2020,1,2),True).volume==0


def test_execution_day_cannot_open_expiring_or_replaced_front():
    engine,_,_=setup()
    state=dict(future='IF2001.CFE',product='IF')
    mapping={('IF',date(2020,1,17)):'IF2001.CFE',('IF',date(2020,1,16)):'IF2002.CFE'}
    assert not valid_open(state,date(2020,1,17),engine.ledger.instruments,mapping)
    assert not valid_open(state,date(2020,1,16),engine.ledger.instruments,mapping)


def test_funding_exit_waits_for_next_bar_no_same_close_rollback():
    engine,callback,_=setup()
    engine.step(frame(date(2020,1,2)),on_bar=callback)
    engine.step(frame(date(2020,1,3)))
    shock=frame(date(2020,1,6),future='300')
    shock=replace(shock,settlements=(('IF2001.CFE',date(2020,1,6),D(300)),),
                  settle_instruments=frozenset(shock.bars))
    engine.step(shock,on_day_end=callback)
    assert callback.audit[-1]['reason']=='funding_next_open'
    assert len(engine.ledger.fills)==2
    engine.step(frame(date(2020,1,7),future='300'))
    assert len(engine.ledger.fills)==4 and not engine.ledger.lots
