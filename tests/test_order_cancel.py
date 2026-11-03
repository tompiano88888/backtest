"""Cancellation only removes future execution, never reverses actual fills."""
from datetime import timedelta
from dataclasses import replace

import pytest

from backtest.models import Instruction
from test_market_price_clamp import T, bar, fixture, step


def test_cancel_partial_operation_preserves_fills_cash_and_unfilled_trace():
    e=fixture();op=e.submit([Instruction('F',1,5)],T)
    step(e,bar(volume=30))
    assert op.legs[0].quantity==2
    before=(e.ledger.cash,len(e.ledger.fills),e.ledger.quantity('F',1,'standalone'))
    assert e.cancel(op.id,e.now,'scheduled_transition')
    assert not e.cancel(op.id,e.now,'repeat')
    step(e,replace(bar(),start=T+timedelta(minutes=1),end=T+timedelta(minutes=2)))
    assert before==(e.ledger.cash,len(e.ledger.fills),e.ledger.quantity('F',1,'standalone'))
    warning=next(w for w in e.warnings if w['code']=='ORDER_CANCELLED')
    assert warning['legs'][0]['quantity']==2 and op.state=='cancelled'


def test_callback_cancel_uses_current_frame_and_completed_order_is_noop():
    e=fixture();op=e.submit([Instruction('F',1,5)],T)
    b=bar()
    from backtest.engine import Frame
    e.step(Frame(b.end,{'F':b},frozenset({'F'})),on_bar=lambda ctx:ctx.cancel(op.id,'noon_cutoff'))
    assert op.state=='cancelled'
    with pytest.raises(ValueError):e.cancel(op.id,e.now+timedelta(minutes=1))
    with pytest.raises(ValueError):e.cancel(999,e.now)
