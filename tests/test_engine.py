from datetime import datetime, timedelta, timezone, date

import pytest

from backtest.accounting import Ledger
from backtest.engine import Engine, ExecutionConfig, Frame
from backtest.models import Bar, D, Instruction, MarginRule
from test_accounting import future, option, trade

T = datetime(2026, 9, 21, 1, tzinfo=timezone.utc)


def frame(n, missing=(), volumes=None, open_ids=None):
    bars = {}
    for i in ("A", "B", "C"):
        if i not in missing:
            bars[i] = Bar(i, T + timedelta(minutes=n-1), T + timedelta(minutes=n), T.date(), D(100), D(110), D(90), D(101), (volumes or {}).get(i, 100))
    return Frame(T + timedelta(minutes=n), bars, frozenset(open_ids if open_ids is not None else ("A", "B", "C")))


def engine():
    inst = {i: future(i) for i in ("A", "B", "C")}
    return Engine(Ledger(inst, 1), {i: MarginRule("future", D("0.1")) for i in inst}, ExecutionConfig(participation=D(1)))


def test_partial_combination_three_bars_then_unwind_fourth():
    e = engine()
    op = e.submit([Instruction("A", 1, 2, group="pair"), Instruction("B", -1, 2, group="pair")], T, True)
    for n in (1, 2, 3):
        e.step(frame(n, missing=("B",)))
    assert op.state == "unwinding"
    assert e.ledger.quantity("A", 1, "pair") == 2
    assert len(e.ledger.fills) == 1
    e.step(frame(4, missing=("B",)))
    assert op.state == "unwound"
    assert not e.ledger.lots
    assert e.ledger.fills[-1].time == T + timedelta(minutes=3)
    assert len([w for w in e.warnings if w["code"] == "COMBINATION_INCOMPLETE"]) == 3


def test_partial_retries_only_remaining_quantity_and_shared_liquidity():
    e = engine()
    op = e.submit([Instruction("A", 1, 3, group="pair"), Instruction("B", -1, 3, group="pair")], T, True)
    e.step(frame(1, volumes={"A": 1, "B": 2}))
    e.step(frame(2))
    assert op.state == "completed"
    assert e.ledger.quantity("A", 1, "pair") == 3
    assert e.ledger.quantity("B", -1, "pair") == 3
    assert any(w["code"] == "COMBINATION_INCOMPLETE" for w in e.warnings)


def test_closed_market_does_not_consume_retry():
    e = engine()
    op = e.submit([Instruction("A", 1, 1, group="pair"), Instruction("B", -1, 1, group="pair")], T, True)
    e.step(frame(1, missing=("B",), open_ids=("A",)))
    assert op.attempts == 0
    assert not e.ledger.fills


def test_callback_cannot_fill_current_bar():
    e = engine()
    e.step(frame(1), lambda ctx: ctx.submit([Instruction("A", 1, 1)]))
    assert not e.ledger.fills
    e.step(frame(2))
    assert len(e.ledger.fills) == 1
    assert e.ledger.fills[0].time == T + timedelta(minutes=1)


def test_close_failure_never_reopens_completed_legs():
    e = engine()
    trade(e.ledger, "A", 1, 1, 100, group="pair")
    trade(e.ledger, "B", -1, 1, 100, group="pair")
    op = e.submit([Instruction("A", 1, 1, "close", "pair"), Instruction("B", -1, 1, "close", "pair")], T, True)
    for n in (1, 2, 3):
        e.step(frame(n, missing=("B",)))
    assert e.ledger.quantity("A", 1, "pair") == 0
    assert op.state == "unwinding"
    e.step(frame(4))
    assert not e.ledger.lots
    assert op.state == "unwound"


def test_missing_daily_settlement_fails():
    e = engine()
    trade(e.ledger, "A", 1, 1, 100)
    f = frame(1)
    with pytest.raises(ValueError, match="Missing settlement"):
        e.step(Frame(f.time, f.bars, f.open_instruments, frozenset({"A"})))


def test_option_weight_not_accepted():
    e = Engine(Ledger({"O": option()}, 1000), {})
    with pytest.raises(ValueError, match="Option weights"):
        e.target_weight("O", "0.1", T)


def test_roll_explicit_switch_and_later_expiry():
    a = future("A", date(2026, 9, 23))
    b = future("B", date(2026, 10, 23))
    e = Engine(Ledger({"A": a, "B": b}, 10000), {i: MarginRule("future", D("0.1")) for i in ("A", "B")}, ExecutionConfig(participation=D(1)))
    trade(e.ledger, "A", 1, 2, 100, group="holding")
    assert e.roll("holding", {"P": "B"}, T) is None
    op = e.roll("holding", {"P": "B"}, T, enabled=True)
    f = frame(1, missing=("C",))
    e.step(f)
    assert op.state == "completed"
    assert e.ledger.quantity("A", 1) == 0
    assert e.ledger.quantity("B", 1) == 2


def test_shared_volume_budget_and_global_close_priority():
    e = engine()
    trade(e.ledger, "A", 1, 1, 100, group="existing")
    opening = e.submit([Instruction("A", 1, 1, group="new")], T)
    closing = e.submit([Instruction("A", 1, 1, "close", "existing")], T)
    e.step(frame(1, volumes={"A": 1}))
    assert closing.state == "completed"
    assert opening.state == "pending"
    assert e.ledger.quantity("A", 1) == 0


def test_three_leg_combination_completes_on_third_bar():
    e = engine()
    op = e.submit([Instruction("A", 1, 1, group="triple"), Instruction("B", -1, 2, group="triple"), Instruction("C", 1, 3, group="triple")], T, True)
    e.step(frame(1, missing=("C",)))
    e.step(frame(2, missing=("C",)))
    e.step(frame(3))
    assert op.state == "completed"
    assert len(e.ledger.fills) == 3
    assert e.groups["triple"]["status"] == "holding"


def test_limit_locked_leg_cannot_fill():
    from dataclasses import replace
    e = engine()
    op = e.submit([Instruction("A", 1, 1)], T)
    f = frame(1)
    f.bars["A"] = replace(f.bars["A"], upper_limit=D(100))
    e.step(f)
    assert op.state == "pending"
    assert not e.ledger.fills


def test_callback_and_precomputed_instruction_have_same_results():
    a, b = engine(), engine()
    a.step(frame(1))
    a.submit([Instruction("A", 1, 2)], T + timedelta(minutes=1))
    b.step(frame(1), lambda ctx: ctx.submit([Instruction("A", 1, 2)]))
    a.step(frame(2))
    b.step(frame(2))
    assert a.ledger.fills == b.ledger.fills
    assert a.ledger.cash == b.ledger.cash


def test_end_of_day_callback_order_survives_for_next_session():
    e = engine()
    f = frame(1)
    e.step(Frame(f.time, f.bars, f.open_instruments, frozenset({"A"})),
           on_day_end=lambda ctx: ctx.submit([Instruction("A", 1, 1)]))
    assert e.operations[-1].state == "pending"
    e.step(frame(2))
    assert e.operations[-1].state == "completed"


def test_callback_position_is_read_only():
    e = engine()
    trade(e.ledger, "A", 1, 1, 100)
    def callback(ctx):
        with pytest.raises(TypeError):
            ctx.positions[0]["quantity"] = 999
        with pytest.raises(TypeError):
            ctx.marks["A"] = D(999)
    e.step(frame(1), callback)
    assert e.ledger.quantity("A", 1) == 1


def test_end_valuation_does_not_force_close():
    from backtest.cli import demo_config
    from backtest.runner import run_scenario, result
    cfg = demo_config()
    cfg["frames"] = cfg["frames"][:2]
    e = run_scenario(cfg)
    report = result(e, cfg)
    assert len(report["positions"]) == 1
    assert report["operations"][0]["state"] == "pending"
