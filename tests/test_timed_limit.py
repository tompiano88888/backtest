import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from backtest.accounting import Ledger
from backtest.engine import Engine, ExecutionConfig, Frame
from backtest.models import Account, Asset, Bar, D, Fill, Instrument, Instruction, MarginRule
from backtest.runner import run_scenario

TZ = ZoneInfo("Asia/Shanghai")
DAY = date(2026, 1, 5)


def make_engine(config=None, codes=("F",), cash=10_000_000):
    insts = {c: Instrument(c, Asset.FUTURE, Account.FUTURES, "X", c, D(10), D(1), 1, date(2030, 1, 1), date(2030, 1, 1))
             for c in codes}
    return Engine(Ledger(insts, cash), {c: MarginRule("future", D("0.1")) for c in codes},
                  config or ExecutionConfig(participation=D(".5"), slippage_ticks=1))


def mbar(code, hm, o, h, l, c, volume=100, turnover=None):
    end = datetime.fromisoformat(f"2026-01-05T{hm}:00").replace(tzinfo=TZ)
    return Bar(code, end - timedelta(minutes=1), end, DAY, D(o), D(h), D(l), D(c), volume,
               turnover=None if turnover is None else D(turnover))


def dbar(code, day, o, h, l, c, volume=100, turnover=None):
    start = datetime.fromisoformat(f"{day}T09:00:00").replace(tzinfo=TZ)
    return Bar(code, start, start.replace(hour=15), date.fromisoformat(day), D(o), D(h), D(l), D(c), volume,
               turnover=None if turnover is None else D(turnover))


def run(engine, frames, first_orders):
    """frames: list of dict code->Bar. first_orders(ctx) is called once, after the first frame."""
    seen = []
    state = {"done": False, "cursor": 0}

    def on_bar(ctx):
        fills = ctx.fills_since(state["cursor"])
        details = ctx.execution_details_since(state["cursor"])
        state["cursor"] += len(fills)
        seen.append(dict(fills=fills, details=details, warnings=[(w["code"], w.get("reason")) for w in ctx.warnings]))
        if not state["done"]:
            state["done"] = True
            first_orders(ctx)

    for bars in frames:
        end = next(iter(bars.values())).end
        engine.step(Frame(end, bars, frozenset(bars)), on_bar)
    return seen


def lim(code, qty, price, side=1, effect="open", group="standalone"):
    return Instruction(code, side, qty, effect, group, D(price), "limit_open_then_market")


@pytest.mark.parametrize('action', ['market', 'cancel'])
def test_timed_limit_five_minutes_partial_remainder_and_cash(action):
    # At 09:31 submit five lots at 99. Only two fill in five minutes.
    # At 09:36 market remainder fills at 101 or cancellation preserves two lots.
    e = make_engine()
    first = mbar('F', '09:31', 100, 102, 100, 100)
    frames = [{'F': first}]
    for minute in range(32, 37):
        frames.append({'F': mbar('F', f'09:{minute}', 100, 102, 99 if minute == 34 else 100,
                                100, volume=4 if minute == 34 else 100)})
    frames.append({'F': mbar('F', '09:37', 100, 102, 98, 100)})
    run(e, frames, lambda ctx: ctx.submit([Instruction(
        'F', 1, 5, limit_price=99, price_model='limit_timed',
        limit_wait_minutes=5, timeout_action=action)]))
    assert [(f.instruction.quantity, f.price) for f in e.ledger.fills] == (
        [(2, D(99)), (3, D(101))] if action == 'market' else [(2, D(99))])
    assert e.operations[0].state == ('completed' if action == 'market' else 'cancelled')
    assert e.ledger.cash == D(10_000_000)  # Futures opening has no principal cash movement.
    if action == 'cancel':
        assert any(w['code'] == 'ORDER_CANCELLED' and w['reason'] == 'limit_timeout' for w in e.warnings)
    quantity = e.ledger.quantity('F', 1, 'standalone')
    e.submit([Instruction('F', 1, quantity, 'close')], e.now)
    closing = mbar('F', '09:38', 101, 102, 99, 101)
    e.step(Frame(closing.end, {'F': closing}, frozenset({'F'})))
    assert e.ledger.cash == D(10_000_000) + (D(-10) if action == 'market' else D(20))
    assert not e.ledger.lots


@pytest.mark.parametrize('side,limit,opening,expected', [(1, 101, 100, 100), (-1, 99, 100, 100)])
def test_timed_limit_open_improvement(side, limit, opening, expected):
    e = make_engine()
    run(e, [{'F': mbar('F', '09:31', 100, 102, 98, 100)},
            {'F': mbar('F', '09:32', opening, 102, 98, 100)}],
        lambda ctx: ctx.submit([Instruction('F', side, 5, limit_price=limit,
            price_model='limit_timed', limit_wait_minutes=5, timeout_action='market')]))
    assert e.ledger.fills[0].price == D(expected)


def test_timed_limit_elapsed_clock_missing_quotes_and_target_callback():
    e = make_engine()
    first = mbar('F', '09:31', 100, 101, 100, 100)
    e.step(Frame(first.end, {'F': first}, frozenset({'F'})), lambda ctx: ctx.target(
        'F', 5, price_model='limit_timed', limit_price=99,
        limit_wait_minutes=5, timeout_action='market'))
    # No observations until 09:40. Timeout is independent of retry count.
    later = mbar('F', '09:41', 100, 102, 100, 100)
    e.step(Frame(later.end, {'F': later}, frozenset({'F'})))
    assert e.ledger.fills[0].price == D(101)
    assert e.ledger.fills[0].time == later.start


@pytest.mark.parametrize('params', [dict(limit_wait_minutes=0, timeout_action='market'),
    dict(limit_wait_minutes=True, timeout_action='market'),
    dict(limit_wait_minutes=5, timeout_action='unknown'), dict(limit_wait_minutes=5)])
def test_timed_limit_validation(params):
    with pytest.raises(ValueError):
        Instruction('F', 1, 5, price_model='limit_timed', limit_price=99, **params)


def test_timed_limit_rejects_bar_crossing_deadline():
    e = make_engine()
    first = mbar('F', '09:31', 100, 101, 100, 100)
    e.submit([Instruction('F', 1, 5, limit_price=99, price_model='limit_timed',
                         limit_wait_minutes=5, timeout_action='market')], first.start)
    coarse = Bar('F', first.start, first.end + timedelta(minutes=5), DAY,
                 D(100), D(102), D(98), D(100), 100)
    with pytest.raises(ValueError, match='straddles'):
        e.step(Frame(coarse.end, {'F': coarse}, frozenset({'F'})))


def test_timed_cancel_without_quotes():
    e = make_engine()
    first = mbar('F', '09:31', 100, 101, 100, 100)
    e.step(Frame(first.end, {'F': first}, frozenset({'F'})), lambda ctx: ctx.submit([
        Instruction('F', 1, 5, limit_price=99, price_model='limit_timed',
                    limit_wait_minutes=5, timeout_action='cancel')]))
    e.step(Frame(first.end + timedelta(minutes=5), {}, frozenset()))
    assert e.operations[0].state == 'cancelled'
    assert not e.ledger.fills


@pytest.mark.parametrize('action,expected', [('market', 1), ('cancel', 0)])
def test_timed_limit_auction_at_deadline(action, expected):
    from backtest.models import Auction
    e = make_engine()
    first = mbar('F', '09:31', 100, 101, 100, 100)
    e.submit([Instruction('F', 1, 5, limit_price=99, price_model='limit_timed',
                         limit_wait_minutes=5, timeout_action=action)], first.end)
    at = first.end + timedelta(minutes=5)
    auction = Auction('F', at, DAY, D(101), 100)
    e.step(Frame(at, {}, frozenset({'F'}), auctions={'F': auction}))
    assert len(e.ledger.fills) == expected
    if expected:
        assert e.ledger.fills[0].price == D(101)


@pytest.mark.parametrize('action,expected', [('market', 1), ('cancel', 0)])
def test_precomputed_target_forwards_timed_parameters(action, expected):
    from backtest.cli import demo_config
    cfg = demo_config()
    cfg['instructions'] = [dict(at=cfg['frames'][0]['bars'][0]['start'],
        instrument='A', quantity=1, price_model='limit_timed', limit_price='1',
        limit_wait_minutes=1, timeout_action=action)]
    e = run_scenario(cfg)
    assert len(e.ledger.fills) == expected
    assert e.operations[0].state == ('completed' if expected else 'cancelled')
    if expected:
        fill = e.ledger.fills[0]
        assert fill.time.isoformat() == cfg['frames'][1]['bars'][0]['start']
        assert fill.instruction.limit_wait_minutes == 1
