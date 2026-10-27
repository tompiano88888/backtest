from dataclasses import replace
from datetime import date,timedelta

from backtest.accounting import Ledger
from backtest.engine import Engine,ExecutionConfig,Frame
from backtest.fees import FeeRule
from backtest.models import D,MarginRule
from backtest.rbreaker_fee_avoidance import FeeAvoidingRBreaker
from tests.test_rbreaker import bar,fixture


class Fees:
    def resolve(self,code,day):
        return FeeRule(D('.000023'),D(0),D('.000023'),D(0),D('.00023'),D(0),'today_first','test-history')


def setup():
    inst,_=fixture();day=date(2026,1,5)
    engine=Engine(Ledger({inst.id:inst},10000000),{inst.id:MarginRule('future',D('.15'))},
                  ExecutionConfig(participation=1,slippage_ticks=1,fee_mode='historical'),fee_provider=Fees())
    atr={(inst.id,day+timedelta(days=i)):D(40) for i in range(4)}
    strategy=FeeAvoidingRBreaker({inst.id:inst},atr)
    strategy.begin_day({'IF':inst.id},{inst.id:dict(high=100,low=90,close=95)})
    engine.execution_guard=strategy.execution_guard
    return inst,day,engine,strategy


def step(inst,day,engine,strategy,hm,price,volume=100000,settle=False):
    b=replace(bar(inst.id,day,hm,price),low=D(price)-1,high=D(price)+1,volume=volume)
    return engine.step(Frame(b.end,{inst.id:b},frozenset({inst.id}),
                             frozenset({inst.id}) if settle else frozenset(),
                             ((inst.id,day,D(price)),) if settle else ()),on_bar=strategy)


def test_same_day_reversal_reopens_instead_of_closing_today_lock():
    inst,day,e,s=setup()
    for hm,p in [('09:31',120),('09:32',121),('09:33',94),('09:34',93),('09:35',93)]:
        step(inst,day,e,s,hm,p)
    assert [r['action'] for r in s.routing_fills]==['enter_open','exit_hedge','enter_open']
    assert all(f.instruction.effect=='open' for f in e.ledger.fills)
    assert all(r['close_today_quantity']==0 for r in e.fees.details)
    assert sum(p.side*p.quantity for p in e.ledger.lots)<0


def locked_day():
    inst,day,e,s=setup()
    for hm,p in [('09:31',120),('09:32',121),('14:59',100)]:step(inst,day,e,s,hm,p)
    step(inst,day,e,s,'15:00',100,settle=True)
    assert len(e.ledger.lots)==2 and sum(p.side*p.quantity for p in e.ledger.lots)==0
    assert e.ledger.snapshot(e.margin_rules).margin>0
    return inst,day,e,s


def test_next_day_unlock_is_risk_entry_and_can_close_again_without_today_open():
    inst,day,e,s=locked_day();tomorrow=day+timedelta(days=1)
    s.begin_day({'IF':inst.id},{inst.id:dict(high=100,low=90,close=95)})
    step(inst,tomorrow,e,s,'09:31',130)
    step(inst,tomorrow,e,s,'09:32',131)
    assert s.routing_fills[-1]['action']=='enter_unlock'
    assert e.ledger.fills[-1].instruction.effect=='close'
    assert s.stops.active[inst.id]['lots'][0][1]==D('131.2')
    assert s.stops.active[inst.id]['distance']==40
    step(inst,tomorrow,e,s,'14:59',100)
    step(inst,tomorrow,e,s,'15:00',100,settle=True)
    assert s.routing_fills[-1]['action']=='exit_close'
    assert all(r['close_today_quantity']==0 for r in e.fees.details)


def test_partial_entry_keeps_one_budget_and_volume_zero_exit_keeps_inventory():
    inst,day,e,s=setup()
    step(inst,day,e,s,'09:31',120)
    maximum=s.stops.seeds[inst.id]['maximum']
    step(inst,day,e,s,'09:32',121,volume=1)
    step(inst,day,e,s,'09:33',122,volume=1)
    assert s.stops.active[inst.id]['opened_quantity']==2
    assert s.logic.intents[inst.id]['quantity']<=maximum
    step(inst,day,e,s,'14:59',100,volume=0)
    count=len(e.ledger.fills)
    step(inst,day,e,s,'15:00',100,volume=0,settle=True)
    assert len(e.ledger.fills)==count and sum(p.side*p.quantity for p in e.ledger.lots)==2


def test_stop_hedges_today_exposure_and_keeps_cooldown():
    inst,day,e,s=setup()
    step(inst,day,e,s,'09:31',120)
    step(inst,day,e,s,'09:32',121)
    step(inst,day,e,s,'09:33',80)
    assert s.events and s.stops.active[inst.id]['stopped']
    step(inst,day,e,s,'09:34',79)
    assert not s.logical_positions()
    assert s.routing_fills[-1]['action']=='exit_hedge'
    step(inst,day,e,s,'09:35',150)
    assert not s.logic.intents
    assert all(r['close_today_quantity']==0 for r in e.fees.details)


def test_obsolete_locked_inventory_cleanup_reconciles_original_exposure_trades():
    inst,day,e,s=locked_day();tomorrow=day+timedelta(days=1)
    s.begin_day({}, {})
    step(inst,tomorrow,e,s,'09:31',110)
    step(inst,tomorrow,e,s,'09:32',111)
    assert not e.ledger.lots
    assert [r['action'] for r in s.routing_fills[-2:]]==['cleanup','cleanup']
    trade_pnl=sum(r['net_pnl'] for r in s.records(e.ledger.marks))
    assert trade_pnl+s.inventory_adjustment(e.ledger.marks)==e.ledger.snapshot(e.margin_rules).pnl
    assert all(r['close_today_quantity']==0 for r in e.fees.details)


def test_unfilled_yesterday_hedge_is_cancelled_before_today_direct_close():
    inst,day,e,s=setup()
    step(inst,day,e,s,'09:31',120);step(inst,day,e,s,'09:32',121)
    step(inst,day,e,s,'14:59',100)
    step(inst,day,e,s,'15:00',100,volume=0,settle=True)
    tomorrow=day+timedelta(days=1)
    s.begin_day({'IF':inst.id},{inst.id:dict(high=130,low=90,close=100)})
    prep=bar(inst.id,tomorrow,'09:25',100).end
    e.step(Frame(prep,{},frozenset({inst.id})),on_bar=s)
    step(inst,tomorrow,e,s,'09:31',100)
    assert not e.ledger.lots
    assert s.routing_fills[-1]['action']=='exit_close'
    assert not any(f.instruction.effect=='open' and f.trading_date==tomorrow for f in e.ledger.fills)


def test_mix_of_unlock_and_new_open_uses_one_risk_budget_and_exit_hedges_all():
    inst,day,e,s=locked_day();tomorrow=day+timedelta(days=1)
    s.stops.atr[inst.id,tomorrow]=D(20)
    s.begin_day({'IF':inst.id},{inst.id:dict(high=100,low=90,close=95)})
    step(inst,tomorrow,e,s,'09:31',130)
    step(inst,tomorrow,e,s,'09:32',131)
    actions=[r['action'] for r in s.routing_fills if r['trading_date']==tomorrow]
    assert actions==['enter_unlock','enter_open']
    t=s.stops.active[inst.id]
    assert t['opened_quantity']<=t['maximum']
    assert t['opened_quantity']*t['planned_risk_per_lot']<=t['budget']
    step(inst,tomorrow,e,s,'14:59',100)
    step(inst,tomorrow,e,s,'15:00',100,settle=True)
    assert s.routing_fills[-1]['action']=='exit_hedge'
    assert all(r['close_today_quantity']==0 for r in e.fees.details)
