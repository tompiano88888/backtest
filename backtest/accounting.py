from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from .models import Asset, D, Fill, Instrument, Lot, MarginRule


@dataclass(frozen=True)
class Snapshot:
    cash: Decimal
    equity: Decimal
    margin: Decimal
    risk_ratio: Decimal | None
    funding_gap: Decimal
    pnl: Decimal
    dividend_receivable: Decimal = Decimal(0)


class Ledger:
    """One cash pool. Margin is diagnostic, never a debit or admission gate."""

    def __init__(self, instruments: dict[str, Instrument], initial_cash):
        self.instruments = instruments
        self.initial_cash = D(initial_cash)
        self.cash = self.initial_cash
        self.lots: list[Lot] = []
        self.fills: list[Fill] = []
        self.entries: list[dict] = []
        self.marks: dict[str, Decimal] = {}
        self.settled: set[tuple[str, date]] = set()
        self.actions: set[str] = set()
        self.last_equity = self.initial_cash
        self.receivables: dict[str, dict] = {}
        self.noncash_entries: list[dict] = []

    def quantity(self, instrument, side, group=None):
        return sum(p.quantity for p in self.lots if p.instrument == instrument and p.side == side and (group is None or p.group == group))

    def available(self, instruction, day):
        inst = self.instruments[instruction.instrument]
        return sum(p.quantity for p in self.lots if p.instrument == inst.id and p.side == instruction.side and p.group == instruction.group
                   and (inst.sell_delay_days == 0 or p.trading_date < day))

    def _cash(self, amount, kind, instrument, group, stamp):
        self.cash += amount
        self.entries.append(dict(time=str(stamp), kind=kind, instrument=instrument, group=group, amount=str(amount)))

    def post_cash_event(self, event_id, amount, kind, instrument, group, stamp):
        """Framework accounting events only; strategies must submit orders instead."""
        amount = D(amount)
        if event_id in self.actions:
            raise ValueError("Duplicate cash event")
        self._cash(amount, kind, instrument, group, stamp)
        self.entries[-1]["event_id"] = event_id
        self.actions.add(event_id)

    def recognize_dividend(self, claim_id, instrument, group, gross, tax, day):
        key = f"dividend-recognize:{claim_id}"
        gross, tax = D(gross), D(tax)
        if key in self.actions or not 0 <= tax <= gross:
            raise ValueError("Duplicate/invalid dividend receivable")
        if self.instruments[instrument].asset not in (Asset.EQUITY, Asset.FUND):
            raise ValueError("Dividend requires equity or fund")
        self.receivables[claim_id] = dict(instrument=instrument, group=group, amount=gross-tax)
        self.noncash_entries.append(dict(time=str(day), kind="dividend_receivable", claim_id=claim_id,
                                        instrument=instrument, group=group, gross=str(gross), tax=str(tax), amount=str(gross-tax)))
        self.actions.add(key)

    def pay_dividend(self, claim_id, day):
        claim = self.receivables[claim_id]
        self.post_cash_event(f"dividend-payment:{claim_id}", claim["amount"], "cash_dividend",
                             claim["instrument"], claim["group"], day)
        del self.receivables[claim_id]

    def apply(self, fill: Fill):
        order = fill.instruction
        inst = self.instruments[order.instrument]
        if inst.asset == Asset.INDEX or (inst.asset in (Asset.EQUITY, Asset.FUND) and order.side == -1):
            raise ValueError("Index trading / security shorting not supported")
        if order.effect == "open" and order.quantity % inst.lot:
            raise ValueError("Invalid opening lot size")
        if inst.delivery and fill.trading_date > inst.delivery:
            raise ValueError("Contract already delivered")
        if (inst.id, fill.trading_date) in self.settled:
            raise ValueError("Cannot trade after instrument settlement")
        if order.effect == "close" and order.quantity > self.available(order, fill.trading_date):
            raise ValueError("Insufficient closeable position")
        amount = D(0)
        if order.effect == "open":
            self.lots.append(Lot(inst.id, order.side, order.quantity, fill.price, fill.price, fill.trading_date, order.group))
            if inst.asset != Asset.FUTURE:
                amount = -order.side * order.quantity * fill.price * inst.multiplier
        else:
            remaining = order.quantity
            for lot in self.lots:
                if remaining == 0:
                    break
                if (lot.instrument, lot.side, lot.group) != (inst.id, order.side, order.group):
                    continue
                if inst.sell_delay_days and lot.trading_date >= fill.trading_date:
                    continue
                count = min(remaining, lot.quantity)
                if inst.asset == Asset.FUTURE:
                    amount += lot.side * count * (fill.price - lot.basis) * inst.multiplier
                else:
                    amount += lot.side * count * fill.price * inst.multiplier
                lot.quantity -= count
                remaining -= count
            self.lots = [p for p in self.lots if p.quantity]
        self._cash(amount, "trade", inst.id, order.group, fill.time)
        self._cash(-fill.fee, "fee", inst.id, order.group, fill.time)
        self.fills.append(fill)
        self.marks[inst.id] = fill.price

    def settle(self, instrument: str, day: date, price: Decimal):
        inst = self.instruments[instrument]
        key = instrument, day
        if key in self.settled:
            raise ValueError("Duplicate settlement")
        if inst.asset not in (Asset.FUTURE, Asset.OPTION):
            raise ValueError("Only derivatives have settlement events")
        if not price.is_finite() or (inst.asset == Asset.OPTION and price < 0):
            raise ValueError("Invalid settlement price")
        if inst.delivery and day > inst.delivery:
            raise ValueError("Missed delivery event")
        delivery = day == inst.delivery
        for lot in self.lots:
            if lot.instrument != instrument:
                continue
            if inst.asset == Asset.FUTURE:
                delta = lot.side * lot.quantity * inst.multiplier * (price - lot.basis)
                self._cash(delta, "delivery" if delivery else "variation_margin", instrument, lot.group, day)
                lot.basis = price
            elif delivery:
                self._cash(lot.side * lot.quantity * inst.multiplier * price, "delivery", instrument, lot.group, day)
            if delivery:
                lot.quantity = 0
        self.lots = [p for p in self.lots if p.quantity]
        self.marks[instrument] = price
        self.settled.add(key)

    def corporate_action(self, event_id: str, instrument: str, day: date, cash_per_unit: Decimal, ratio: Decimal):
        """Explicit ex-date event. Separate pay-date entitlement handling is future work."""
        if event_id in self.actions or ratio <= 0:
            raise ValueError("Duplicate/invalid corporate action")
        inst = self.instruments[instrument]
        if inst.asset not in (Asset.EQUITY, Asset.FUND):
            raise ValueError("Corporate action requires security")
        eligible = [p for p in self.lots if p.instrument == instrument and p.trading_date < day]
        if any(p.quantity * ratio != int(p.quantity * ratio) for p in eligible):
            raise ValueError("Fractional corporate-action allocation needs explicit cash-in-lieu")
        for lot in eligible:
            self._cash(lot.quantity * cash_per_unit * inst.multiplier, "distribution", instrument, lot.group, day)
            lot.quantity = int(lot.quantity * ratio)
            lot.entry /= ratio
            lot.basis /= ratio
        self.actions.add(event_id)

    def group_pnl(self):
        result = defaultdict(lambda: D(0))
        for entry in self.entries:
            result[entry["group"]] += D(entry["amount"])
        for claim in self.receivables.values():
            result[claim["group"]] += claim["amount"]
        for p in self.lots:
            inst = self.instruments[p.instrument]
            price = self.marks[p.instrument]
            result[p.group] += p.side * p.quantity * inst.multiplier * (price - p.basis if inst.asset == Asset.FUTURE else price)
        return dict(result)

    def snapshot(self, rules: dict[str, MarginRule]) -> Snapshot:
        receivable = sum((c["amount"] for c in self.receivables.values()), D(0))
        equity = self.cash + receivable
        future_margin = defaultdict(lambda: {1: D(0), -1: D(0)})
        option_margin = D(0)
        for p in self.lots:
            inst = self.instruments[p.instrument]
            mark = self.marks[p.instrument]
            equity += p.side * p.quantity * inst.multiplier * (mark - p.basis if inst.asset == Asset.FUTURE else mark)
            if inst.asset == Asset.FUTURE:
                rule = rules[inst.id]
                if rule.model != "future":
                    raise ValueError("Wrong futures margin model")
                future_margin[inst.exchange, inst.product][p.side] += p.quantity * abs(mark) * inst.multiplier * rule.rate
            elif inst.asset == Asset.OPTION and p.side == -1:
                rule = rules[inst.id]
                underlying = self.marks[inst.underlying]
                strike = inst.strike
                otm = max((strike - underlying) if inst.call else (underlying - strike), D(0))
                if rule.model == "index_option":
                    per_unit = mark + max(underlying * rule.rate - otm, rule.floor * (underlying if inst.call else strike) * rule.rate)
                elif rule.model == "etf_option":
                    per_unit = mark + max(underlying * rule.rate - otm, rule.floor * (underlying if inst.call else strike))
                    if not inst.call:
                        per_unit = min(per_unit, strike)
                elif rule.model == "future_option":
                    base = abs(underlying) * rule.underlying_rate
                    per_unit = mark + max(base - otm * rule.rate, base * rule.floor)
                else:
                    raise ValueError("Wrong option margin model")
                option_margin += p.quantity * inst.multiplier * per_unit
        margin = option_margin + sum((max(sides.values()) for sides in future_margin.values()), D(0))
        return Snapshot(self.cash, equity, margin, margin / equity if equity > 0 else None,
                        max(margin - self.cash, D(0)), equity - self.initial_cash, receivable)
