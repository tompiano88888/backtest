"""Execution fees, independent of strategy and FIFO cost accounting."""
from dataclasses import dataclass, asdict
from datetime import datetime, time, timezone
from decimal import Decimal
import json
from .models import D


@dataclass(frozen=True)
class FeeRule:
    open_rate: Decimal
    open_per_unit: Decimal
    close_rate: Decimal
    close_per_unit: Decimal
    close_today_rate: Decimal
    close_today_per_unit: Decimal
    matching: str = 'fifo'
    source: str = 'fixed-research'
    revision: str = 'fixed'

    def __post_init__(self):
        for name in ('open_rate', 'open_per_unit', 'close_rate', 'close_per_unit',
                     'close_today_rate', 'close_today_per_unit'):
            value = D(getattr(self, name))
            if value < 0:
                raise ValueError('Negative fee rule')
            object.__setattr__(self, name, value)
        if self.matching not in ('fifo', 'today_first') or not self.source:
            raise ValueError('Invalid fee rule matching/source')

    @classmethod
    def fixed(cls, cfg):
        return cls(cfg.fee_rate, cfg.fee_per_unit, cfg.fee_rate, cfg.fee_per_unit,
                   cfg.fee_rate if cfg.close_today_fee_rate is None else cfg.close_today_fee_rate,
                   cfg.fee_per_unit if cfg.close_today_fee_per_unit is None else cfg.close_today_fee_per_unit,
                   cfg.fee_matching)


class HistoricalFees:
    """Load once from a pinned Store dataset cutoff; no open DB during execution."""
    def __init__(self, root, version, as_of):
        import duckdb
        from pathlib import Path
        if isinstance(as_of, str):
            as_of = datetime.fromisoformat(as_of)
        if as_of.tzinfo is None or not version:
            raise ValueError('Historical fees require version and aware as_of')
        with duckdb.connect(str(Path(root) / 'metadata.duckdb'), read_only=True) as db:
            cutoff = db.execute('SELECT created_at FROM datasets WHERE version=?', [version]).fetchone()
            if not cutoff:
                raise KeyError(f'Missing fee dataset version: {version}')
            rows = db.execute("SELECT key,valid_from,valid_to,known_at,revision,payload FROM metadata "
                              "WHERE kind='fee_rule' AND recorded_at<=? AND known_at<=?",
                              [cutoff[0], as_of]).fetchall()
        self.version, self.as_of = version, as_of
        # Corrections of the same effective interval use latest visible knowledge.
        latest = {}
        for key, start, end, known, revision, payload in rows:
            identity = key, start, end
            previous = latest.get(identity)
            if previous and previous[0] == known and previous[1] != revision:
                raise ValueError(f'Conflicting fee revisions: {key}')
            if previous is None or known > previous[0]:
                latest[identity] = known, revision, payload
        self.records = {}
        for (key, start, end), (_, revision, payload) in latest.items():
            record = json.loads(payload)
            required = {'open_rate', 'open_per_unit', 'close_rate', 'close_per_unit',
                        'close_today_rate', 'close_today_per_unit', 'matching', 'source'}
            if not required <= record.keys():
                raise ValueError(f'Incomplete historical fee rule: {key}')
            rule = FeeRule(**(record | {'revision': revision}))
            self.records.setdefault(key, []).append((start, end, rule))
        for key, records in self.records.items():
            records.sort(key=lambda r: r[0])
            for previous, current in zip(records, records[1:]):
                if previous[1] is None or previous[1] > current[0]:
                    raise ValueError(f'Overlapping fee rules: {key}')

    def resolve(self, instrument, day):
        from zoneinfo import ZoneInfo
        effective = datetime.combine(day, time(), ZoneInfo('Asia/Shanghai'))
        found = [rule for start, end, rule in self.records.get(instrument, ())
                 if start <= effective and (end is None or effective < end)]
        if len(found) != 1:
            raise KeyError(f'Missing historical fee rule: {instrument}/{day}')
        return found[0]


class FeeService:
    def __init__(self, provider=None):
        self.provider = provider
        self.balances = {}
        self.details = []

    def quote(self, leg, quantity, price, day, inst, cfg, lots, balance=None):
        if cfg.fee_mode == 'historical':
            if self.provider is None:
                raise ValueError('Historical fee provider required')
            rule = self.provider.resolve(inst.id, day)
        else:
            rule = FeeRule.fixed(cfg)
        today = 0
        if leg.effect == 'close':
            if rule.matching == 'today_first':
                today = min(quantity, self.balances.get((day, inst.id, leg.side), 0) if balance is None else balance)
            else:
                left = quantity
                for count, is_today in lots:
                    used = min(left, count)
                    today += used if is_today else 0
                    left -= used
                    if not left:
                        break
        normal = quantity - today
        notional = abs(price) * inst.multiplier
        rate = rule.open_rate if leg.effect == 'open' else rule.close_rate
        unit = rule.open_per_unit if leg.effect == 'open' else rule.close_per_unit
        exchange = normal * (unit + notional * rate) + today * (rule.close_today_per_unit + notional * rule.close_today_rate)
        commission = quantity * (cfg.commission_per_unit + notional * cfg.commission_rate)
        return dict(normal_quantity=normal, close_today_quantity=today, exchange_fee=exchange,
                    commission=commission, total=exchange + commission, rule_revision=rule.revision,
                    rule_source=rule.source, matching=rule.matching, mode=cfg.fee_mode)

    def commit(self, fill, detail):
        leg = fill.instruction
        key = fill.trading_date, leg.instrument, leg.side
        # Keep only the latest trading day for this instrument (night sessions use trading_date).
        for old in list(self.balances):
            if old[1] == leg.instrument and old[0] != fill.trading_date:
                del self.balances[old]
        balance = self.balances.get(key, 0)
        self.balances[key] = balance + leg.quantity if leg.effect == 'open' else max(0, balance - leg.quantity)
        self.details.append(dict(time=fill.time, trading_date=fill.trading_date,
                                 instruction=asdict(leg), **detail))


def publish_fee_snapshot(store, source):
    """Pin already imported fee_rule metadata; source must identify the import."""
    import uuid
    if not source:
        raise ValueError('Fee snapshot source required')
    rows = store.db.execute("SELECT payload FROM metadata WHERE kind='fee_rule'").fetchall()
    if not rows:
        raise ValueError('No fee rules to publish')
    required = {'open_rate', 'open_per_unit', 'close_rate', 'close_per_unit',
                'close_today_rate', 'close_today_per_unit', 'matching', 'source'}
    for (payload,) in rows:
        record = json.loads(payload)
        if not required <= record.keys():
            raise ValueError('Incomplete historical fee rule')
        FeeRule(**record)
    version = uuid.uuid4().hex
    store.db.execute('INSERT INTO datasets VALUES (?, ?, ?, ?)',
                     [version, datetime.now(timezone.utc), source, '{}'])
    return version
