from dataclasses import replace
from datetime import date,datetime,timedelta
from zoneinfo import ZoneInfo
import pytest

from backtest.engine import Engine,ExecutionConfig,Frame
from backtest.models import D,Instruction,Fill,Auction,Account,Asset,Instrument,MarginRule,Bar
from backtest.accounting import Ledger
# Synthetic framework fixtures deliberately independent of research strategies.
def fixture():
    inst = Instrument('IF2601.CFE', Asset.FUTURE, Account.FUTURES, 'CFFEX', 'IF',
                      D(300), D('.2'), 1, date(2026,1,16), date(2026,1,16))
    e = Engine(Ledger({inst.id:inst}, 1000000), {inst.id:MarginRule('future',D('.15'))},
               ExecutionConfig(participation=D(1)))
    return inst, e


def bar(code, day, hm, price):
    end = datetime.fromisoformat(f'{day}T{hm}:00').replace(tzinfo=ZoneInfo('Asia/Shanghai'))
    return Bar(code,end-timedelta(minutes=1),end,day,*([D(price)]*4),100)



def test_guard_only_sees_opening_marks_and_can_shrink_with_exact_fees():
    inst,engine=fixture()
    engine.config=ExecutionConfig(participation=D(1),fee_rate=D('.000023'))
    day=date(2026,1,5)
    b=bar(inst.id,day,'09:32',100)
    engine.submit([Instruction(inst.id,1,5)],b.start)
    captured=[]
    def guard(ctx,candidate):
        captured.append(ctx)
        assert not hasattr(ctx,'bars') and not hasattr(ctx,'settlements')
        assert ctx.marks[inst.id]==D(100)
        assert ctx.fill_for_quantity(2).fee==D(2)*D(100)*D(300)*D('.000023')
        with pytest.raises(TypeError):ctx.marks[inst.id]=D(999)
        return 2
    engine.execution_guard=guard
    observations=[]
    engine.execution_observer=lambda ctx,stage:observations.append((stage,ctx.account.equity,ctx.marks[inst.id]))
    changed=replace(b,high=D(200),close=D(200))
    engine.step(Frame(b.end,{inst.id:changed},frozenset({inst.id})))
    assert engine.ledger.fills[0].instruction.quantity==2
    assert observations[0]==('open',D(1000000),D(100))
    assert observations[1]==('after_fill',D('999998.6200'),D(100))
    assert observations[-1][2]==D(200)
    assert captured[0].fill_for_quantity(2).price==D(100)
    assert captured[0].fill_for_quantity(2).instruction.quantity==2


def test_zero_guard_defers_remainder_without_fabricating_fill():
    inst,engine=fixture()
    day=date(2026,1,5);b=bar(inst.id,day,'09:32',100)
    op=engine.submit([Instruction(inst.id,1,3)],b.start)
    engine.execution_guard=lambda ctx,fill:0
    engine.step(Frame(b.end,{inst.id:b},frozenset({inst.id})))
    assert not engine.ledger.fills and op.legs[0].quantity==3 and op.state=='pending'
    engine.execution_guard=lambda ctx,fill:fill.instruction.quantity
    b=bar(inst.id,day,'09:33',101)
    engine.step(Frame(b.end,{inst.id:b},frozenset({inst.id})))
    assert engine.ledger.fills[0].instruction.quantity==3


def test_guard_cannot_increase_quantity_and_observes_previous_actual_fill():
    inst,engine=fixture()
    day=date(2026,1,5);b=bar(inst.id,day,'09:32',100)
    engine.submit([Instruction(inst.id,1,2,group='A'),Instruction(inst.id,1,2,group='B')],b.start)
    seen=[]
    def guard(ctx,fill):
        seen.append(sum(p['quantity'] for p in ctx.positions))
        return fill.instruction.quantity
    engine.execution_guard=guard
    engine.step(Frame(b.end,{inst.id:b},frozenset({inst.id})))
    assert seen==[0,2]
    b=bar(inst.id,day,'09:33',100)
    engine.submit([Instruction(inst.id,1,1)],b.start)
    engine.execution_guard=lambda ctx,fill:2
    with pytest.raises(ValueError,match='shrink'):
        engine.step(Frame(b.end,{inst.id:b},frozenset({inst.id})))


def test_close_today_fee_rate_uses_actual_fifo_dates():
    inst,engine=fixture()
    old=date(2026,1,5);day=old+timedelta(days=1)
    engine.config=ExecutionConfig(participation=D(1),fee_rate=D('.000023'),close_today_fee_rate=D('.00023'))
    for d,q in [(old,2),(day,3)]:
        engine.ledger.apply(Fill(bar(inst.id,d,'09:31',100).start,d,Instruction(inst.id,1,q),D(100),D(0)))
    b=bar(inst.id,day,'09:32',100)
    engine.submit([Instruction(inst.id,1,4,'close')],b.start)
    engine.step(Frame(b.end,{inst.id:b},frozenset({inst.id})))
    assert engine.ledger.fills[-1].fee==D(100)*D(300)*(2*D('.000023')+2*D('.00023'))


def test_guard_deferral_keeps_three_bar_combination_timeout_and_unwind():
    from test_engine import engine,frame,T
    e=engine()
    e.execution_guard=lambda ctx,fill:0 if fill.instruction.instrument=='B' else fill.instruction.quantity
    op=e.submit([Instruction('A',1,2,group='guard-pair'),Instruction('B',-1,2,group='guard-pair')],T,True)
    for n in (1,2,3):e.step(frame(n,volumes={'A':1,'B':100}))
    assert op.state=='unwinding' and e.ledger.quantity('A',1,'guard-pair')==2
    assert sum(f.instruction.quantity for f in e.ledger.fills)==2
    e.step(frame(4))
    assert op.state=='unwound' and not e.ledger.lots


def test_auction_guard_receives_result_price_and_does_not_add_tick():
    inst,e=fixture();day=date(2026,1,5)
    e.config=ExecutionConfig(slippage_ticks=1,participation=D(1))
    stamp=bar(inst.id,day,'09:31',123).start
    e.submit([Instruction(inst.id,1,2)],stamp-timedelta(minutes=5))
    def guard(ctx,fill):
        assert ctx.phase=='auction' and ctx.time==stamp
        assert ctx.marks[inst.id]==fill.price==D(123)
        return 1
    e.execution_guard=guard
    a=Auction(inst.id,stamp,day,D(123),100)
    e.step(Frame(stamp,{},frozenset({inst.id}),auctions={inst.id:a}))
    assert e.ledger.fills[0].price==D(123) and e.ledger.fills[0].instruction.quantity==1


def test_group_targets_keep_other_groups_and_readonly_fill_history():
    from dataclasses import FrozenInstanceError
    from test_engine import engine,frame,T
    e=engine()
    e.target_group('A',2,T,'batch-a')
    e.target_group('A',3,T,'batch-b')
    contexts=[]
    e.step(frame(1),lambda ctx:contexts.append(ctx))
    assert e.ledger.quantity('A',1,'batch-a')==2
    assert e.ledger.quantity('A',1,'batch-b')==3
    fills=contexts[0].fills_since()
    assert isinstance(fills,tuple) and len(fills)==2
    with pytest.raises(FrozenInstanceError):fills[0].fee=D(100)
    with pytest.raises(ValueError):contexts[0].fills_since(-1)
    e.target_group('A',0,e.now,'batch-a')
    e.step(frame(2))
    assert e.ledger.quantity('A',1,'batch-a')==0 and e.ledger.quantity('A',1,'batch-b')==3


def test_group_target_cannot_replace_combination_legs():
    from test_engine import engine,T
    e=engine()
    e.submit([Instruction('A',1,1,group='pair'),Instruction('B',-1,1,group='pair')],T,True)
    with pytest.raises(ValueError,match='combinations'):
        e.target_group('A',0,T,'pair')
