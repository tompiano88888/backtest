from dataclasses import replace
from datetime import date,datetime,timedelta
from types import SimpleNamespace

from backtest.engine import Frame,ExecutionConfig
from backtest.models import Auction,D
from backtest.rbreaker import TZ,reconcile_fills
from backtest.rbreaker_locks import LockedRBreaker
from backtest.rbreaker_portfolio import margin_usage,FEE
from tests.test_rbreaker import fixture,bar


def setup():
    inst,engine=fixture()
    engine.config=ExecutionConfig(participation=D(1),fee_rate=FEE)
    day=date(2026,1,5)
    atr={(inst.id,day+timedelta(days=n)):D(10) for n in range(5)}
    callback=LockedRBreaker({inst.id:inst},atr)
    callback.begin_day({'IF':inst.id},{inst.id:dict(high=100,low=90,close=95)},D(1000000))
    return inst,engine,callback,day


def step(engine,callback,code,day,hm,price,volume=100,settlement=None):
    b=replace(bar(code,day,hm,price),volume=volume)
    return engine.step(Frame(b.end,{code:b},frozenset({code}),frozenset({code}) if settlement is not None else frozenset(),
                             ((code,day,D(settlement)),) if settlement is not None else ()),on_bar=callback)


def lock_one():
    inst,engine,callback,day=setup()
    step(engine,callback,inst.id,day,'09:31',120)
    step(engine,callback,inst.id,day,'09:32',105)
    # Stop after this completed minute, opposite OPEN only at the next event.
    step(engine,callback,inst.id,day,'09:33',93)
    assert len(engine.ledger.fills)==1
    step(engine,callback,inst.id,day,'09:34',92)
    return inst,engine,callback,day


def test_today_atr_exit_creates_physical_lock_and_larger_side_margin():
    inst,engine,callback,day=lock_one()
    assert [f.instruction.effect for f in engine.ledger.fills]==['open','open']
    assert [f.instruction.side for f in engine.ledger.fills]==[1,-1]
    batch=next(iter(callback.active.values()))
    assert callback.net(batch)==0 and batch['state']=='locked'
    qty=callback.quantity(batch,1)
    assert qty==9
    snapshot=engine.ledger.snapshot(engine.margin_rules)
    assert snapshot.margin==qty*D(92)*inst.multiplier*D('.15')
    assert snapshot.pnl==-qty*D(13)*inst.multiplier-sum(f.fee for f in engine.ledger.fills)
    assert snapshot.pnl==reconcile_fills(engine)


def test_main_lock_stays_through_next_auction_then_signal_unlocks_only_batch():
    inst,engine,callback,day=lock_one()
    step(engine,callback,inst.id,day,'15:00',110,settlement=110)
    next_day=day+timedelta(days=1)
    callback.begin_day({'IF':inst.id},{inst.id:dict(high=120,low=100,close=110)},engine.ledger.last_equity)
    stamp=datetime.combine(next_day,datetime.min.time(),TZ).replace(hour=9,minute=25)
    engine.step(Frame(stamp,{},frozenset()),on_bar=callback)
    stamp=stamp.replace(minute=30)
    auction=Auction(inst.id,stamp,next_day,D(110),100)
    engine.step(Frame(stamp,{},frozenset({inst.id}),auctions={inst.id:auction}),on_bar=callback)
    assert len(engine.ledger.fills)==2
    step(engine,callback,inst.id,next_day,'09:31',134)
    assert len(engine.ledger.fills)==2
    step(engine,callback,inst.id,next_day,'09:32',135)
    fill=engine.ledger.fills[-1]
    assert fill.instruction.effect=='close' and fill.instruction.side==-1
    expected=int(callback.reference_equity*D('.05')/((D(134)+inst.tick)*inst.multiplier*D('.15')))
    assert fill.instruction.quantity==expected
    batch=next(iter(callback.active.values()))
    assert callback.net(batch)==expected
    assert batch['stop_basis']==D(135) # unlock price, not historical retained long entry
    assert batch['distance']==D(10)


def test_obsolete_main_both_closes_submitted_before_auction_and_zero_slip():
    inst,engine,callback,day=lock_one()
    step(engine,callback,inst.id,day,'15:00',110,settlement=110)
    next_day=day+timedelta(days=1)
    callback.begin_day({}, {},engine.ledger.last_equity)
    stamp=datetime.combine(next_day,datetime.min.time(),TZ).replace(hour=9,minute=25)
    engine.step(Frame(stamp,{},frozenset()),on_bar=callback)
    assert len(engine.ledger.fills)==2
    assert all(op.submitted==stamp for op in engine.operations if op.state=='pending')
    stamp=stamp.replace(minute=30)
    engine.config=ExecutionConfig(participation=D(1),fee_rate=FEE,slippage_ticks=1)
    auction=Auction(inst.id,stamp,next_day,D(115),100)
    snapshot=engine.step(Frame(stamp,{},frozenset({inst.id}),auctions={inst.id:auction}),on_bar=callback)
    assert not engine.ledger.lots
    assert len(engine.ledger.fills)==4
    assert all(f.price==D(115) and f.instruction.effect=='close' for f in engine.ledger.fills[-2:])
    assert snapshot.pnl==reconcile_fills(engine)
    assert sum(r['net_pnl'] for r in callback.records(engine.ledger.marks))==snapshot.pnl


def test_partial_hedge_carries_residual_then_closes_yesterday_instead_of_opening_more():
    inst,engine,callback,day=setup()
    for hm,price in [('09:31',120),('09:32',105),('09:33',93)]:
        step(engine,callback,inst.id,day,hm,price)
    step(engine,callback,inst.id,day,'15:00',108,volume=2,settlement=108)
    batch=next(iter(callback.active.values()))
    assert callback.net(batch)==7
    next_day=day+timedelta(days=1)
    callback.begin_day({'IF':inst.id},{inst.id:dict(high=120,low=100,close=110)},engine.ledger.last_equity)
    stamp=datetime.combine(next_day,datetime.min.time(),TZ).replace(hour=9,minute=25)
    engine.step(Frame(stamp,{},frozenset()),on_bar=callback)
    auction=Auction(inst.id,stamp.replace(minute=30),next_day,D(110),100)
    engine.step(Frame(auction.time,{},frozenset({inst.id}),auctions={inst.id:auction}),on_bar=callback)
    assert engine.ledger.fills[-1].instruction.effect=='close'
    assert engine.ledger.fills[-1].instruction.side==1
    assert engine.ledger.fills[-1].instruction.quantity==7
    assert callback.net(batch)==0
    assert callback.quantity(batch,1)==callback.quantity(batch,-1)==2


def test_yesterday_directional_exit_closes_without_hedge():
    inst,engine,callback,day=setup()
    step(engine,callback,inst.id,day,'09:31',120)
    step(engine,callback,inst.id,day,'09:32',121)
    step(engine,callback,inst.id,day,'14:59',122)
    # Prevent a previously submitted add from filling; completed EOD cancels it.
    step(engine,callback,inst.id,day,'15:00',122,volume=0,settlement=122)
    next_day=day+timedelta(days=1)
    callback.begin_day({'IF':inst.id},{inst.id:dict(high=125,low=115,close=122)},engine.ledger.last_equity)
    step(engine,callback,inst.id,next_day,'09:31',109)
    step(engine,callback,inst.id,next_day,'09:32',108)
    assert all(f.instruction.effect=='close' for f in engine.ledger.fills if f.trading_date==next_day)


def test_lock_keeps_margin_and_cannot_be_counted_as_free_net_zero_inventory():
    inst,engine,callback,day=lock_one()
    snapshot=engine.ledger.snapshot(engine.margin_rules)
    assert snapshot.margin>0
    # Independent aggregate includes both physical groups; max side, not net or sum.
    positions=[p.__dict__ for p in engine.ledger.lots]
    assert margin_usage(positions,engine.ledger.marks,{inst.id:inst},D('.15'))['IF']==snapshot.margin


def test_new_open_uses_remaining_larger_side_headroom_not_net_position():
    inst,engine,callback,day=lock_one()
    ctx=SimpleNamespace(time=bar(inst.id,day,'09:35',92).end,
                        positions=tuple(p.__dict__ for p in engine.ledger.lots),
                        marks=engine.ledger.marks,account=SimpleNamespace(equity=D(100000)))
    plan={g:callback.quantity(b,s) for b in callback.active.values() for s,g in b['groups'].items()}
    new=callback.new_batch(inst.id,1,'breakout',ctx)
    plan[new['groups'][1]]=9
    plan[new['groups'][-1]]=0
    usage,fees=callback.reservation(ctx,plan)
    assert usage['IF']>D(50000)
    assert not callback.affordable(ctx,plan,'IF')


def test_partial_obsolete_main_auction_keeps_unfilled_leg_and_retries():
    inst,engine,callback,day=lock_one()
    step(engine,callback,inst.id,day,'15:00',110,settlement=110)
    next_day=day+timedelta(days=1)
    callback.begin_day({}, {},engine.ledger.last_equity)
    stamp=datetime.combine(next_day,datetime.min.time(),TZ).replace(hour=9,minute=25)
    engine.step(Frame(stamp,{},frozenset()),on_bar=callback)
    auction=Auction(inst.id,stamp.replace(minute=30),next_day,D(115),13)
    engine.step(Frame(auction.time,{},frozenset({inst.id}),auctions={inst.id:auction}),on_bar=callback)
    assert sum(p.quantity for p in engine.ledger.lots)==5
    assert next(iter(callback.active.values()))['state']=='closing_all'
    snapshot=step(engine,callback,inst.id,next_day,'09:31',116)
    assert not engine.ledger.lots
    assert snapshot.pnl==reconcile_fills(engine)


def test_stop_residual_that_becomes_old_main_still_blocks_actual_exit_day():
    inst,engine,callback,day=setup()
    for hm,price in [('09:31',120),('09:32',105),('09:33',93)]:
        step(engine,callback,inst.id,day,hm,price)
    step(engine,callback,inst.id,day,'15:00',92,volume=2,settlement=92)
    next_day=day+timedelta(days=1)
    callback.begin_day({}, {},engine.ledger.last_equity)
    stamp=datetime.combine(next_day,datetime.min.time(),TZ).replace(hour=9,minute=25)
    engine.step(Frame(stamp,{},frozenset()),on_bar=callback)
    auction=Auction(inst.id,stamp.replace(minute=30),next_day,D(95),100)
    engine.step(Frame(auction.time,{},frozenset({inst.id}),auctions={inst.id:auction}),on_bar=callback)
    assert not engine.ledger.lots
    assert ('IF',next_day) in callback.locked
