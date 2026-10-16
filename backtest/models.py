from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum


def D(value=0) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("Non-finite monetary value")
    return result


class Asset(StrEnum):
    EQUITY = "equity"
    FUND = "fund"
    FUTURE = "future"
    OPTION = "option"
    INDEX = "index"


class Account(StrEnum):
    SECURITY = "security"
    FUTURES = "futures"
    ETF_OPTION = "etf_option"


@dataclass(frozen=True)
class Instrument:
    id: str
    asset: Asset
    account: Account
    exchange: str
    product: str
    multiplier: Decimal = Decimal(1)
    tick: Decimal = Decimal("0.01")
    lot: int = 1
    expiry: date | None = None
    delivery: date | None = None
    underlying: str | None = None
    strike: Decimal | None = None
    call: bool = True
    sell_delay_days: int = 0

    def __post_init__(self):
        if self.multiplier <= 0 or self.tick <= 0 or type(self.lot) is not int or self.lot < 1:
            raise ValueError("Invalid contract units")
        if self.asset in (Asset.FUTURE, Asset.OPTION) and (not self.expiry or not self.delivery):
            raise ValueError("Derivative requires expiry and delivery dates")
        if self.asset == Asset.OPTION and (not self.underlying or self.strike is None):
            raise ValueError("Option requires underlying and strike")
        if self.expiry and self.delivery and self.expiry > self.delivery:
            raise ValueError("Expiry cannot follow delivery")


@dataclass(frozen=True)
class Bar:
    instrument: str
    start: datetime
    end: datetime
    trading_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    settlement_price: Decimal | None = None
    upper_limit: Decimal | None = None
    lower_limit: Decimal | None = None
    suspended: bool = False
    turnover: Decimal | None = None
    open_interest: Decimal | None = None
    previous_settlement_price: Decimal | None = None

    def __post_init__(self):
        if self.start.tzinfo is None or self.end.tzinfo is None or self.start >= self.end:
            raise ValueError("Bar requires aware start < end")
        if type(self.volume) is not int or self.volume < 0 or self.low > min(self.open, self.close) or self.high < max(self.open, self.close) or self.low > self.high:
            raise ValueError("Invalid OHLCV")
        if any(not p.is_finite() for p in (self.open, self.high, self.low, self.close)):
            raise ValueError("Invalid price")
        for value in (self.settlement_price, self.previous_settlement_price, self.upper_limit, self.lower_limit, self.turnover, self.open_interest):
            if value is not None and not value.is_finite():
                raise ValueError("Non-finite optional field")


@dataclass(frozen=True)
class Instruction:
    instrument: str
    side: int  # position direction: +1 long, -1 short
    quantity: int
    effect: str = "open"
    group: str = "standalone"

    def __post_init__(self):
        if self.side not in (-1, 1) or type(self.quantity) is not int or self.quantity <= 0 or self.effect not in ("open", "close"):
            raise ValueError("Invalid instruction")

    @property
    def buy(self) -> bool:
        return (self.side == 1) == (self.effect == "open")


@dataclass
class Lot:
    instrument: str
    side: int
    quantity: int
    entry: Decimal
    basis: Decimal
    trading_date: date
    group: str


@dataclass(frozen=True)
class Fill:
    time: datetime | date
    trading_date: date
    instruction: Instruction
    price: Decimal
    fee: Decimal


@dataclass(frozen=True)
class MarginRule:
    model: str  # future, index_option, etf_option, future_option
    rate: Decimal
    floor: Decimal = Decimal("0.5")
    underlying_rate: Decimal = Decimal(0)

    def __post_init__(self):
        if self.model not in {"future", "index_option", "etf_option", "future_option"}:
            raise ValueError("Unknown margin model")
        if min(self.rate, self.floor, self.underlying_rate) < 0:
            raise ValueError("Negative margin parameter")
