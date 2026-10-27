from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform

from .accounting import Ledger
from .account_events import from_config as accounting_from_config
from .engine import Engine, ExecutionConfig, Frame
from .models import Account, Asset, Auction, D, Instrument, Instruction, MarginRule
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
    for key in ("participation", "fee_per_unit", "fee_rate", "close_today_fee_per_unit", "close_today_fee_rate", "commission_rate", "commission_per_unit"):
        if row.get(key) is not None:
            row[key] = D(row[key])
    return ExecutionConfig(**row)


def auction_from_record(record):
    row = dict(record)
    row['time'] = datetime.fromisoformat(row['time'])
    row['trading_date'] = date.fromisoformat(row['trading_date'])
    for key in ('price','upper_limit','lower_limit'):
        if row.get(key) is not None:
            row[key] = D(row[key])
    return Auction(**row)


def engine_from_config(config):
    instruments = {r["id"]: instrument_from_record(r) for r in config["instruments"]}
    if len(instruments) != len(config["instruments"]):
        raise ValueError("Duplicate instruments")
    rules = {key: MarginRule(**{k: (v if k == "model" else D(v)) for k, v in row.items()}) for key, row in config.get("margin_rules", {}).items()}
    fee_provider = None
    if 'historical_fees' in config:
        from .fees import HistoricalFees
        fee_provider = HistoricalFees(**config['historical_fees'])
    return Engine(Ledger(instruments, config["initial_cash"]), rules, execution_from_record(config.get("execution", {})),
                  {i: execution_from_record(row) for i, row in config.get("execution_by_instrument", {}).items()},
                  accounting_from_config(config["accounting"]) if "accounting" in config else None,
                  fee_provider=fee_provider)


def run_scenario(config, on_bar=None, *, output=None):
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
        auctions = {r['instrument']:auction_from_record(r) for r in row.get('auctions',[])}
        if len(auctions) != len(row.get('auctions',[])):
            raise ValueError('Duplicate auction instrument')
        settlements = tuple((s["instrument"], date.fromisoformat(s["trading_date"]), D(s["settlement_price"])) for s in row.get("settlements", []))
        start_day = date.fromisoformat(row["accounting_day_start"]) if row.get("accounting_day_start") else None
        end_day = date.fromisoformat(row["accounting_day_end"]) if row.get("accounting_day_end") else None
        engine.step(Frame(stamp, bars, frozenset(row["open_instruments"]), frozenset(row.get("settle_instruments", [])), settlements,
                          start_day, end_day, row.get("valuation_marks"), auctions), on_bar=on_bar)
    if engine.accounting_events and engine.accounting_events.current_day is not None:
        engine.warnings.append(dict(code="ACCOUNTING_DAY_NOT_CLOSED", day=str(engine.accounting_events.current_day)))
    if index < len(inputs):
        engine.warnings.append(dict(code="INPUT_AFTER_BACKTEST_END", count=len(inputs) - index))
    if output is not None:
        save_result(engine, config, output)
    return engine


def save_result(engine, config, path):
    """Finalize a complete scenario with its JSON and mandatory standard report."""
    from .performance import export_scenario_report
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = result(engine, config)
    path.write_text(json.dumps(payload, default=json_default, ensure_ascii=False, indent=2), encoding='utf-8')
    export_scenario_report(payload, config, path.parent / path.stem)
    return payload


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
        assumptions=["unified_cash_pool", "no_funding_admission_gate", "cash_delivery", "no_forced_liquidation", "bar_liquidity_approximation"] +
                    (["explicit_calendar_accounting_clock", "dividend_net_receivable_on_ex_date_cash_on_payment_day_start",
                      "configured_dividend_tax_assumption_not_automatic_stock_tax_law",
                      "idle_cash_estimated_daily_income_not_repo_order_simulation"] if engine.accounting_events else []),
        final=asdict(final), max_drawdown=drawdown, group_pnl=engine.ledger.group_pnl(),
        snapshots=engine.snapshots, positions=[asdict(p) for p in engine.ledger.lots],
        fills=[asdict(f) for f in engine.ledger.fills], cash_entries=engine.ledger.entries,
        fee_details=engine.fees.details,
        fee_provenance=(dict(version=engine.fees.provider.version, as_of=engine.fees.provider.as_of)
                        if engine.fees.provider is not None else None),
        noncash_entries=engine.ledger.noncash_entries,
        receivables=engine.ledger.receivables,
        accounting=engine.accounting_events.report() if engine.accounting_events else None,
        income=dict(dividend_gross=sum((D(r["gross"]) for r in engine.ledger.noncash_entries if r["kind"] == "dividend_receivable"), D(0)),
                    dividend_tax=sum((D(r["tax"]) for r in engine.ledger.noncash_entries if r["kind"] == "dividend_receivable"), D(0)),
                    dividend_cash_paid=sum((D(r["amount"]) for r in engine.ledger.entries if r["kind"] == "cash_dividend"), D(0)),
                    idle_cash_income=sum((D(r["amount"]) for r in engine.ledger.entries if r["kind"] == "idle_cash_income"), D(0))),
        operations=[asdict(o) for o in engine.operations], groups=engine.groups, warnings=engine.warnings,
    )
