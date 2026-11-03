"""Explicit auction capacity simulation, with manually reconciled cash and rules."""
from dataclasses import replace
from datetime import date,datetime,timedelta
from zoneinfo import ZoneInfo
import pytest
from backtest.accounting import Ledger
from backtest.engine import Engine,ExecutionConfig,Frame
from backtest.models import Account,Asset,Auction,Bar,D,Fill,Instruction,Instrument

T=datetime(2020,1,3,15,tzinfo=ZoneInfo('Asia/Shanghai'))
def setup(exempt=None,*,today=False,groups=('g',)):
    inst=Instrument('E',Asset.FUND,Account.SECURITY,'SYNTH','E',tick=D('.001'),lot=100,sell_delay_days=1)
    ledger=Ledger({'E':inst},D(100000))
    for g in groups:
        d=T.date() if today else T.date()-timedelta(days=1)
        ledger.apply(Fill(T-timedelta(hours=1),d,Instruction('E',1,1000,group=g),D(3),D(0)))
    return Engine(ledger,{},ExecutionConfig(price_model='bar_average',commission_rate=D('.0001')),auction_volume_exemption=exempt)
def auction(e,**kw):
    a=Auction('E',T,T.date(),D('3.1'),0,**kw)
    return e.step(Frame(T,{},frozenset({'E'}),auctions={'E':a}))
def close(e,g='g',q=1000,limit=None):return e.submit([Instruction('E',1,q,'close',g,limit)],T-timedelta(minutes=3))

def test_default_zero_volume_does_not_fill():
    e=setup();op=close(e);auction(e)
    assert not op.filled and e.ledger.quantity('E',1,'g')==1000

def test_selected_zero_volume_auction_fills_remaining_and_reconciles_fee_cash():
    e=setup(lambda leg:leg.group=='g');op=close(e);s=auction(e)
    assert op.state=='completed' and e.ledger.fills[-1].price==D('3.1')
    assert e.ledger.fills[-1].fee==D('.31') and s.cash==D('100099.69') and s.pnl==D('99.69')
    assert e.execution_details[-1]['auction_volume_exempt'] is True
    assert e.execution_details[-1]['bar_volume']==0 and e.execution_details[-1]['method']=='auction'

def test_exemption_keeps_T1_rule():
    e=setup(lambda leg:True,today=True);op=close(e);auction(e)
    assert not op.filled and e.execution_attempts[-1]['reason']=='NO_CLOSEABLE_POSITION'

@pytest.mark.parametrize('kw,reason',[({'suspended':True},'SUSPENDED'),({'lower_limit':D('3.1')},'LOWER_LIMIT')])
def test_exemption_keeps_suspension_and_price_limits(kw,reason):
    e=setup(lambda leg:True);op=close(e);auction(e,**kw)
    assert not op.filled and e.execution_attempts[-1]['reason']==reason

def test_exemption_keeps_order_limit():
    e=setup(lambda leg:True);op=close(e,limit=D('3.2'));auction(e)
    assert not op.filled and e.execution_attempts[-1]['reason']=='LIMIT_PRICE_NOT_TOUCHED'

def test_guard_can_still_shrink_exempt_candidate_and_charge_actual_fee():
    e=setup(lambda leg:True);e.execution_guard=lambda ctx,f:400;op=close(e);auction(e)
    assert op.legs[0].quantity==600 and e.ledger.fills[-1].instruction.quantity==400
    assert e.ledger.fills[-1].fee==D('.124')

def test_exempt_leg_does_not_consume_other_orders_shared_native_capacity():
    e=setup(lambda leg:leg.group=='exempt',groups=('exempt','a','b'))
    close(e,'exempt',300);a=close(e,'a',200);b=close(e,'b',200)
    q=Auction('E',T,T.date(),D('3.1'),1000)
    e.step(Frame(T,{},frozenset({'E'}),auctions={'E':q}))
    fills=[f for f in e.ledger.fills if f.instruction.effect=='close']
    assert [(f.instruction.group,f.instruction.quantity) for f in fills]==[('exempt',300),('a',100)]
    assert a.legs[0].quantity==100 and b.legs[0].quantity==200

def test_bar_never_uses_auction_exemption():
    def forbidden(leg):raise AssertionError('Not an auction')
    e=setup(forbidden);op=close(e)
    b=Bar('E',T-timedelta(minutes=1),T,T.date(),*[D('3.1')]*4,0)
    e.step(Frame(T,{'E':b},frozenset({'E'})))
    assert not op.filled

def test_missing_auction_quote_cannot_be_fabricated_by_exemption():
    e=setup(lambda leg:True);op=close(e);e.step(Frame(T,{},frozenset({'E'})))
    assert not op.filled

def test_exemption_callback_must_return_boolean():
    e=setup(lambda leg:1);close(e)
    with pytest.raises(ValueError,match='return a bool'):auction(e)

def test_callback_must_be_callable():
    with pytest.raises(ValueError,match='callable'):setup(True)

def test_opening_lot_rounding_still_applies_without_auction_volume_limit():
    e=setup(lambda leg:True);e.ledger.instruments['E']=replace(e.ledger.instruments['E'],lot=100)
    # An outstanding close below one lot is allowed; opening validation remains strict.
    with pytest.raises(ValueError,match='lot size'):
        e.submit([Instruction('E',1,1,group='new')],T-timedelta(minutes=1))
