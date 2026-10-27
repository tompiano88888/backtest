from dataclasses import replace
from datetime import date,timedelta
from types import SimpleNamespace

from backtest.engine import ExecutionConfig,Frame,StrategyContext
from backtest.models import D,Instruction,Fill
from backtest.rbreaker_exposure import NetExposureRBreaker,exposure_metrics
from backtest.rbreaker_portfolio import FEE
from tests.test_rbreaker import fixture,bar


def setup(direct=False):
    inst,engine=fixture()
    day=date(2026,1,5)
    policy=NetExposureRBreaker({inst.id:inst},{(inst.id,day+timedelta(days=n)):D(10) for n in range(4)},direct_close=direct)
    policy.begin_day({'IF':inst.id},{inst.id:dict(high=100,low=90,close=95)},D(1000000))
    engine.config=ExecutionConfig(participation=D(1),fee_rate=FEE,close_today_fee_rate=D('.00023') if direct else None)
    engine.execution_guard=policy.execution_guard
    engine.execution_observer=policy.observe_execution
    return inst,engine,policy,day


def step(engine,policy,code,day,hm,price,volume=100):
    b=replace(bar(code,day,hm,price),volume=volume)
    return engine.step(Frame(b.end,{code:b},frozenset({code})),on_bar=policy)


def seed_locks(inst,engine,policy,day,quantities):
    b=replace(bar(inst.id,day,'09:31',100),volume=10000)
    ctx=SimpleNamespace(time=b.start)
    for quantity in quantities:
        batch=policy.new_batch(inst.id,1,'breakout',ctx)
        for side,group in batch['groups'].items():
            engine.submit([Instruction(inst.id,side,quantity,group=group)],b.start)
    guard=engine.execution_guard;engine.execution_guard=None
    engine.step(Frame(b.end,{inst.id:b},frozenset({inst.id})),on_bar=policy)
    engine.execution_guard=guard


def test_multi_container_unlock_and_new_open_share_one_five_percent_batch():
    inst,engine,policy,day=setup()
    seed_locks(inst,engine,policy,day,[2,2])
    next_day=day+timedelta(days=1)
    policy.begin_day({'IF':inst.id},{inst.id:dict(high=120,low=100,close=110)},D(1000000))
    step(engine,policy,inst.id,next_day,'09:31',134)
    assert len(policy.audit)==1 and policy.audit[0]['action']=='mixed'
    assert policy.audit[0]['new_lots']==8
    assert [r['quantity'] for r in policy.routing_audit]==[2,2,4]
    step(engine,policy,inst.id,next_day,'09:32',135)
    intent=policy.intents[1]
    assert intent['acquired_quantity']==8 and intent['basis']==D(135)
    assert 8*D(135)*D(300)*D('.15')<=intent['margin_budget']
    assert all(b['stop_basis']==D(135) for b in policy.active.values() if b.get('intent_id')==1)


def test_unlock_is_denied_at_ninety_percent_even_with_zero_net_exposure():
    inst,engine,policy,day=setup()
    seed_locks(inst,engine,policy,day,[201])
    next_day=day+timedelta(days=1)
    policy.begin_day({'IF':inst.id},{inst.id:dict(high=120,low=100,close=110)},D(1000000))
    step(engine,policy,inst.id,next_day,'09:31',134)
    assert not policy.audit
    assert sum(p.quantity for p in engine.ledger.lots)==402


def test_opening_gap_rechecks_five_percent_and_partial_fill_fee():
    inst,engine,policy,day=setup()
    step(engine,policy,inst.id,day,'09:31',120)
    assert policy.audit[0]['new_lots']==9
    step(engine,policy,inst.id,day,'09:32',200)
    fill=engine.ledger.fills[0]
    assert fill.instruction.quantity==5
    assert fill.fee==5*D(200)*D(300)*FEE
    assert policy.guard_audit[0]['reason']=='logical_batch_5'
    assert policy.intents[1]['acquired_quantity']==5


def test_partial_volume_retries_same_logical_batch_without_new_budget():
    inst,engine,policy,day=setup()
    step(engine,policy,inst.id,day,'09:31',120)
    for hm in ['09:32','09:33','09:34']:
        step(engine,policy,inst.id,day,hm,105,volume=1)
    assert len(policy.intents)==1
    assert policy.intents[1]['acquired_quantity']==3
    assert len(engine.ledger.fills)==3


def test_actual_net_margin_is_distinct_from_locked_collateral():
    inst,engine,policy,day=setup()
    seed_locks(inst,engine,policy,day,[10])
    ctx=StrategyContext(engine,Frame(bar(inst.id,day,'09:32',100).end,{},frozenset()))
    net,exposure,margin=exposure_metrics(ctx.positions,ctx.marks,policy.instruments,policy.rate)
    assert net[inst.id]==0 and exposure['IF']==0
    assert margin['IF']==D(45000)


def test_direct_control_same_signal_and_atr_but_today_close_fee_is_tenfold():
    inst,engine,policy,day=setup(True)
    for hm,price in [('09:31',120),('09:32',105),('09:33',93),('09:34',92)]:
        step(engine,policy,inst.id,day,hm,price)
    close=engine.ledger.fills[-1]
    assert close.instruction.effect=='close'
    assert close.fee==close.instruction.quantity*D(92)*D(300)*D('.00023')
    assert not engine.ledger.lots


def test_passive_funding_breach_remains_visible_without_liquidation():
    inst,engine,policy,day=setup()
    # Real existing short inventory can gap into a funding deficit without a fill.
    b=bar(inst.id,day,'09:31',100)
    batch=policy.new_batch(inst.id,-1,'breakout',SimpleNamespace(time=b.start))
    engine.ledger.apply(Fill(b.start,day,Instruction(inst.id,-1,200,group=batch['groups'][-1]),D(100),D(0)))
    # Diagnostic observer must not alter the account or hide the breach.
    ctx=engine._execution_view(b.end,day,'continuous',{inst.id:D(105)})
    policy.observe_execution(ctx,'open')
    assert policy.funding['status']=='failed' and policy.funding['max_ratio']>1
    assert engine.ledger.cash==D(1000000) and engine.ledger.lots[0].quantity==200


def test_new_open_and_unlock_have_identical_fifty_percent_exposure_boundary():
    results=[]
    for unlock in (False,True):
        inst,engine,policy,day=setup()
        old=day-timedelta(days=1)
        stamp=bar(inst.id,day,'09:31',100).start
        active=policy.new_batch(inst.id,1,'breakout',SimpleNamespace(time=stamp))
        locked=policy.new_batch(inst.id,1,'breakout',SimpleNamespace(time=stamp))
        for batch,side,quantity in [(active,1,110),(locked,1,30),(locked,-1,30)]:
            engine.ledger.apply(Fill(stamp,old,Instruction(inst.id,side,quantity,group=batch['groups'][side]),D(100),D(0)))
        batch=locked if unlock else policy.new_batch(inst.id,1,'breakout',SimpleNamespace(time=stamp))
        batch.update(state='entering',direction=1,intent_id=1)
        policy.intents[1]=dict(id=1,instrument=inst.id,side=1,margin_budget=D(50000),components=[batch['group']])
        side=-1 if unlock else 1
        leg=Instruction(inst.id,side,10,'close' if unlock else 'open',batch['groups'][side])
        def quote(q):return Fill(stamp,day,replace(leg,quantity=q),D(100),q*D(100)*D(300)*FEE)
        ctx=engine._execution_view(stamp,day,'continuous',{inst.id:D(100)},quote)
        results.append(policy.execution_guard(ctx,quote(10)))
        assert policy.guard_audit[-1]['exposure_after']<=policy.guard_audit[-1]['equity_after']*D('.5')
    assert results==[1,1]


def test_pending_exit_does_not_release_exposure_for_another_request():
    inst,engine,policy,day=setup()
    stamp=bar(inst.id,day,'09:31',100).start
    old=policy.new_batch(inst.id,1,'breakout',SimpleNamespace(time=stamp))
    fresh=policy.new_batch(inst.id,1,'breakout',SimpleNamespace(time=stamp))
    engine.ledger.apply(Fill(stamp,day,Instruction(inst.id,1,100,group=old['groups'][1]),D(100),D(0)))
    old['state']='exiting'
    fresh.update(state='entering',direction=1)
    ctx=engine._execution_view(stamp,day,'continuous',{inst.id:D(100)})
    plan={old['groups'][1]:0,old['groups'][-1]:0,fresh['groups'][1]:50,fresh['groups'][-1]:0}
    assert policy.reserved_exposure(ctx,plan)['IF']>D(500000)
    assert not policy.affordable(ctx,plan,'IF')
