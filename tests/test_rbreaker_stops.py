from dataclasses import FrozenInstanceError, replace
from datetime import date, timedelta
import pytest

from backtest.accounting import Ledger
from backtest.engine import Engine, ExecutionConfig, Frame, StrategyContext
from backtest.models import D, MarginRule
from backtest.rbreaker_portfolio import PortfolioRBreaker
from backtest.rbreaker_stops import TradeStops, prior_atr
from tests.test_rbreaker import bar, fixture


def setup():
    inst,_=fixture()
    engine=Engine(Ledger({inst.id:inst},1000000),{inst.id:MarginRule('future',D('.15'))},ExecutionConfig(participation=D(1)))
    day=date(2026,1,5)
    atr={(inst.id,day):D(10),(inst.id,day+timedelta(days=1)):D(99)}
    policy=TradeStops({inst.id:inst},atr)
    strategy=PortfolioRBreaker({inst.id:inst},stop_policy=policy)
    strategy.begin_day({'IF':inst.id},{inst.id:dict(high=100,low=90,close=95)})
    return inst,engine,day,policy,strategy


def step(engine,strategy,b,settlement=None):
    final=settlement is not None
    return engine.step(Frame(b.end,{b.instrument:b},frozenset({b.instrument}),frozenset({b.instrument}) if final else frozenset(),
                             ((b.instrument,b.trading_date,D(settlement)),) if final else ()),on_bar=strategy)


def test_atr_uses_only_completed_prior_days_and_gap_true_range():
    start=date(2026,1,1)
    rows=[dict(instrument='A',trading_date=start,high=100,low=90,close=95),
          dict(instrument='A',trading_date=start+timedelta(days=1),high=120,low=115,close=118),
          dict(instrument='A',trading_date=start+timedelta(days=2),high=120,low=110,close=115)]
    result=prior_atr(rows,2)
    assert result=={('A',start+timedelta(days=2)):D('17.5')}
    rows[-1]['high']=1000
    assert prior_atr(rows,2)==result


def test_risk_sizing_caps_whole_trade_and_counts_partial_fills():
    inst,engine,day,policy,strategy=setup()
    step(engine,strategy,bar(inst.id,day,'09:31',120))
    seed=policy.seeds[inst.id]
    assert seed['maximum']==3
    assert seed['maximum']*seed['planned_risk_per_lot']<=D(10000)
    step(engine,strategy,replace(bar(inst.id,day,'09:32',121),volume=1))
    assert policy.active[inst.id]['opened_quantity']==1
    assert engine.targets[inst.id,'standalone']==3
    step(engine,strategy,replace(bar(inst.id,day,'09:33',122),volume=1))
    assert policy.active[inst.id]['opened_quantity']==2
    assert engine.targets[inst.id,'standalone']==3


def test_stop_on_intrabar_touch_executes_only_next_bar_and_blocks_reentry():
    inst,engine,day,policy,strategy=setup()
    step(engine,strategy,bar(inst.id,day,'09:31',120))
    step(engine,strategy,bar(inst.id,day,'09:32',121))
    step(engine,strategy,replace(bar(inst.id,day,'09:33',122),low=D(110)))
    assert policy.events[0]['stop_price']==D(111)
    assert len(engine.ledger.fills)==1  # cannot fill at this bar's low/threshold
    step(engine,strategy,bar(inst.id,day,'09:34',109))
    assert not engine.ledger.lots
    assert engine.ledger.fills[-1].price==D(109)
    assert policy.completed[0]['realized']==D(-10800)
    step(engine,strategy,bar(inst.id,day,'09:35',150))
    assert not engine.ledger.lots and not strategy.intents


def test_stop_persists_when_unfilled_across_settlement_and_day_change():
    inst,engine,day,policy,strategy=setup()
    step(engine,strategy,bar(inst.id,day,'09:31',120))
    step(engine,strategy,bar(inst.id,day,'09:32',121))
    step(engine,strategy,bar(inst.id,day,'14:59',110))
    step(engine,strategy,replace(bar(inst.id,day,'15:00',109),volume=0),109)
    assert engine.ledger.lots and policy.active[inst.id]['stopped']
    assert policy.active[inst.id]['budget']==D(10000)
    assert policy.active[inst.id]['distance']==D(10)
    tomorrow=day+timedelta(days=1)
    strategy.begin_day({'IF':inst.id},{inst.id:dict(high=130,low=100,close=109)})
    step(engine,strategy,bar(inst.id,tomorrow,'09:31',100))
    assert not engine.ledger.lots
    trade=policy.completed[0]
    assert trade['realized']==D(-18900)  # 3*300*(100-121), not reset settlement basis 109
    assert trade['distance']==10
    step(engine,strategy,bar(inst.id,tomorrow,'09:32',200))
    assert not strategy.intents and not engine.ledger.lots


def test_missing_atr_prevents_entry_and_fill_observation_is_immutable():
    inst,engine,day,policy,strategy=setup()
    policy.atr.clear()
    b=bar(inst.id,day,'09:31',120)
    step(engine,strategy,b)
    assert not strategy.intents
    ctx=StrategyContext(engine,Frame(b.end,{inst.id:b},frozenset({inst.id})))
    assert ctx.fills_since()==()
    with pytest.raises(ValueError):
        ctx.fills_since(-1)
    engine.target(inst.id,1,b.end)
    next_bar=bar(inst.id,day,'09:32',121)
    engine.step(Frame(next_bar.end,{inst.id:next_bar},frozenset({inst.id})))
    observed=StrategyContext(engine,Frame(next_bar.end,{inst.id:next_bar},frozenset({inst.id}))).fills_since()
    assert isinstance(observed,tuple) and len(observed)==1
    with pytest.raises(FrozenInstanceError):
        observed[0].price=D(0)
