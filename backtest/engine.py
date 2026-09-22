from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from typing import Callable
from types import MappingProxyType

from .accounting import Ledger
from .models import Asset, Bar, D, Fill, Instruction, MarginRule


@dataclass(frozen=True)
class ExecutionConfig:
    participation: Decimal = Decimal("0.1")
    slippage_ticks: int = 0
    fee_per_unit: Decimal = Decimal(0)
    fee_rate: Decimal = Decimal(0)
    close_today_fee_per_unit: Decimal | None = None

    def __post_init__(self):
        if not 0 < self.participation <= 1 or min(self.slippage_ticks, self.fee_per_unit, self.fee_rate) < 0:
            raise ValueError("Invalid execution settings")


@dataclass(frozen=True)
class Frame:
    """Explicit calendar frame. open_instruments comes from calendars, not bar presence."""
    time: datetime
    bars: dict[str, Bar]
    open_instruments: frozenset[str]
    settle_instruments: frozenset[str] = frozenset()
    # (instrument, trading_date, settlement_price) from final daily records
    settlements: tuple = ()


@dataclass
class Operation:
    id: int
    submitted: datetime
    legs: list[Instruction]
    combination: bool = False
    attempts: int = 0
    state: str = "pending"
    mode: str = "open"
    filled: list[Instruction] = field(default_factory=list)
    original_instruments: frozenset[str] = frozenset()


class StrategyContext:
    """Read-only observations and order methods; not a sandbox for untrusted code."""

    def __init__(self, engine, frame):
        self.__engine = engine
        self.time = frame.time
        self.bars = MappingProxyType(dict(frame.bars))
        self.marks = MappingProxyType(dict(engine.ledger.marks))
        self.account = engine.ledger.snapshot(engine.margin_rules)
        self.positions = tuple(MappingProxyType(dict(p.__dict__)) for p in engine.ledger.lots)

    def submit(self, legs, combination=False):
        return self.__engine.submit(legs, self.time, combination)

    def target(self, instrument, quantity):
        return self.__engine.target(instrument, quantity, self.time)

    def target_weight(self, instrument, weight):
        return self.__engine.target_weight(instrument, weight, self.time)


class Engine:
    def __init__(self, ledger: Ledger, margin_rules: dict[str, MarginRule], config=None, execution_by_instrument=None):
        self.ledger = ledger
        self.margin_rules = margin_rules
        self.config = config or ExecutionConfig()
        self.execution_by_instrument = execution_by_instrument or {}
        self.operations: list[Operation] = []
        self.warnings: list[dict] = []
        self.snapshots: list[dict] = []
        self.now: datetime | None = None
        self._counter = 0
        self.targets: dict[tuple, int] = {}
        self.groups: dict[str, dict] = {}

    def submit(self, legs: list[Instruction], at: datetime, combination=False, mode=None):
        if not legs or at.tzinfo is None or (self.now and at < self.now):
            raise ValueError("Empty operation or invalid submission time")
        for leg in legs:
            inst = self.ledger.instruments[leg.instrument]
            if inst.asset == Asset.INDEX or (inst.asset in (Asset.EQUITY, Asset.FUND) and leg.side < 0):
                raise ValueError("Unsupported trade")
            if leg.effect == "open" and leg.quantity % inst.lot:
                raise ValueError("Opening quantity must match lot size")
        if combination and len({leg.group for leg in legs}) != 1:
            raise ValueError("Combination requires one group instance")
        group = legs[0].group
        if combination and group == "standalone":
            raise ValueError("Combination needs explicit group instance")
        if combination and any(op.state in ("pending", "unwinding") and any(l.group == group for l in op.legs) for op in self.operations):
            raise ValueError("Group already has an active operation")
        mode = mode or ("open" if all(x.effect == "open" for x in legs) else "close")
        if combination and mode == "open" and any(p.group == group for p in self.ledger.lots):
            raise ValueError("New opening requires a unique empty group instance")
        self._counter += 1
        op = Operation(self._counter, at, list(legs), combination, mode=mode, original_instruments=frozenset(x.instrument for x in legs))
        self.operations.append(op)
        if combination:
            self.groups.setdefault(group, dict(expected=sorted({(l.instrument, l.side) for l in legs}), status="opening"))
        return op

    def target(self, instrument, quantity: int, at: datetime, group="standalone"):
        """Persistent net target; explicit derivative directions use submit instead."""
        if group != "standalone":
            raise ValueError("Combination quantities must be submitted as legs")
        self.targets[instrument, group] = quantity
        self._reconcile_target(instrument, group, at)

    def target_weight(self, instrument, weight, at):
        inst = self.ledger.instruments[instrument]
        if inst.asset in (Asset.OPTION, Asset.INDEX):
            raise ValueError("Option weights belong in research; indexes are not tradeable")
        nominal = self.ledger.marks[instrument] * inst.multiplier
        if nominal <= 0 or self.ledger.last_equity <= 0:
            raise ValueError("Weight sizing requires positive equity and nominal price")
        quantity = int(D(weight) * self.ledger.last_equity / nominal / inst.lot) * inst.lot
        self.target(instrument, quantity, at)

    def _reconcile_target(self, instrument, group, at):
        for op in self.operations:
            if not op.combination and op.state == "pending" and any(l.instrument == instrument and l.group == group for l in op.legs):
                op.state = "cancelled"
        target = self.targets[instrument, group]
        legs = []
        for side in (1, -1):
            held = self.ledger.quantity(instrument, side, group)
            wanted = abs(target) if target * side > 0 else 0
            if held > wanted:
                legs.append(Instruction(instrument, side, held - wanted, "close", group))
            elif held < wanted:
                legs.append(Instruction(instrument, side, wanted - held, "open", group))
        if legs:
            self.submit(legs, at)

    def _warn(self, op, code, **extra):
        self.warnings.append(dict(time=str(self.now), operation=op.id, code=code, **extra))

    def _match(self, leg, bar, available_volume):
        inst = self.ledger.instruments[leg.instrument]
        if bar.suspended or bar.volume == 0 or (inst.expiry and bar.trading_date > inst.expiry):
            return None
        if leg.buy and bar.upper_limit is not None and bar.open >= bar.upper_limit:
            return None
        if not leg.buy and bar.lower_limit is not None and bar.open <= bar.lower_limit:
            return None
        cfg = self.execution_by_instrument.get(inst.id, self.config)
        qty = min(leg.quantity, available_volume)
        if leg.effect == "close":
            qty = min(qty, self.ledger.available(leg, bar.trading_date))
        else:
            qty = qty // inst.lot * inst.lot
        if not qty:
            return None
        price = bar.open + (1 if leg.buy else -1) * cfg.slippage_ticks * inst.tick
        if price < bar.low or price > bar.high:
            return None
        if (bar.upper_limit is not None and price > bar.upper_limit) or (bar.lower_limit is not None and price < bar.lower_limit):
            return None
        fee = qty * cfg.fee_per_unit + qty * abs(price) * inst.multiplier * cfg.fee_rate
        if leg.effect == "close" and cfg.close_today_fee_per_unit is not None:
            remaining, today = qty, 0
            for lot in self.ledger.lots:
                if (lot.instrument, lot.side, lot.group) == (leg.instrument, leg.side, leg.group):
                    used = min(remaining, lot.quantity)
                    if lot.trading_date == bar.trading_date:
                        today += used
                    remaining -= used
                    if not remaining:
                        break
            fee += today * (cfg.close_today_fee_per_unit - cfg.fee_per_unit)
        return Fill(bar.start, bar.trading_date, replace(leg, quantity=qty), price, fee)

    def step(self, frame: Frame, on_bar: Callable | None = None, on_day_end: Callable | None = None):
        if frame.time.tzinfo is None or (self.now and frame.time <= self.now):
            raise ValueError("Frames must be aware and strictly ordered")
        if any(b.end != frame.time or key != b.instrument for key, b in frame.bars.items()):
            raise ValueError("Bars must share the frame end")
        self.now = frame.time
        budgets = {i: int(b.volume * self.execution_by_instrument.get(i, self.config).participation) for i, b in frame.bars.items()}
        # Eligibility is decided before any fill; all closing legs precede all opening legs.
        pending = sorted([o for o in self.operations if o.state in ("pending", "unwinding")], key=lambda o: (not any(l.effect == "close" for l in o.legs), o.id))
        eligible = []
        for op in pending:
            if op.submitted >= frame.time:
                continue
            if op.combination and not op.original_instruments <= frame.open_instruments:
                continue
            if not op.combination and not any(l.instrument in frame.open_instruments for l in op.legs):
                continue
            # Never execute at a bar open earlier than the decision.
            if any(b.start < op.submitted for i, b in frame.bars.items() if i in op.original_instruments):
                continue
            op.attempts += 1
            eligible.append(op)
        remaining_by_id = {o.id: [] for o in eligible}
        ordered_legs = sorted([(o, j, l) for o in eligible for j, l in enumerate(o.legs)], key=lambda item: (item[2].effect != "close", item[0].id, item[1]))
        for op, _, leg in ordered_legs:
            bar = frame.bars.get(leg.instrument)
            fill = self._match(leg, bar, budgets.get(leg.instrument, 0)) if bar and leg.instrument in frame.open_instruments else None
            if fill:
                self.ledger.apply(fill)
                budgets[leg.instrument] -= fill.instruction.quantity
                op.filled.append(fill.instruction)
                if fill.instruction.quantity < leg.quantity:
                    remaining_by_id[op.id].append(replace(leg, quantity=leg.quantity - fill.instruction.quantity))
            else:
                remaining_by_id[op.id].append(leg)
        for op in eligible:
            remaining = remaining_by_id[op.id]
            op.legs = remaining
            if not remaining:
                op.state = "unwound" if op.state == "unwinding" else "completed"
                if op.mode == "roll" and op.state == "completed":
                    group = op.filled[0].group
                    self.groups[group]["expected"] = sorted({(p.instrument, p.side) for p in self.ledger.lots if p.group == group})
                continue
            if op.combination:
                self._warn(op, "COMBINATION_INCOMPLETE", attempt=op.attempts)
                if op.attempts >= 3 and op.state != "unwinding":
                    if op.mode in ("open", "roll"):
                        group = remaining[0].group
                        # Open operations have unique groups; roll failure exits the residual group.
                        quantities = {}
                        for p in self.ledger.lots:
                            if p.group == group:
                                key = p.instrument, p.side
                                quantities[key] = quantities.get(key, 0) + p.quantity
                        op.legs = [Instruction(i, s, q, "close", group) for (i, s), q in quantities.items()]
                    op.state = "unwinding" if op.legs else "unwound"
                    # Cleanup is no longer gated by unavailable original legs.
                    op.combination = False
                    self._warn(op, "COMBINATION_TIMEOUT")
            elif op.state == "unwinding":
                self._warn(op, "EXIT_PENDING")
        for i, b in frame.bars.items():
            self.ledger.marks[i] = b.close
        if on_bar:
            on_bar(StrategyContext(self, frame))
        provided = {i for i, _, _ in frame.settlements}
        required = {p.instrument for p in self.ledger.lots if p.instrument in frame.settle_instruments and self.ledger.instruments[p.instrument].asset in (Asset.FUTURE, Asset.OPTION)}
        if not required <= provided:
            raise ValueError(f"Missing settlement prices: {sorted(required - provided)}")
        delivered = set()
        for i, day, price in frame.settlements:
            self.ledger.settle(i, day, price)
            if self.ledger.instruments[i].delivery == day:
                delivered.add(i)
        for op in self.operations:
            if op.state not in ("pending", "unwinding"):
                continue
            if any(l.instrument in delivered for l in op.legs):
                op.state = "expired"
                self._warn(op, "DELIVERY_INTERRUPTED_OPERATION")
            elif not op.combination and op.state == "pending" and op.submitted < frame.time and any(l.instrument in frame.settle_instruments for l in op.legs):
                op.state = "expired"
        for key in list(self.targets):
            if key[0] in delivered:
                del self.targets[key]
            elif key[0] in frame.settle_instruments:
                self._reconcile_target(*key, frame.time)
        snapshot = self.ledger.snapshot(self.margin_rules)
        for group, state in self.groups.items():
            held = {(p.instrument, p.side) for p in self.ledger.lots if p.group == group}
            active = [o for o in self.operations if o.state in ("pending", "unwinding") and any(l.group == group for l in o.legs)]
            if active:
                state["status"] = "unwinding" if any(o.state == "unwinding" for o in active) else "pending"
            elif not held:
                state["status"] = "closed"
            elif any(i in delivered for i, _ in state["expected"]):
                state["status"] = "incomplete"
                self.warnings.append(dict(time=str(frame.time), code="GROUP_LEG_DELIVERED", group=group))
            elif state["status"] != "incomplete":
                state["status"] = "holding"
        self.snapshots.append(dict(time=str(frame.time), **snapshot.__dict__))
        if frame.settle_instruments:
            self.ledger.last_equity = snapshot.equity
        if snapshot.equity <= 0 or snapshot.risk_ratio is not None and snapshot.risk_ratio > 1:
            self.warnings.append(dict(time=str(frame.time), code="FUNDING_RISK", equity=str(snapshot.equity), margin=str(snapshot.margin)))
        if frame.settle_instruments and on_day_end:
            on_day_end(StrategyContext(self, frame))
        return snapshot

    def roll(self, group, mapping: dict[str, str], at: datetime, enabled=False):
        if not enabled:
            return None
        legs = []
        for lot in self.ledger.lots:
            inst = self.ledger.instruments[lot.instrument]
            if lot.group != group or inst.asset != Asset.FUTURE:
                continue
            target = self.ledger.instruments[mapping[inst.product]]
            if target.asset != Asset.FUTURE or (target.exchange, target.product) != (inst.exchange, inst.product):
                raise ValueError("Invalid main-contract mapping")
            if inst.expiry < target.expiry:
                legs.extend([Instruction(inst.id, lot.side, lot.quantity, "close", group), Instruction(target.id, lot.side, lot.quantity, "open", group)])
        return self.submit(legs, at, combination=True, mode="roll") if legs else None
