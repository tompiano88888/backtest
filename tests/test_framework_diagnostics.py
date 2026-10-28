"""Diagnostics added around the unified engine: nothing here changes matching or accounting.

Warnings must reach the strategy callback (ctx.warnings) exactly once, with a reason code, so
research code can design its own response instead of discovering a stuck order afterwards.
"""
import json
import subprocess
import sys
from datetime import date, datetime, timezone
from decimal import Decimal as D

import pytest

from backtest.cli import demo_config
from backtest.engine import Engine
from backtest.accounting import Ledger
from backtest.models import Bar
from backtest.runner import result, run_scenario, strategy_identity
from backtest.storage import Store

TZ = "+08:00"
D1, D2, D3, D4, D5 = "2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08"


def bar(inst, day, o, h, l, c, settle, suspended=False):
    return dict(instrument=inst, start=f"{day}T09:00:00{TZ}", end=f"{day}T15:00:00{TZ}", trading_date=day,
                open=o, high=h, low=l, close=c, volume=1000, settlement_price=settle,
                upper_limit="1000", lower_limit="1", suspended=suspended)


def frame(day, bars, settle_ids):
    return dict(time=f"{day}T15:00:00{TZ}", bars=bars, open_instruments=[b["instrument"] for b in bars],
                settle_instruments=settle_ids,
                settlements=[dict(instrument=b["instrument"], trading_date=day, settlement_price=b["settlement_price"])
                             for b in bars if b["instrument"] in settle_ids])


def scenario(frames, instructions, slip=0, delivery_a="2030-01-01", **extra):
    row = dict(asset="future", account="futures", exchange="SHFE", product="AU", multiplier="1000", tick="1")
    return dict(initial_cash="1000000",
                instruments=[dict(id="A", expiry=delivery_a, delivery=delivery_a, **row),
                             dict(id="B", expiry="2030-01-01", delivery="2030-01-01", **{**row, "product": "AG"})],
                margin_rules={i: dict(model="future", rate="0.1") for i in "AB"},
                execution=dict(participation="1", slippage_ticks=slip),
                instructions=instructions, frames=frames, **extra)


def buy(day, instrument="A", effect="open"):
    return dict(at=f"{day}T15:00:00{TZ}", legs=[dict(instrument=instrument, side=1, quantity=1, effect=effect)])


def recorder():
    seen = {}

    def on_bar(ctx):
        seen[ctx.time.date().isoformat()] = [(w["code"], w.get("reason")) for w in ctx.warnings]

    on_bar.seen = seen
    return on_bar


def test_unfilled_then_expired_orders_reach_callback_with_reasons():
    # open + 1 tick of slippage lies above the bar high: the order cannot fill and (one-shot) expires.
    cfg = scenario([frame(D1, [bar("A", D1, "100", "101", "99", "100", "100")], ["A"]),
                    frame(D2, [bar("A", D2, "102", "102", "100", "101", "101")], ["A"]),
                    frame(D3, [bar("A", D3, "101", "102", "100", "101", "101")], ["A"])],
                   [buy(D1)], slip=1)
    callback = recorder()
    engine = run_scenario(cfg, callback)
    # Behaviour is unchanged: the order is dropped and nothing is held.
    assert [o.state for o in engine.operations] == ["expired"]
    assert engine.ledger.lots == []
    # The failed attempt is known in the same frame's callback; the expiry arrives at the next one.
    assert callback.seen[D1] == []
    assert callback.seen[D2] == [("ORDER_UNFILLED", "PRICE_OUTSIDE_BAR")]
    assert callback.seen[D3] == [("ORDER_EXPIRED", "PRICE_OUTSIDE_BAR")]
    unfilled = next(w for w in engine.warnings if w["code"] == "ORDER_UNFILLED")
    assert (unfilled["instrument"], unfilled["requested"], unfilled["filled"], unfilled["remaining"]) == ("A", 1, 0, 1)
    expired = next(w for w in engine.warnings if w["code"] == "ORDER_EXPIRED")
    assert expired["legs"] == [dict(instrument="A", side=1, effect="open", group="standalone", quantity=1)]


def test_pending_order_warns_once_per_reason_not_every_frame():
    # No settlement instruments, so the order stays pending across suspended frames.
    cfg = scenario([frame(D1, [bar("A", D1, "100", "101", "99", "100", "100")], []),
                    frame(D2, [bar("A", D2, "100", "101", "99", "100", "100", True)], []),
                    frame(D3, [bar("A", D3, "100", "101", "99", "100", "100", True)], []),
                    frame(D4, [bar("A", D4, "100", "101", "99", "100", "100")], [])],
                   [buy(D1)])
    engine = run_scenario(cfg)
    assert [w["reason"] for w in engine.warnings if w["code"] == "ORDER_UNFILLED"] == ["SUSPENDED"]
    assert len(engine.ledger.lots) == 1  # filled on the first tradable frame after the suspension


def test_every_engine_warning_is_delivered_once_in_order():
    delivered = []

    def on_bar(ctx):
        delivered.extend(dict(w) for w in ctx.warnings)

    cfg = demo_config()
    engine = run_scenario(cfg, on_bar)
    assert delivered, "demo emits combination warnings"
    assert any(w["code"] == "COMBINATION_INCOMPLETE" for w in delivered)
    # Exactly once and in order: a prefix of the engine list (later ones had no further callback).
    assert delivered == engine.warnings[:len(delivered)]


def held_future_without_quotes(stale):
    frames = [frame(D1, [bar("A", D1, "100", "101", "99", "100", "100"), bar("B", D1, "10", "11", "9", "10", "10")], ["A", "B"]),
              frame(D2, [bar("A", D2, "100", "101", "99", "100", "100"), bar("B", D2, "10", "11", "9", "10", "10")], ["A", "B"])]
    for day in (D3, D4, D5):  # A has no bar from its delivery day on
        frames.append(frame(day, [bar("B", day, "10", "11", "9", "10", "10")], ["B"]))
    return scenario(frames, [buy(D1), buy(D2, effect="close")], delivery_a=D3, stale_quote_days=stale)


def test_past_delivery_and_stale_quote_reach_callback_and_residual():
    cfg = held_future_without_quotes(2)
    callback = recorder()
    engine = run_scenario(cfg, callback)
    # Accounting is untouched: the position is still held at its last mark, as before.
    assert [(p.instrument, p.quantity) for p in engine.ledger.lots] == [("A", 1)]
    # Delivery day itself is not "past"; the next frame flags it, together with the second quote-less day.
    assert callback.seen[D3] == []
    assert sorted(code for code, _ in callback.seen[D4]) == ["POSITION_PAST_DELIVERY", "STALE_QUOTE"]
    assert callback.seen[D5] == []  # each condition is reported once per episode
    stale = next(w for w in engine.warnings if w["code"] == "STALE_QUOTE")
    assert (stale["instrument"], stale["last_quote_day"], stale["trading_days_without_quote"]) == ("A", D2, 2)
    past = next(w for w in engine.warnings if w["code"] == "POSITION_PAST_DELIVERY")
    assert (past["instrument"], past["delivery"], past["quantity"]) == ("A", D3, 1)
    # run_scenario finalizes: still-open exposure and the never-attempted close order are reported.
    codes = [w["code"] for w in engine.warnings]
    assert {"PENDING_OPERATION_AT_END", "POSITION_PAST_DELIVERY_AT_END", "STALE_QUOTE_AT_END"} <= set(codes)
    residual = result(engine, cfg)["residual"]
    assert residual["past_delivery"] == ["A"] and residual["stale_quotes"] == ["A"]
    assert [p["instrument"] for p in residual["open_positions"]] == ["A"]
    assert [o["legs"][0]["effect"] for o in residual["pending_operations"]] == ["close"]
    assert residual["pending_operations"][0]["after_last_frame"] is False
    json.dumps(residual)  # must stay serializable for result.json


def test_stale_quote_threshold_is_configurable_and_can_be_disabled():
    engine = run_scenario(held_future_without_quotes(None))
    assert not any(w["code"] == "STALE_QUOTE" for w in engine.warnings)
    assert any(w["code"] == "POSITION_PAST_DELIVERY" for w in engine.warnings)
    for bad in (0, -1, 1.5, "3"):
        with pytest.raises(ValueError):
            Engine(Ledger({}, 1), {}, stale_quote_days=bad)


def test_order_submitted_by_last_callback_is_residual_but_not_a_warning():
    cfg = scenario([frame(D1, [bar("A", D1, "100", "101", "99", "100", "100")], ["A"])], [])

    def on_bar(ctx):
        ctx.target("A", 1)

    engine = run_scenario(cfg, on_bar)
    assert not any(w["code"] == "PENDING_OPERATION_AT_END" for w in engine.warnings)
    pending = engine.residual()["pending_operations"]
    assert len(pending) == 1 and pending[0]["after_last_frame"] is True


def test_strategy_identity_distinguishes_parameters_and_hashes_source():
    def make(fast):
        def on_bar(ctx):
            return fast
        on_bar.strategy_params = dict(fast=fast, slow=20)
        return on_bar

    one, two = strategy_identity(make(5)), strategy_identity(make(10))
    assert one["params"] == dict(fast=5, slow=20) and two["params"] == dict(fast=10, slow=20)
    assert one["source_sha256"] == two["source_sha256"] and len(one["source_sha256"]) == 64
    assert one["name"].endswith("on_bar") and one["module"] == __name__
    assert strategy_identity(None) is None
    # explicit override, e.g. for callable objects or when the closure cannot expose attributes
    explicit = strategy_identity(make(5), name="sma", params=dict(fast=3))
    assert (explicit["name"], explicit["params"]) == ("sma", dict(fast=3))
    with pytest.raises(TypeError):
        strategy_identity(make(5), params=dict(bad=object()))


def test_result_provenance_records_strategy_and_runs_differ_only_by_it():
    cfg = scenario([frame(D1, [bar("A", D1, "100", "101", "99", "100", "100")], ["A"])], [])

    def run(fast):
        def on_bar(ctx):
            pass
        on_bar.strategy_params = dict(fast=fast)
        return result(run_scenario(cfg, on_bar), cfg)["provenance"]

    first, second = run(5), run(10)
    assert first["config_sha256"] == second["config_sha256"] and first["code_sha256"] == second["code_sha256"]
    assert first["strategy"]["params"] == dict(fast=5) and second["strategy"]["params"] == dict(fast=10)
    assert result(run_scenario(cfg), cfg)["provenance"]["strategy"] is None
    named = result(run_scenario(cfg, strategy=dict(name="instruction-flow", params=dict(file="x"))), cfg)
    assert named["provenance"]["strategy"]["name"] == "instruction-flow"


def test_unserializable_strategy_params_fail_before_any_frame_runs():
    cfg = scenario([frame(D1, [bar("A", D1, "100", "101", "99", "100", "100")], ["A"])], [])
    calls = []

    def on_bar(ctx):
        calls.append(ctx.time)

    on_bar.strategy_params = dict(bad=object())
    with pytest.raises(TypeError):
        run_scenario(cfg, on_bar)
    assert calls == []


def sample_bar():
    start = datetime(2024, 1, 2, 9, tzinfo=timezone.utc)
    return Bar("A", start, start.replace(hour=15), date(2024, 1, 2), D(1), D(2), D(1), D(2), 10, D(2))


def test_read_only_store_reads_but_never_writes(tmp_path):
    with Store(tmp_path) as writer:
        version = writer.publish([sample_bar()], "test", "1d")
    with Store(tmp_path, read_only=True) as first, Store(tmp_path, read_only=True) as second:
        assert first.latest() == second.latest() == version
        assert [b.instrument for b in first.bars(version, {"A"}, datetime(2024, 1, 1, tzinfo=timezone.utc),
                                                 datetime(2024, 1, 3, tzinfo=timezone.utc), "1d")] == ["A"]
        with pytest.raises(PermissionError):
            first.publish([sample_bar()], "test", "1d")
        with pytest.raises(PermissionError):
            first.put_metadata("k", "x", datetime(2024, 1, 1, tzinfo=timezone.utc),
                               datetime(2024, 1, 1, tzinfo=timezone.utc), {})
    missing = tmp_path / "absent"
    with pytest.raises(FileNotFoundError):
        Store(missing, read_only=True)
    assert not missing.exists()  # a read-only open must not create a store as a side effect


def test_two_processes_can_replay_one_snapshot_concurrently(tmp_path):
    with Store(tmp_path) as writer:
        writer.publish([sample_bar()], "test", "1d")
    code = ("import sys,time;from backtest.storage import Store;"
            "s=Store(sys.argv[1],read_only=True);print('open',flush=True);time.sleep(4);print(s.latest() is not None)")
    holder = subprocess.Popen([sys.executable, "-c", code, str(tmp_path)], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "open"
        with Store(tmp_path, read_only=True) as other:  # a second reader while the first still holds the file
            assert other.latest() is not None
    finally:
        holder.communicate(timeout=30)
    assert holder.returncode == 0


def test_standard_report_directory_contains_only_the_report(tmp_path, monkeypatch):
    from backtest.performance import export_performance
    monkeypatch.delenv("MPLCONFIGDIR", raising=False)
    rows = [dict(day="2024-01-02", equity="1000"), dict(day="2024-01-03", equity="1010"),
            dict(day="2024-01-04", equity="1005")]
    export_performance(rows, "1000", "2024-01-02", tmp_path)
    names = sorted(p.name for p in (tmp_path / "standard_report").iterdir())
    assert names == ["annual.csv", "daily.csv", "metrics.json", "performance.png"]
    import os
    assert "MPLCONFIGDIR" not in os.environ  # the helper restores the environment


def test_caller_matplotlib_config_dir_is_respected(tmp_path, monkeypatch):
    from backtest.performance import export_performance
    cache = tmp_path / "mpl"
    cache.mkdir()
    monkeypatch.setenv("MPLCONFIGDIR", str(cache))
    rows = [dict(day="2024-01-02", equity="1000"), dict(day="2024-01-03", equity="1010")]
    export_performance(rows, "1000", "2024-01-02", tmp_path / "out")
    import os
    assert os.environ["MPLCONFIGDIR"] == str(cache)
    assert not (tmp_path / "out" / "standard_report" / ".matplotlib").exists()


def test_annual_reconciliation_is_a_real_exception_not_an_assert():
    import inspect
    from backtest import performance
    source = inspect.getsource(performance.statistics)
    assert "assert " not in source and "does not reconcile" in source
