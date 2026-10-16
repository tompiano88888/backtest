"""Calendar-driven account events owned by the unified Engine, not strategies."""
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Protocol

from .models import Asset, D


@dataclass(frozen=True)
class CashDividend:
    event_id: str
    instrument: str
    record_date: date
    ex_date: date
    payment_date: date
    cash_per_unit: Decimal
    withholding_rate: Decimal = D(0)
    source: str = "explicit-input"
    known_at: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "cash_per_unit", D(self.cash_per_unit))
        object.__setattr__(self, "withholding_rate", D(self.withholding_rate))
        if (not self.event_id or any(type(d) is not date for d in (self.record_date, self.ex_date, self.payment_date))
                or not self.record_date < self.ex_date <= self.payment_date):
            raise ValueError("Dividend requires record_date < ex_date <= payment_date")
        if self.cash_per_unit < 0 or not 0 <= self.withholding_rate <= 1:
            raise ValueError("Invalid dividend amount/tax assumption")


@dataclass(frozen=True)
class Entitlement:
    """Immutable record-close lot facts for future holding-period tax policies."""
    instrument: str
    group: str
    quantity: int
    acquired_date: date
    multiplier: Decimal


class DividendTaxPolicy(Protocol):
    def tax_amount(self, event: CashDividend, holding: Entitlement): ...


class FixedDividendTax:
    def tax_amount(self, event, holding):
        return holding.quantity * holding.multiplier * event.cash_per_unit * event.withholding_rate


class AccountEventHandler(Protocol):
    """Trusted framework extension for share actions, cash-in-lieu or tax adjustments.

    Implementations must use Ledger event APIs, retain group attribution and enforce
    their own event IDs. They are not strategy callbacks. No stock tax law implied.
    """
    def day_start(self, day, ledger): ...
    def day_end(self, day, ledger): ...


class IdleCashIncome:
    def __init__(self, rates, base="cash_less_margin", reserve="0", day_count=365, quantum=".00000001"):
        if base not in ("cash", "cash_less_margin") or day_count not in (360, 365):
            raise ValueError("Invalid idle cash base/day count")
        self.rates = sorted((date.fromisoformat(r["valid_from"]), D(r["annual_rate"])) for r in rates)
        self.reserve, self.quantum = D(reserve), D(quantum)
        if (not self.rates or len({d for d, _ in self.rates}) != len(self.rates)
                or any(r < 0 for _, r in self.rates) or self.reserve < 0
                or self.quantum <= 0 or self.quantum.normalize().as_tuple().digits != (1,)):
            raise ValueError("Invalid idle cash rates/reserve/rounding")
        self.base, self.day_count = base, day_count
        self.audit = []

    def accrue(self, day, ledger, rules):
        available_rates = [r for d, r in self.rates if d <= day]
        if not available_rates:
            raise ValueError("Missing historical idle cash rate")
        snap = ledger.snapshot(rules)
        principal = max(snap.cash - self.reserve - (snap.margin if self.base == "cash_less_margin" else D(0)), D(0))
        rate = available_rates[-1]
        amount = (principal * rate / self.day_count).quantize(self.quantum, rounding=ROUND_HALF_EVEN)
        ledger.post_cash_event(f"idle-cash:{day}", amount, "idle_cash_income", None, "cash_management", day)
        self.audit.append(dict(day=day, principal=principal, annual_rate=rate, income=amount, base=self.base,
                               reserve=self.reserve, day_count=self.day_count))


class AccountingEvents:
    def __init__(self, dividends=(), idle_cash=None, tax_policy=None, handlers=()):
        self.dividends = tuple(dividends)
        if len({e.event_id for e in self.dividends}) != len(self.dividends):
            raise ValueError("Duplicate dividend event ID")
        self.idle_cash = idle_cash
        self.tax_policy = tax_policy or FixedDividendTax()
        self.handlers = tuple(handlers)
        self.current_day = self.last_day = None
        self.claims = []
        self.audit = []

    def begin(self, day, ledger):
        if self.current_day is not None or self.last_day is not None and day != self.last_day + timedelta(days=1):
            raise ValueError("Accounting clock requires consecutive closed calendar days")
        if self.last_day is None and any(e.record_date < day for e in self.dividends):
            raise ValueError("Dividend record date predates run; initial entitlements are required")
        self.current_day = day
        for claim in self.claims:
            event = claim["event"]
            if event.ex_date == day:
                ledger.recognize_dividend(claim["id"], event.instrument, claim["holding"].group,
                                          claim["gross"], claim["tax"], day)
                claim["recognized"] = True
                self.audit.append(dict(day=day, action="recognize", claim_id=claim["id"]))
            if event.payment_date == day:
                ledger.pay_dividend(claim["id"], day)
                claim["paid"] = True
                self.audit.append(dict(day=day, action="payment", claim_id=claim["id"]))
        for handler in self.handlers:
            handler.day_start(day, ledger)

    def end(self, day, ledger, rules):
        if self.current_day != day:
            raise ValueError("Accounting day end requires matching day start")
        for event in self.dividends:
            if event.record_date != day:
                continue
            inst = ledger.instruments[event.instrument]
            if inst.asset not in (Asset.EQUITY, Asset.FUND):
                raise ValueError("Cash dividends support only stocks and funds")
            for lot in ledger.lots:
                if lot.instrument != inst.id or lot.side != 1:
                    continue
                holding = Entitlement(inst.id, lot.group, lot.quantity, lot.trading_date, inst.multiplier)
                gross = lot.quantity * inst.multiplier * event.cash_per_unit
                tax = D(self.tax_policy.tax_amount(event, holding))
                if not 0 <= tax <= gross:
                    raise ValueError("Dividend tax must lie between zero and gross amount")
                claim_id = f"{event.event_id}:{len(self.claims)}"
                self.claims.append(dict(id=claim_id, event=event, holding=holding, gross=gross, tax=tax,
                                        net=gross-tax, recognized=False, paid=False))
                self.audit.append(dict(day=day, action="record_entitlement", claim_id=claim_id))
        for handler in self.handlers:
            handler.day_end(day, ledger)
        if self.idle_cash:
            self.idle_cash.accrue(day, ledger, rules)
        self.last_day, self.current_day = day, None

    def report(self):
        return dict(dividends=[asdict(e) for e in self.dividends],
                    claims=[dict(c, event=asdict(c["event"]), holding=asdict(c["holding"])) for c in self.claims],
                    audit=self.audit, idle_cash=self.idle_cash.audit if self.idle_cash else [],
                    closed_through=self.last_day, open_day=self.current_day,
                    custom_handlers=[type(h).__name__ for h in self.handlers], tax_policy=type(self.tax_policy).__name__)


def from_config(config):
    """No implicit enablement or market-rate downloads."""
    records = list(config.get("dividends", []))
    source = config.get("dividend_snapshot")
    if source:
        import duckdb
        from pathlib import Path
        from types import SimpleNamespace
        from .ifind_etf_dividends import read_events
        begin, end = date.fromisoformat(source["record_from"]), date.fromisoformat(source["record_to"])
        if begin > end:
            raise ValueError("Invalid dividend snapshot date range")
        with duckdb.connect(str(Path(source["root"]) / "metadata.duckdb"), read_only=True) as db:
            for code in source["instruments"]:
                for e in read_events(SimpleNamespace(db=db), source["version"], code):
                    if begin <= e["record_date"] <= end:
                        records.append(dict(event_id=f"{code}/{e['announcement_date']}", instrument=code,
                                            record_date=e["record_date"], ex_date=e["exchange_ex_date"],
                                            payment_date=e["payment_date"], cash_per_unit=e["cash_per_unit"],
                                            withholding_rate=source.get("withholding_rate", "0"),
                                            source=f"etf_dividend_snapshot:{source['version']}", known_at=str(e.get("known_at"))))
    events = []
    for row in records:
        item = dict(row)
        for key in ("record_date", "ex_date", "payment_date"):
            if isinstance(item[key], str):
                item[key] = date.fromisoformat(item[key])
            if not isinstance(item[key], date):
                raise ValueError("Missing dividend event date")
        events.append(CashDividend(**item))
    idle = config.get("idle_cash", {})
    income = IdleCashIncome(**{k: v for k, v in idle.items() if k != "enabled"}) if idle.get("enabled", False) else None
    return AccountingEvents(events, income)
