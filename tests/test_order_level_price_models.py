"""Order-level price models chosen by the strategy callback (Instruction.price_model / target*).

Hand-checkable: mixed models inside one callback, per-order override of the configured default,
signal-price quotation + offset (frozen limit_price), turnover/volume average with OHLC/4
fallback, minute 50% / daily 10% shared capacity, persistent targets, confirmation through
ctx.execution_details_since and the disclosure in the run result / standard report.
"""
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
                  config or ExecutionConfig(participation=None, slippage_ticks=1))


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


# ---------------------------------------------------------------- one callback, several models

def test_different_order_types_use_different_models_and_callback_sees_confirmation():
    e = make_engine(codes=("F", "G", "H"))
    first = {c: mbar(c, "09:31", 100, 101, 99, 100) for c in "FGH"}
    second = {"F": mbar("F", "09:32", "100.5", 103, 99, 102),                       # open <= quote 101: open
              "G": mbar("G", "09:32", 100, 102, 99, 101, volume=100, turnover=100 * 10 * 100.8),   # vwap 100.8
              "H": mbar("H", "09:32", 100, 105, 95, 101)}                            # configured default: open+1 tick

    def orders(ctx):
        ctx.submit([lim("F", 10, 101)])                                              # signal 100 + 1 tick
        ctx.submit([Instruction("G", 1, 10, price_model="bar_average")])
        ctx.submit([Instruction("H", 1, 10)])

    seen = run(e, [first, second], orders)
    prices = {f.instruction.instrument: f.price for f in e.ledger.fills}
    assert prices == {"F": D("100.5"), "G": D("100.8"), "H": D(101)}
    methods = {d["price_model"]: (d["method"], d["price_model_source"]) for d in seen[1]["details"]}
    assert methods == {"limit_open_then_market": ("limit_open", "order"), "bar_average": ("bar_vwap", "order")}
    assert len(seen[1]["fills"]) == 3          # confirmation arrives in the frame the fills happened
    rows = {(r["price_model"], r["source"]): r for r in e.execution_report()["modes"]}
    assert set(rows) == {("limit_open_then_market", "order"), ("bar_average", "order"), ("next_open", "config")}
    assert rows[("bar_average", "order")]["fills"] == 1 and rows[("next_open", "config")]["filled_quantity"] == 10


def test_order_model_overrides_configured_default_both_ways():
    cfg = ExecutionConfig(participation=None, slippage_ticks=1, price_model="bar_average")
    e = make_engine(cfg)
    run(e, [{"F": mbar("F", "09:31", 100, 101, 99, 100)},
            {"F": mbar("F", "09:32", 100, 105, 95, 101, turnover=100 * 10 * 102)}],
        lambda ctx: ctx.submit([Instruction("F", 1, 10, price_model="next_open")]))
    assert e.ledger.fills[0].price == D(101)                                   # open + 1 tick, not the 102 average
    cfg = ExecutionConfig(participation=None, slippage_ticks=1, price_model="limit_open_then_market")
    e = make_engine(cfg)
    run(e, [{"F": mbar("F", "09:31", 100, 101, 99, 100)}, {"F": mbar("F", "09:32", 100, 105, 95, 101, turnover=100 * 10 * 102)}],
        lambda ctx: ctx.submit([Instruction("F", 1, 10, price_model="bar_average")]))   # no frozen quotation needed
    assert e.ledger.fills[0].price == D(102)


# -------------------------------------------- signal price + offset (frozen quotation, next Bar)

@pytest.mark.parametrize("bar_args, price, time_is_start, method", [
    (("100.5", 103, 99, 102), "100.5", True, "limit_open"),       # open not worse than quote -> open
    ((103, 104, "100.8", 102), "101", False, "limit_open"),       # open worse, bar reaches quote -> quote, inside OHLC
    ((100, 100, 100, 100), "100", True, "limit_open"),            # quote above the whole bar -> open, never above high
])
def test_buy_signal_quotation_next_bar(bar_args, price, time_is_start, method):
    e = make_engine()
    first = {"F": mbar("F", "09:31", 100, 101, 99, 100)}
    nxt = mbar("F", "09:32", *bar_args)
    run(e, [first, {"F": nxt}], lambda ctx: ctx.submit([lim("F", 10, 101)]))
    fill = e.ledger.fills[0]
    assert fill.price == D(price) and nxt.low <= fill.price <= nxt.high
    assert fill.time == (nxt.start if time_is_start else nxt.end)
    assert e.execution_details[0]["method"] == method and e.execution_details[0]["limit_price"] == D(101)


def test_sell_signal_quotation_mirrors_buy_and_unreached_quote_does_not_fill_that_bar():
    e = make_engine()
    e.ledger.apply(Fill(datetime(2026, 1, 2, 9, 31, tzinfo=TZ), date(2026, 1, 2),
                        Instruction("F", 1, 10, group="g"), D(100), D(0)))
    first = {"F": mbar("F", "09:31", 100, 101, 99, 100)}
    run(e, [first, {"F": mbar("F", "09:32", 98, "99.4", 97, 98)}],           # sell quote 99, open 98 < 99, high 99.4 reaches
        lambda ctx: ctx.submit([lim("F", 10, 99, side=1, effect="close", group="g")]))
    assert e.ledger.fills[-1].price == D(99)
    e2 = make_engine()
    e2.ledger.apply(Fill(datetime(2026, 1, 2, 9, 31, tzinfo=TZ), date(2026, 1, 2),
                         Instruction("F", 1, 10, group="g"), D(100), D(0)))
    seen = run(e2, [first, {"F": mbar("F", "09:32", 98, "98.5", 97, 98)}],   # high 98.5 < 99: not reached
               lambda ctx: ctx.submit([lim("F", 10, 99, side=1, effect="close", group="g")]))
    assert len(e2.ledger.fills) == 1 and ("ORDER_UNFILLED", "LIMIT_PRICE_NOT_TOUCHED") in seen[1]["warnings"]


# ------------------------------------------------------------------ average price and capacity

@pytest.mark.parametrize("turnover, expected, method", [
    (100 * 10 * 101, "101", "bar_vwap"),
    (100 * 10 * 120, "100.25", "bar_ohlc4_invalid_vwap"),       # 120 outside [99, 101.5]
    (None, "100.25", "bar_ohlc4"),
])
def test_bar_average_per_order_with_ohlc4_fallback_and_no_tick(turnover, expected, method):
    e = make_engine(ExecutionConfig(participation=None, slippage_ticks=3))
    run(e, [{"F": mbar("F", "09:31", 100, 101, 99, 100)},
            {"F": mbar("F", "09:32", 100, "101.5", 99, "100.5", turnover=turnover)}],
        lambda ctx: ctx.submit([Instruction("F", 1, 10, price_model="bar_average")]))
    assert e.ledger.fills[0].price == D(expected) and e.execution_details[0]["method"] == method


@pytest.mark.parametrize("model", ["bar_average", "limit_open_then_market"])
def test_minute_cap_is_50_percent_and_daily_cap_10_percent_shared_per_instrument(model):
    def order(qty):
        return (Instruction("F", 1, qty, price_model="bar_average") if model == "bar_average"
                else lim("F", qty, 101))
    e = make_engine()
    run(e, [{"F": mbar("F", "09:31", 100, 101, 99, 100)}, {"F": mbar("F", "09:32", 100, 101, 99, 100, volume=100)}],
        lambda ctx: ctx.submit([order(30), order(30)]))                       # two orders share one 50-lot budget
    assert sum(f.instruction.quantity for f in e.ledger.fills) == 50
    e = make_engine()
    run(e, [{"F": dbar("F", "2026-01-05", 100, 101, 99, 100)}, {"F": dbar("F", "2026-01-06", 100, 101, 99, 100, volume=100)}],
        lambda ctx: ctx.submit([order(30)]))
    assert sum(f.instruction.quantity for f in e.ledger.fills) == 10          # daily Bar: 10%


# -------------------------------------------------------------------------- targets and validation

def target_scenario(model, limit):
    e = make_engine()
    state = {"n": 0}

    def on_bar(ctx):
        state["n"] += 1
        if state["n"] == 1:
            ctx.target("F", 30, price_model=model, limit_price=limit)

    days = ["2026-01-05", "2026-01-06", "2026-01-07"]
    vols = [100, 100, 100]
    for day, vol in zip(days, vols):
        b = dbar("F", day, 100, 101, 99, 100, volume=vol)
        e.step(Frame(b.end, {"F": b}, frozenset({"F"}), frozenset({"F"}),
                     (("F", b.trading_date, D(100)),)), on_bar)
    return e


def test_persistent_average_target_keeps_going_but_frozen_limit_target_is_not_resubmitted():
    avg = target_scenario("bar_average", None)
    assert sum(f.instruction.quantity for f in avg.ledger.fills) >= 20 and len(avg.operations) >= 2
    assert all(f.instruction.price_model == "bar_average" for f in avg.ledger.fills)
    frozen = target_scenario("limit_open_then_market", D(101))
    assert len(frozen.operations) == 1                                       # stale quotation never re-sent
    assert sum(f.instruction.quantity for f in frozen.ledger.fills) == 10    # day-2 cap, remainder expired


def test_invalid_order_models_are_rejected():
    with pytest.raises(ValueError):
        Instruction("F", 1, 1, price_model="market")
    with pytest.raises(ValueError):
        Instruction("F", 1, 1, price_model="limit_open_then_market")                       # needs a quotation
    with pytest.raises(ValueError):
        Instruction("F", 1, 1, limit_price=100, price_model="bar_average")                # average is market intent
    with pytest.raises(ValueError):
        Instruction("F", 1, 1, limit_price=100, price_model="previous_close_then_open")
    Instruction("F", 1, 1, limit_price=100, price_model="next_open")
    e = make_engine()
    with pytest.raises(ValueError):
        e.target("F", 1, datetime(2026, 1, 5, 9, 31, tzinfo=TZ), price_model="limit_open_then_market")
    with pytest.raises(ValueError, match="close_locked"):
        e.target("F", 1, datetime(2026, 1, 5, 9, 31, tzinfo=TZ), price_model="locked_close_average")


def test_combination_timeout_exit_falls_back_to_configured_model_with_warning():
    e = make_engine(codes=("A", "B"))
    start = mbar("A", "09:31", 100, 101, 99, 100).end
    e.submit([Instruction("A", 1, 10, group="g", price_model="bar_average"),
              Instruction("B", -1, 10, group="g", price_model="bar_average")], start, combination=True)
    for hm in ["09:32", "09:33", "09:34", "09:35"]:
        bar = mbar("A", hm, 100, 101, 99, 100)
        e.step(Frame(bar.end, {"A": bar}, frozenset("AB")))
    codes = [w["code"] for w in e.warnings]
    assert "COMBINATION_TIMEOUT" in codes and "UNWIND_USES_CONFIGURED_PRICE_MODEL" in codes


# -------------------------------------------------------------------------- run result / report

def scenario():
    row = dict(asset="future", account="futures", exchange="SHFE", product="AU", multiplier="1", tick="1")

    def bar(day, o, h, l, c, turnover):
        return dict(instrument="A", start=f"{day}T09:00:00+08:00", end=f"{day}T15:00:00+08:00", trading_date=day,
                    open=o, high=h, low=l, close=c, volume=1000, turnover=turnover, settlement_price=c)

    frames = []
    for day, b in [("2026-01-05", bar("2026-01-05", "100", "101", "99", "100", "100000")),
                   ("2026-01-06", bar("2026-01-06", "101", "103", "100", "102", "101500")),
                   ("2026-01-07", bar("2026-01-07", "102", "104", "101", "103", "103000"))]:
        frames.append(dict(time=f"{day}T15:00:00+08:00", bars=[b], open_instruments=["A"], settle_instruments=["A"],
                           settlements=[dict(instrument="A", trading_date=day, settlement_price=b["settlement_price"])]))
    return dict(initial_cash="1000000", instruments=[dict(id="A", expiry="2030-01-01", delivery="2030-01-01", **row)],
                margin_rules={"A": dict(model="future", rate="0.1")}, execution=dict(slippage_ticks=1), frames=frames)


def test_result_and_standard_report_disclose_order_level_models(tmp_path):
    state = {"n": 0}

    def callback(ctx):
        state["n"] += 1
        if state["n"] == 1:
            ctx.submit([Instruction("A", 1, 50, price_model="limit_open_then_market", limit_price=D(101))])
        elif state["n"] == 2:
            ctx.submit([Instruction("A", 1, 20, "close", price_model="bar_average")])

    engine = run_scenario(scenario(), callback, output=tmp_path / "run.json")
    payload = json.loads((tmp_path / "run.json").read_text(encoding="utf-8"))
    rows = {(r["price_model"], r["source"]): r for r in payload["execution_modes"]["modes"]}
    assert rows[("limit_open_then_market", "order")]["fills"] == 1 and rows[("bar_average", "order")]["fills"] == 1
    assert payload["execution_modes"]["default_config"]["price_model"] == "next_open"
    assert "callback_selected_order_level_price_models" in payload["assumptions"]
    assert [d["price_model"] for d in payload["execution_details"]] == ["limit_open_then_market", "bar_average"]
    assert D(payload["fills"][1]["price"]) == 103         # day-3 turnover 103000 / volume 1000
    report = tmp_path / "run" / "standard_report"
    assert json.loads((report / "execution_modes.json").read_text(encoding="utf-8"))["modes"]
    assert json.loads((report / "metrics.json").read_text(encoding="utf-8"))["execution_modes"]["selection"]
    assert (report / "performance.png").read_bytes().startswith(b"\x89PNG")
    assert engine.ledger.quantity("A", 1) == 30
