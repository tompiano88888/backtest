"""Parameter schedule -> one read-only Engine callback; no research execution path."""
from bisect import bisect_right
from collections import defaultdict
import csv
from datetime import date, datetime
import hashlib
import json
from pathlib import Path

from .cash_carry_signals import annualized_rate
from .cash_futures_basis import PAIRS
from .models import Asset, D, Instruction
from .runner import result, run_scenario


BASELINE = "outputs/pooled_rolling_arbitrage_etf_dividends"


def export_parameters(source=BASELINE, output="configs/cash_carry_best.json"):
    source, output = Path(source), Path(output)
    files = {name: hashlib.sha256((source / name).read_bytes()).hexdigest()
             for name in ("config.json", "selections.csv", "summary.csv")}
    with (source / "selections.csv").open(encoding="utf-8-sig", newline="") as stream:
        selections = list(csv.DictReader(stream))
    rows = [dict(product=r["product"], decision_day=r["decision_day"],
                 window_start=r["window_start"], window_end=r["window_end"],
                 entry_threshold=r["entry_threshold"], exit_threshold=r["exit_threshold"])
            for r in selections]
    config = dict(schema_version=1, source=str(source), source_sha256=files,
                  source_config=json.loads((source / "config.json").read_text(encoding="utf-8")),
                  selection_policy="previously recorded trailing-year daily winners; never backfill latest thresholds",
                  parameters=rows)
    # Validate before publishing.
    ParameterSchedule(config)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    return config


class ParameterSchedule:
    def __init__(self, config):
        if config["schema_version"] != 1:
            raise ValueError("Unsupported parameter schema")
        self.rows = defaultdict(list)
        seen = set()
        for r in config["parameters"]:
            product, day = r["product"], date.fromisoformat(r["decision_day"])
            if product not in PAIRS or (product, day) in seen:
                raise ValueError("Unknown product or duplicate parameter day")
            if not date.fromisoformat(r["window_start"]) <= date.fromisoformat(r["window_end"]) <= day:
                raise ValueError("Invalid or future training window")
            entry, exit_ = D(r["entry_threshold"]), D(r["exit_threshold"])
            if entry < exit_:
                raise ValueError("Entry below exit threshold")
            seen.add((product, day))
            self.rows[product].append((day, entry, exit_))
        if not seen:
            raise ValueError("Empty parameter schedule")
        for rows in self.rows.values():
            rows.sort()

    def get(self, product, day):
        rows = self.rows.get(product, [])
        index = bisect_right([r[0] for r in rows], day) - 1
        # No stale parameter extrapolation outside the recorded daily schedule.
        return rows[index][1:] if index >= 0 and rows[index][0] == day else None


class CashCarryCallback:
    """Decide only at caller-supplied completed daily close timestamps.

    Thresholds include that day's close, hence must not be consumed intraday.
    Contract lifecycles and clocks are supplied by the normalized run input.
    """
    def __init__(self, parameters, instruments, decision_times, front_contracts, margin_rate=".15", max_lots=100):
        self.schedule = ParameterSchedule(parameters)
        self.instruments = dict(instruments)
        self.decision_times = frozenset(datetime.fromisoformat(t) for t in decision_times)
        self.front_contracts = dict(front_contracts)
        if any(t.tzinfo is None for t in self.decision_times):
            raise ValueError("Decision times require timezone")
        self.margin_rate = D(margin_rate)
        if self.margin_rate < 0 or type(max_lots) is not int or max_lots <= 0:
            raise ValueError("Invalid strategy sizing")
        self.max_lots = max_lots
        self.groups = {}
        self.audit = []
        self.counter = 0

    def __call__(self, ctx):
        # Engine owns operation progress, including partial fills and unwind.
        held = defaultdict(list)
        for p in ctx.positions:
            if p["group"] in self.groups:
                held[p["group"]].append(p)
        for group, state in list(self.groups.items()):
            if state["operation"].state in ("pending", "unwinding"):
                continue
            positions = held[group]
            if not positions:
                del self.groups[group]
                continue
            future = self.instruments[state["future"]]
            bar = ctx.bars.get(future.id)
            index = ctx.bars.get(PAIRS[future.product][0])
            # Delivery leaves the ETF leg; close it at its next allowed bar.
            residual = not any(p["instrument"] == future.id for p in positions)
            day = bar.trading_date if bar else None
            rate = annualized_rate(bar.close, index.close, (future.expiry-day).days) if bar and index and index.trading_date == day else None
            if residual or (ctx.time in self.decision_times and rate is not None and rate < state["exit"]):
                quantities = defaultdict(int)
                for p in positions:
                    quantities[p["instrument"], p["side"]] += p["quantity"]
                state["operation"] = ctx.submit([Instruction(i, s, q, "close", group) for (i, s), q in quantities.items()], combination=len(quantities) > 1)
                self.audit.append(dict(time=str(ctx.time), group=group, action="close", reason="residual_after_delivery" if residual else "threshold"))
        if ctx.time not in self.decision_times:
            return
        # Reserve pending opening notional locally until matching completes.
        budget = ctx.account.cash - ctx.account.margin
        for state in self.groups.values():
            if state["operation"].mode == "open" and state["operation"].state in ("pending", "unwinding"):
                budget -= state["reserve"]
        candidates = []
        for product, (index_id, etf_id) in PAIRS.items():
            index, etf = ctx.bars.get(index_id), ctx.bars.get(etf_id)
            if not index or not etf or index.trading_date != etf.trading_date or etf.close <= 0:
                continue
            day = index.trading_date
            params = self.schedule.get(product, day)
            if params is None:
                continue
            code = self.front_contracts.get(day.isoformat(), {}).get(product)
            if code is None:
                continue
            future = self.instruments[code]
            if future.asset != Asset.FUTURE or future.product != product:
                raise ValueError("Invalid front contract mapping")
            bar = ctx.bars.get(code)
            if bar is None:
                continue
            if bar.trading_date != day or day >= future.expiry:
                continue
            rate = annualized_rate(bar.close, index.close, (future.expiry-day).days)
            if rate is None or rate <= params[0]:
                continue
            inst = self.instruments[etf_id]
            quantity = int(index.close * future.multiplier / etf.close / inst.lot) * inst.lot
            if quantity <= 0:
                continue
            reserve = quantity * etf.close + abs(bar.close) * future.multiplier * self.margin_rate
            candidates.append((rate-params[1], product, future, quantity, reserve, params))
        candidates.sort(key=lambda c: (-c[0], c[1]))
        for _ in range(max(0, self.max_lots-len(self.groups))):
            choice = next((c for c in candidates if c[4] <= budget), None)
            if choice is None:
                break
            gap, product, future, quantity, reserve, params = choice
            self.counter += 1
            group = f"cash-carry-{self.counter:06d}"
            op = ctx.submit([Instruction(future.id, -1, 1, group=group), Instruction(PAIRS[product][1], 1, quantity, group=group)], combination=True)
            self.groups[group] = dict(operation=op, future=future.id, exit=params[1], reserve=reserve)
            self.audit.append(dict(time=str(ctx.time), group=group, action="open", product=product, entry=str(params[0]), exit=str(params[1]), expected_gap=str(gap), etf_quantity=quantity))
            budget -= reserve


def run(config, parameter_path):
    from .runner import engine_from_config
    raw = Path(parameter_path).read_bytes()
    parameters = json.loads(raw)
    instruments = engine_from_config(config).ledger.instruments
    callback = CashCarryCallback(parameters, instruments, **config["cash_carry"])
    if config.get("instructions"):
        raise ValueError("Callback runs cannot mix offline instructions")
    engine = run_scenario(config, on_bar=callback)
    enriched = dict(config, strategy_parameters=parameters)
    output = result(engine, enriched)
    output["strategy"] = dict(name="cash_carry", parameters_sha256=hashlib.sha256(raw).hexdigest(), audit=callback.audit)
    output["assumptions"] += ["close_signal_next_allowed_bar", "close_sized_fixed_integer_orders", "no_retrospective_opening_cancellation", "dividends_and_idle_cash_only_when_configured", "historical_rules_and_calendar_supplied_by_caller", "not_a_reproduction_of_research_returns"]
    return output
