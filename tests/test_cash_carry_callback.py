from copy import deepcopy
from datetime import date
import json

import pytest

from backtest.cash_carry_callback import CashCarryCallback, ParameterSchedule, export_parameters, run
from backtest.runner import engine_from_config, run_scenario


def parameters():
    return dict(schema_version=1, parameters=[dict(product="IF", decision_day="2020-01-02", window_start="2019-01-03", window_end="2020-01-02", entry_threshold=".01", exit_threshold="0")])


def scenario():
    instruments = [dict(id="IF2001.CFE", asset="future", account="futures", exchange="CFE", product="IF", multiplier="10", tick="1", expiry="2020-01-17", delivery="2020-01-17"),
                   dict(id="510300.SH", asset="fund", account="security", exchange="SSE", product="ETF", lot=100, sell_delay_days=1),
                   dict(id="000300.SH", asset="index", account="security", exchange="SSE", product="INDEX")]
    frames = []
    for day, start, end, f in [("2020-01-02", "14:59", "15:00", "110"), ("2020-01-03", "09:30", "09:31", "110"), ("2020-01-03", "14:59", "15:00", "90"), ("2020-01-06", "09:30", "09:31", "90")]:
        bars = [dict(instrument=i, start=f"{day}T{start}:00+08:00", end=f"{day}T{end}:00+08:00", trading_date=day, open=p, close=p, high="120", low="1", volume=100000) for i, p in [("IF2001.CFE", f), ("510300.SH", "1"), ("000300.SH", "100")]]
        frames.append(dict(time=f"{day}T{end}:00+08:00", bars=bars, open_instruments=[i["id"] for i in instruments]))
    frames[2]["settle_instruments"] = ["IF2001.CFE", "510300.SH"]
    frames[2]["settlements"] = [dict(instrument="IF2001.CFE", trading_date="2020-01-03", settlement_price="90")]
    return dict(initial_cash="2000", instruments=instruments, margin_rules={"IF2001.CFE": dict(model="future", rate=".15")}, execution=dict(participation="1"), frames=frames, cash_carry=dict(decision_times=[frames[0]["time"], frames[2]["time"]], front_contracts={"2020-01-02": {"IF": "IF2001.CFE"}}, max_lots=1))


def test_callback_next_bar_frozen_exit_and_standard_pnl(tmp_path):
    path = tmp_path / "params.json"
    path.write_text(json.dumps(parameters()))
    output = run(scenario(), path)
    assert len(output["fills"]) == 4
    assert str(output["fills"][0]["time"]) == "2020-01-03 09:30:00+08:00"
    assert str(output["fills"][-1]["time"]) == "2020-01-06 09:30:00+08:00"
    # Short 1 future: (110-90)*10 = 200; ETF unchanged.
    assert output["final"]["pnl"] == 200
    assert output["final"]["cash"] == 2200
    assert sum(output["group_pnl"].values()) == 200
    assert not output["positions"]
    assert output["strategy"]["audit"][-1]["action"] == "close"


def test_no_future_or_stale_parameters():
    schedule = ParameterSchedule(parameters())
    assert schedule.get("IF", date(2020, 1, 1)) is None
    assert schedule.get("IF", date(2020, 1, 3)) is None
    bad = deepcopy(parameters())
    bad["parameters"][0]["window_end"] = "2020-01-03"
    with pytest.raises(ValueError, match="future"):
        ParameterSchedule(bad)


def test_partial_fill_timeout_managed_only_by_engine():
    config = scenario()
    # Three common frames without ETF liquidity; fourth unwinds future.
    from datetime import datetime, timedelta
    first = config["frames"][0]
    config["frames"] = [first]
    for n in range(1, 5):
        row = deepcopy(first)
        stamp = datetime.fromisoformat(first["time"]) + timedelta(minutes=n)
        row["time"] = stamp.isoformat()
        for b in row["bars"]:
            b["start"] = (stamp-timedelta(minutes=1)).isoformat()
            b["end"] = stamp.isoformat()
            if b["instrument"] == "510300.SH":
                b["volume"] = 0
        config["frames"].append(row)
    engine = engine_from_config(config)
    callback = CashCarryCallback(parameters(), engine.ledger.instruments, [first["time"]], config["cash_carry"]["front_contracts"], max_lots=1)
    engine = run_scenario(config, on_bar=callback)
    assert len(engine.operations) == 1
    assert engine.operations[0].state == "unwound"
    assert len(engine.ledger.fills) == 2
    assert not engine.ledger.lots


def test_backtest_end_keeps_open_positions(tmp_path):
    path = tmp_path / "params.json"
    path.write_text(json.dumps(parameters()))
    config = scenario()
    config["frames"] = config["frames"][:2]
    output = run(config, path)
    assert len(output["fills"]) == 2
    assert len(output["positions"]) == 2
    assert output["final"]["pnl"] == 0


def test_export_preserves_entire_daily_schedule(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "config.json").write_text("{}")
    (source / "summary.csv").write_text("cagr\n.03\n")
    (source / "selections.csv").write_text("product,decision_day,window_start,window_end,entry_threshold,exit_threshold\nIF,2020-01-02,2019-01-03,2020-01-02,.01,0\nIF,2020-01-03,2019-01-04,2020-01-03,.02,-.02\n")
    output = export_parameters(source, tmp_path / "parameters.json")
    assert len(output["parameters"]) == 2
    assert len(output["source_sha256"]["selections.csv"]) == 64
