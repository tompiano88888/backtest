"""Validated observations; monetary arithmetic stays Decimal."""
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from backtest.models import D


def positive(value, name, zero=False):
    value = D(value)
    if value < 0 or (not zero and value == 0):
        raise ValueError(f'Invalid {name}')
    return value


@dataclass(frozen=True)
class Contract:
    id: str
    product: str
    exchange: str
    multiplier: Decimal
    tick: Decimal
    expiry: date
    lot: int = 1

    def __post_init__(self):
        if self.exchange != 'CFFEX' or self.product not in ('IF', 'IH', 'IC', 'IM'):
            raise ValueError('This adapter supports CFFEX index futures only')
        positive(self.multiplier, 'multiplier'); positive(self.tick, 'tick')
        if self.lot != 1:
            raise ValueError('Index futures must use integer contracts')


@dataclass(frozen=True)
class ContractRules:
    """All values explicit, including valid zero fees. No economic defaults."""
    instrument: str
    trading_day: date
    long_margin_rate: Decimal
    short_margin_rate: Decimal
    open_rate: Decimal
    open_per_lot: Decimal
    close_rate: Decimal
    close_per_lot: Decimal
    close_today_rate: Decimal
    close_today_per_lot: Decimal
    source: str
    confirmed_at: datetime
    valid_until: date

    def __post_init__(self):
        for name in ('long_margin_rate', 'short_margin_rate'):
            if not 0 < D(getattr(self, name)) <= 1:
                raise ValueError(f'Invalid {name}')
        for name in ('open_rate','open_per_lot','close_rate','close_per_lot','close_today_rate','close_today_per_lot'):
            positive(getattr(self,name),name,zero=True)
        if not self.source.strip() or self.confirmed_at.tzinfo is None or self.valid_until < self.trading_day:
            raise ValueError('Unconfirmed or expired contract rules')

    def fee(self, contract, quantity, price, effect):
        prefix = 'open' if effect == 'open' else 'close'
        return quantity * (abs(D(price))*contract.multiplier*D(getattr(self,prefix+'_rate'))
                           + D(getattr(self,prefix+'_per_lot')))

    def margin(self, contract, quantity, price, side):
        return quantity*abs(D(price))*contract.multiplier*D(self.long_margin_rate if side>0 else self.short_margin_rate)


@dataclass(frozen=True)
class CounterAccount:
    equity: Decimal
    margin: Decimal
    frozen_margin: Decimal
    available: Decimal
    risk: Decimal
    commission: Decimal
    queried_at: datetime
    previous_balance: Decimal | None = None
    deposit: Decimal | None = None
    withdrawal: Decimal | None = None

    def __post_init__(self):
        positive(self.equity,'equity')
        for name in ('margin','frozen_margin','risk','commission'):
            positive(getattr(self,name),name,zero=True)
        D(self.available)
        if self.queried_at.tzinfo is None:
            raise ValueError('Aware query time required')
        # Installed AccountData.risk is margin / dynamic_rights, NOT a percent.
        if abs(self.risk-self.margin/self.equity) > D('.0001'):
            raise ValueError('Counter risk scale or account snapshot is inconsistent')


@dataclass(frozen=True)
class Quote:
    price: Decimal
    bid: Decimal
    ask: Decimal
    time: datetime
    trading_day: date
    upper_limit: Decimal
    lower_limit: Decimal

    def limit_price(self, contract, buy, payup=1):
        reference = self.ask if buy else self.bid
        positive(reference,'executable quote')
        price = reference + (1 if buy else -1)*payup*contract.tick
        if not self.lower_limit <= price <= self.upper_limit:
            raise ValueError('Order would cross a limit price')
        if price % contract.tick:
            raise ValueError('Quote not aligned to tick')
        return price
