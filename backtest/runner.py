from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform

from .accounting import Ledger
from .engine import Engine, ExecutionConfig, Frame
from .models import Account, Asset, D, Instrument, Instruction, MarginRule
from .storage import bar_from_record, json_default


def instrument_from_record(record):
    row = dict(record)
    row["asset"] = Asset(row["asset"])
    row["account"] = Account(row["account"])
    for key in ("expiry", "delivery"):
        if row.get(key):
            row[key] = date.fromisoformat(row[key])
    for key in ("multiplier", "tick", "strike"):
        if row.get(key) is not None:
            row[key] = D(row[key])
    return Instrument(**row)


def execution_from_record(record):
    row = dict(record)
    for key in ("participation", "fee_per_unit", "fee_rate", "close_today_fee_per_unit"):
        if row.get(key) is not None:
            row[key] = D(row[key])
    return ExecutionConfig(**row)


def engine_from_config(config):
    instruments = {r["id"]: instrument_from_record(r) for r in config["instruments"]}
    if len(instruments) != len(config["instruments"]):
        raise ValueError("Duplicate instruments")
    rules = {key: MarginRule(**{k: (v if k == "model" else D(v)) for k, v in row.items()}) for key, row in config.get("margin_rules", {}).items()}
    return Engine(Ledger(instruments, config["initial_cash"]), rules, execution_from_record(config.get("execution", {})),
                  {i: execution_from_record(row) for i, row in config.get("execution_by_instrument", {}).items()})


def run_scenario(config):
    """Explicit frame replay for reproducible tests and normalized research inputs."""
    engine = engine_from_config(config)
    inputs = sorted(config.get("instructions", []), key=lambda x: datetime.fromisoformat(x["at"]))
    index = 0
    for row in config["frames"]:
        stamp = datetime.fromisoformat(row["time"])
        # enqueue decisions only when their timestamp has been reached
        while index < len(inputs) and datetime.fromisoformat(inputs[index]["at"]) < stamp:
            event = inputs[index]
            at = datetime.fromisoformat(event["at"])
            if "legs" in event:
                engine.submit([Instruction(**leg) for leg in event["legs"]], at, event.get("combination", False))
            elif "weight" in event:
                engine.target_weight(event["instrument"], event["weight"], at)
            else:
                engine.target(event["instrument"], event["quantity"], at)
            index += 1
        bars = {r["instrument"]: bar_from_record(r) for r in row.get("bars", [])}
        settlements = tuple((s["instrument"], date.fromisoformat(s["trading_date"]), D(s["settlement_price"])) for s in row.get("settlements", []))
        engine.step(Frame(stamp, bars, frozenset(row["open_instruments"]), frozenset(row.get("settle_instruments", [])), settlements))
    if index < len(inputs):
        engine.warnings.append(dict(code="INPUT_AFTER_BACKTEST_END", count=len(inputs) - index))
    return engine


def result(engine, config):
    final = engine.ledger.snapshot(engine.margin_rules)
    peak = engine.ledger.initial_cash
    drawdown = D(0)
    for row in engine.snapshots:
        peak = max(peak, row["equity"])
        if peak > 0:
            drawdown = max(drawdown, (peak - row["equity"]) / peak)
    code = hashlib.sha256()
    for path in sorted(Path(__file__).parent.glob("*.py")):
        code.update(path.name.encode())
        code.update(path.read_bytes())
    return dict(
        schema_version=1,
        provenance=dict(config_sha256=hashlib.sha256(json.dumps(config, sort_keys=True, default=json_default).encode()).hexdigest(),
                        code_sha256=code.hexdigest(), python=platform.python_version(),
                        dependencies={name: importlib.metadata.version(name) for name in ("duckdb", "pyarrow", "tzdata", "pytz")},
                        data_version=config.get("data_version", "inline-scenario")),
        assumptions=["unified_cash_pool", "no_funding_admission_gate", "cash_delivery", "no_forced_liquidation", "bar_liquidity_approximation"],
        final=asdict(final), max_drawdown=drawdown, group_pnl=engine.ledger.group_pnl(),
        snapshots=engine.snapshots, positions=[asdict(p) for p in engine.ledger.lots],
        fills=[asdict(f) for f in engine.ledger.fills], cash_entries=engine.ledger.entries,
        operations=[asdict(o) for o in engine.operations], groups=engine.groups, warnings=engine.warnings,
    )
