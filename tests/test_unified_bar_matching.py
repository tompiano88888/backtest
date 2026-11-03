"""Hand-reconciled quote crossing, minute/daily capacity and fallback fills."""
from dataclasses import replace
from datetime import timedelta

import pytest

from backtest.engine import ExecutionConfig, Frame
from backtest.models import Account, Asset, Auction, D, Fill, Instruction, Instrument
from backtest.runner import execution_from_record
from test_market_price_clamp import T, bar, fixture, step


@pytest.mark.parametrize('side,effect', [(1, 'open'), (-1, 'open'), (1, 'close'), (-1, 'close')])
@pytest.mark.parametrize('model', ['next_open', 'previous_close_then_open'])
def test_explicit_aggressive_quote_all_directions_and_models(side, effect, model):
    e = fixture()
    e.config = replace(e.config, price_model=model)
    buy = (side == 1) == (effect == 'open')
    if effect == 'close':
        e.ledger.apply(Fill(T-timedelta(days=1), T.date()-timedelta(days=1),
                            Instruction('F', side, 3, group='g'), D(100), D(0)))
    op = e.submit([Instruction('F', side, 3, effect, 'g', '110' if buy else '90')], T)
    snapshot = step(e, bar())
    fill = e.ledger.fills[-1]
    expected = D('100.4') if buy else D('99.6')
    fee = expected*30*D('.0001')
    assert op.state == 'completed' and fill.instruction.quantity == 3
    assert fill.price == expected and fill.time == T and fill.fee == fee
    assert snapshot.pnl == -D(12)-fee
    assert e.execution_details[-1]['method'] == 'limit_marketable'


@pytest.mark.parametrize('side,reference,expected', [(1, 110, 101), (-1, 90, 99)])
def test_reference_quote_crosses_range_at_slipped_open(side, reference, expected):
    e = fixture()
    e.config = replace(e.config, price_model='previous_close_then_open')
    seed = replace(bar(o=str(reference), h=str(reference), l=str(reference), c=str(reference)),
                   start=T-timedelta(minutes=1), end=T)
    step(e, seed)
    e.submit([Instruction('F', side, 1)], T)
    step(e, bar(h='105', l='95'))
    assert e.ledger.fills[0].price == expected and e.ledger.fills[0].time == T
    assert e.execution_details[0]['method'] == 'previous_close_marketable'


@pytest.mark.parametrize('side,quote,expected', [(1, '105', 105), (-1, '95', 95)])
def test_quote_on_boundary_remains_a_touch(side, quote, expected):
    e = fixture()
    e.submit([Instruction('F', side, 1, limit_price=quote)], T)
    step(e, bar(h='105', l='95'))
    assert e.ledger.fills[0].price == expected
    assert e.ledger.fills[0].time == T+timedelta(minutes=1)


@pytest.mark.parametrize('side,quote', [(1, '90'), (-1, '110')])
def test_passive_quote_outside_range_cannot_fill(side, quote):
    e = fixture()
    op = e.submit([Instruction('F', side, 1, limit_price=quote)], T)
    step(e, bar(h='105', l='95'))
    assert not e.ledger.fills and op.state == 'pending'
    assert e.execution_attempts[-1]['reason'] == 'LIMIT_PRICE_NOT_TOUCHED'


@pytest.mark.parametrize('side,expected', [(1, '101'), (-1, '99')])
def test_fallback_uses_same_slipped_market_price(side, expected):
    e = fixture()
    e.config = replace(e.config, price_model='previous_close_then_open')
    e.submit([Instruction('F', side, 1)], T)
    step(e, bar())  # no previous completed close, cannot attempt first quote
    second = replace(bar(h='105', l='95'), start=T+timedelta(minutes=1), end=T+timedelta(minutes=2))
    step(e, second)
    assert e.ledger.fills[0].price == D(expected)
    assert e.ledger.fills[0].time == second.start
    assert e.execution_details[0]['method'] == 'market_open'


@pytest.mark.parametrize('duration,expected', [(timedelta(minutes=1), 50), (timedelta(hours=6), 10)])
def test_automatic_minute_daily_budgets_shared_by_orders(duration, expected):
    e = fixture()
    e.config = ExecutionConfig(slippage_ticks=1)
    first = e.submit([Instruction('F', 1, 30, group='a', limit_price=110)], T)
    second = e.submit([Instruction('F', 1, 30, group='b', limit_price=110)], T)
    b = replace(bar(volume=100), end=T+duration)
    step(e, b)
    assert sum(f.instruction.quantity for f in e.ledger.fills) == expected
    assert sum(l.quantity for op in (first, second) for l in op.legs) == 60-expected
    assert all(f.price == D('100.4') for f in e.ledger.fills)


def test_minute_auto_budget_rounds_to_lots_and_does_not_repeat_filled_quantity():
    e = fixture()
    e.config = ExecutionConfig()
    e.ledger.instruments['F'] = replace(e.ledger.instruments['F'], lot=3)
    op = e.submit([Instruction('F', 1, 9, limit_price=110)], T)
    step(e, bar(volume=15))  # floor(15*50%)=7, round to lot -> 6
    assert e.ledger.fills[0].instruction.quantity == 6 and op.legs[0].quantity == 3
    step(e, replace(bar(volume=15), start=T+timedelta(minutes=1), end=T+timedelta(minutes=2)))
    assert op.state == 'completed' and e.ledger.quantity('F', 1, 'standalone') == 9


def test_runner_auto_defaults_and_explicit_override():
    auto = execution_from_record({'participation': None})
    assert auto.slippage_ticks == 1 and auto.participation_for(bar()) == D('.5')
    assert auto.participation_for(replace(bar(), end=T+timedelta(hours=6))) == D('.1')
    assert execution_from_record({'participation': '.2'}).participation_for(bar()) == D('.2')


@pytest.mark.parametrize('side,quote,can_fill', [(1, '110', True), (1, '90', False),
                                              (-1, '90', True), (-1, '110', False)])
def test_discrete_auction_keeps_price_budget_and_quote_constraint(side, quote, can_fill):
    e = fixture()
    e.config = ExecutionConfig()
    e.submit([Instruction('F', side, 20, limit_price=quote)], T-timedelta(seconds=1))
    auction = Auction('F', T, T.date(), D(100), 100)
    e.step(Frame(T, {}, frozenset({'F'}), auctions={'F': auction}))
    assert bool(e.ledger.fills) == can_fill
    if can_fill:
        assert e.ledger.fills[0].price == 100 and e.ledger.fills[0].instruction.quantity == 10


def test_marketable_reference_precedes_intrabar_touch_in_other_operation():
    e = fixture()
    e.config = replace(e.config, price_model='previous_close_then_open')
    step(e, replace(bar(), start=T-timedelta(minutes=1), end=T))
    # Submitted earlier but only touched intrabar; must follow the crossed quote.
    e.submit([Instruction('F', 1, 1, group='touch', limit_price=100)], T)
    e.submit([Instruction('F', 1, 1, group='crossed')], T)
    step(e, bar())
    assert [f.instruction.group for f in e.ledger.fills] == ['crossed', 'touch']
    assert [f.time for f in e.ledger.fills] == [T, T+timedelta(minutes=1)]


@pytest.mark.parametrize('side,field,reason', [(1, 'upper_limit', 'UPPER_LIMIT'),
                                            (-1, 'lower_limit', 'LOWER_LIMIT')])
def test_aggressive_quote_does_not_bypass_locked_limit(side, field, reason):
    e = fixture()
    e.submit([Instruction('F', side, 1, limit_price=110 if side == 1 else 90)], T)
    step(e, replace(bar(), **{field: D(100)}))
    assert not e.ledger.fills and e.execution_attempts[-1]['reason'] == reason


def test_marketable_etf_sell_still_waits_until_t_plus_one():
    e = fixture()
    e.config = ExecutionConfig()
    e.ledger.instruments['F'] = Instrument('F', Asset.FUND, Account.SECURITY, 'TEST', 'F',
                                          tick=D(1), lot=100, sell_delay_days=1)
    e.margin_rules = {}
    e.ledger.apply(Fill(T, T.date(), Instruction('F', 1, 100, group='g'), D(100), D(0)))
    op = e.submit([Instruction('F', 1, 100, 'close', 'g', limit_price=90)], T)
    step(e, bar(volume=1000))
    assert len(e.ledger.fills) == 1 and op.state == 'pending'
    assert e.execution_attempts[-1]['reason'] == 'NO_CLOSEABLE_POSITION'
    step(e, replace(bar(volume=1000), start=T+timedelta(days=1),
                    end=T+timedelta(days=1, minutes=1), trading_date=T.date()+timedelta(days=1)))
    assert op.state == 'completed' and e.ledger.fills[-1].price == D('99.6')
    assert e.ledger.quantity('F', 1, 'g') == 0


@pytest.mark.parametrize('price', ['NaN', 'Infinity'])
def test_order_quote_must_be_finite(price):
    with pytest.raises(ValueError):
        Instruction('F', 1, 1, limit_price=price)
