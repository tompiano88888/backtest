"""Frozen quotation, favorable next open, touch and next-Bar escalation."""
from dataclasses import replace
from datetime import timedelta

import pytest

from backtest.engine import Frame
from backtest.models import D, Fill, Instruction
from test_market_price_clamp import T, bar, fixture, step


@pytest.mark.parametrize('side,effect',[(1,'open'),(-1,'open'),(1,'close'),(-1,'close')])
@pytest.mark.parametrize('open_price', ['98','102'])
def test_favorable_open_or_frozen_quote_all_directions(side,effect,open_price):
    e=fixture();e.config=replace(e.config,price_model='limit_open_then_market')
    buy=(side==1)==(effect=='open');quote=D(101) if buy else D(99)
    if effect=='close':
        e.ledger.apply(Fill(T-timedelta(days=1),T.date()-timedelta(days=1),Instruction('F',side,1,group='g'),D(100),D(0)))
    op=e.submit([Instruction('F',side,1,effect,'g',quote)],T)
    step(e,bar(o=open_price,h='105',l='95',c='100'))
    expected=min(D(open_price),quote) if buy else max(D(open_price),quote)
    fill=e.ledger.fills[-1]
    assert op.state=='completed' and fill.price==expected and fill.fee==expected*10*D('.0001')
    assert fill.time==(T if expected==D(open_price) else T+timedelta(minutes=1))


def test_failed_first_quote_goes_to_following_bar_market_with_one_tick():
    e=fixture();e.config=replace(e.config,price_model='limit_open_then_market')
    op=e.submit([Instruction('F',1,1,limit_price=99)],T)
    step(e,bar(o='102',h='104',l='101',c='103'))
    assert not e.ledger.fills and op.legs[0].limit_price==99
    assert e.execution_attempts[-1]['reason']=='LIMIT_PRICE_NOT_TOUCHED'
    step(e,replace(bar(h='105',l='95'),start=T+timedelta(minutes=1),end=T+timedelta(minutes=2)))
    assert e.ledger.fills[0].price==101 and e.execution_details[0]['method']=='market_open'


def test_frozen_quote_required_and_no_same_bar_after_callback():
    e=fixture();e.config=replace(e.config,price_model='limit_open_then_market')
    with pytest.raises(ValueError,match='frozen'):
        e.submit([Instruction('F',1,1)],T)
    b=bar(h='105',l='95')
    e.step(Frame(b.end,{'F':b},frozenset({'F'})),on_bar=lambda ctx:ctx.submit([Instruction('F',1,1,limit_price=101)]))
    assert not e.ledger.fills
    step(e,replace(b,start=b.end,end=b.end+timedelta(minutes=1)))
    assert e.ledger.fills[0].time==b.end and e.ledger.fills[0].price==100
