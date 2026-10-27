from dataclasses import replace
from datetime import date, timedelta

import pytest

from backtest.accounting import Ledger
from backtest.engine import Engine, ExecutionConfig, Frame
from backtest.models import D, Instruction, MarginRule
from backtest.rbreaker_batches import BatchRBreaker
from tests.test_rbreaker import bar, fixture


def setup():
    inst,_=fixture()
    engine=Engine(Ledger({inst.id:inst},1000000),{inst.id:MarginRule('future',D('.15'))},ExecutionConfig(participation=D(1)))
    callback=BatchRBreaker({inst.id:inst})
    day=date(2026,1,5)
    callback.begin_day({'IF':inst.id},{inst.id:dict(high=100,low=90,close=95)},D(1000000))
    return inst,engine,callback,day


def step(inst,engine,callback,day,hm,price,volume=100,low=None,settlement=None):
    b=bar(inst.id,day,hm,price)
    b=replace(b,volume=volume,low=D(low) if low is not None else b.low)
    final=settlement is not None
    return engine.step(Frame(b.end,{inst.id:b},frozenset({inst.id}),frozenset({inst.id}) if final else frozenset(),
                             ((inst.id,day,D(settlement)),) if final else ()),on_bar=callback)


def test_repeated_batches_use_frozen_prior_equity_and_respect_caps():
    inst,engine,callback,day=setup()
    for minute in range(31,46):
        step(inst,engine,callback,day,f'09:{minute:02d}',120)
    assert len(callback.active)>1
    for batch in callback.active.values():
        assert batch['margin_budget']==D(50000)
        assert batch['budget']==D(5000)
        assert batch['reference_equity']==D(1000000)
    assert engine.ledger.snapshot(engine.margin_rules).margin<=D(500000)
    assert len({p.group for p in engine.ledger.lots})>1


def test_partial_entry_does_not_create_another_batch_until_remainder_finishes():
    inst,engine,callback,day=setup()
    step(inst,engine,callback,day,'09:31',120)
    step(inst,engine,callback,day,'09:32',120,volume=1)
    step(inst,engine,callback,day,'09:33',120,volume=1)
    assert len(callback.active)==1
    assert next(iter(callback.active.values()))['opened_quantity']==2


def test_batch_stop_closes_only_triggered_group_and_cancels_pending_new_entries():
    inst,engine,callback,day=setup()
    step(inst,engine,callback,day,'09:31',120)
    step(inst,engine,callback,day,'09:32',120)
    step(inst,engine,callback,day,'09:33',125)
    # First batch is profitable near 124, second batch loses 1 point per contract.
    second=sorted(callback.active.values(),key=lambda b:b['id'])[1]
    second['budget']=D(100)  # specific independent-group isolation regression
    step(inst,engine,callback,day,'09:34',124)
    assert second['stopped']
    first=sorted(callback.active.values(),key=lambda b:b['id'])[0]
    assert not first['stopped']
    step(inst,engine,callback,day,'09:35',124)
    assert any(p.group==first['group'] for p in engine.ledger.lots)
    assert not any(p.group==second['group'] for p in engine.ledger.lots)
    assert (inst.product,day) in callback.locked


def test_reference_equity_changes_only_at_day_boundary_and_old_batch_stop_stays_fixed():
    inst,engine,callback,day=setup()
    step(inst,engine,callback,day,'09:31',120)
    step(inst,engine,callback,day,'09:32',120)
    old=next(iter(callback.active.values()))
    step(inst,engine,callback,day,'14:59',130)
    step(inst,engine,callback,day,'15:00',130,settlement=130)
    next_day=day+timedelta(days=1)
    callback.begin_day({'IF':inst.id},{inst.id:dict(high=100,low=90,close=95)},engine.ledger.last_equity)
    step(inst,engine,callback,next_day,'09:31',130)
    assert old['budget']==D(5000) and old['margin_budget']==D(50000)
    newest=max(callback.active.values(),key=lambda b:b['id'])
    assert newest['margin_budget']==engine.ledger.last_equity*D('.05')
    assert newest['budget']==engine.ledger.last_equity*D('.005')


def test_independent_group_targets_cannot_manage_combination_or_other_group():
    inst,engine,callback,day=setup()
    b=bar(inst.id,day,'09:31',120)
    engine.target_group(inst.id,2,b.start,'one')
    engine.target_group(inst.id,3,b.start,'two')
    engine.step(Frame(b.end,{inst.id:b},frozenset({inst.id})))
    engine.target_group(inst.id,0,b.end,'one')
    b=bar(inst.id,day,'09:32',121)
    engine.step(Frame(b.end,{inst.id:b},frozenset({inst.id})))
    assert engine.ledger.quantity(inst.id,1,'one')==0
    assert engine.ledger.quantity(inst.id,1,'two')==3
    engine.submit([Instruction(inst.id,1,1,group='combo')],b.end,combination=True)
    with pytest.raises(ValueError,match='combinations'):
        engine.target_group(inst.id,0,b.end,'combo')


def test_stop_still_monitors_a_normal_exit_that_has_not_filled():
    inst,engine,callback,day=setup()
    step(inst,engine,callback,day,'09:31',120)
    step(inst,engine,callback,day,'09:32',120)
    batch=next(iter(callback.active.values()))
    batch.update(closing=True,exit_reason='normal_pending_exit')
    step(inst,engine,callback,day,'09:33',118,volume=0)
    # The pending normal exit cannot disable this batch's cash risk threshold.
    batch['budget']=D(100)
    step(inst,engine,callback,day,'09:34',117,volume=0)
    assert batch['stopped'] and batch['exit_reason']=='fixed_cash_stop'
    assert (inst.product,day) in callback.locked
