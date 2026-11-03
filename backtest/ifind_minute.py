"""Pinned vendor minute observations, normalized against explicit session facts.

No strategy, inferred trading calendar, forward filling, or execution lives here.
Opening boundary observations remain separate from continuous minute Bars.
"""
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path

import duckdb
import pyarrow.parquet as pq

from .ifind_normalize import IFinDBarNormalizer, SHANGHAI
from .models import Asset, D


class MinuteSnapshot:
    """Read one immutable landing snapshot with bounded partition caching."""

    def __init__(self, root, version, *, dataset, cache_partitions=24):
        tables = {'futures': 'futures_minute_snapshots',
                  'index_etf': 'index_etf_minute_snapshots'}
        if dataset not in tables or type(cache_partitions) is not int or cache_partitions < 1:
            raise ValueError('Invalid minute snapshot configuration')
        self.root = Path(root).resolve()
        self.version = version
        with duckdb.connect(str(self.root / 'metadata.duckdb'), read_only=True) as db:
            found = db.execute(f'SELECT report FROM {tables[dataset]} WHERE version=?', [version]).fetchone()
        if found is None:
            raise ValueError('Pinned minute snapshot not found')
        self.report = json.loads(found[0])
        fingerprint = hashlib.sha256(json.dumps(self.report, sort_keys=True).encode()).hexdigest()
        if fingerprint != version:
            raise ValueError('Minute snapshot report checksum mismatch')
        self.by_code = defaultdict(list)
        for item in self.report['manifest']:
            self.by_code[item['instrument']].append(item)
        self.no_quote_dates = {}
        for item in self.report.get('no_quote_dates', []):
            key = item['instrument'], date.fromisoformat(item['date'])
            if key in self.no_quote_dates or not item.get('reason') or not item.get('source'):
                raise ValueError('Invalid declared no-quote date')
            self.no_quote_dates[key] = item
        self.cache_partitions = cache_partitions
        self.cache = OrderedDict()
        self.verified = set()

    def rows(self, code, day):
        """Missing observations stay missing; only declared whole-day gaps pass."""
        if code not in self.by_code:
            raise ValueError(f'Instrument absent from minute snapshot: {code}')
        if not date.fromisoformat(self.report['start']) <= day <= date.fromisoformat(self.report['end']):
            raise ValueError('Date outside pinned minute snapshot')
        rows = {}
        for item in self.by_code[code]:
            if not item.get('first') or not item['first'][:10] <= str(day) <= item['last'][:10]:
                continue
            key = item['path']
            if key not in self.cache:
                path = (self.root / key).resolve()
                if not path.is_relative_to(self.root) or hashlib.sha256(path.read_bytes()).hexdigest() != item['sha256']:
                    raise ValueError('Minute partition path/checksum mismatch')
                records = pq.read_table(path).to_pylist()
                if len(records) != item['rows']:
                    raise ValueError('Minute partition row count mismatch')
                grouped = defaultdict(list)
                for row in records:
                    if row['instrument'] != code:
                        raise ValueError('Minute partition instrument mismatch')
                    stamp = row['source_timestamp']
                    stamp = stamp.replace(tzinfo=SHANGHAI) if stamp.tzinfo is None else stamp.astimezone(SHANGHAI)
                    grouped[stamp.date()].append(row)
                self.cache[key] = grouped
                self.verified.add(key)
                while len(self.cache) > self.cache_partitions:
                    self.cache.popitem(last=False)
            self.cache.move_to_end(key)
            for row in self.cache[key].get(day, ()):
                stamp = row['source_timestamp']
                if stamp in rows:
                    raise ValueError('Duplicate minute observation')
                rows[stamp] = row
        if (code, day) in self.no_quote_dates:
            if rows:
                raise ValueError('Quote on declared no-quote date')
        elif not rows:
            raise ValueError(f'Undeclared missing minute day: {code}/{day}')
        return [rows[key] for key in sorted(rows)]


def session_minute_ends(calendar, code, day):
    """Generate the expected clock independently of observed rows."""
    sessions = calendar.by_day.get((code, day))
    if not sessions:
        raise ValueError(f'Missing explicit session: {code}/{day}')
    result = []
    for session in sorted(sessions, key=lambda s: s.start):
        if any(t.second or t.microsecond for t in (session.start, session.end)):
            raise ValueError('Session is not minute aligned')
        stamp = session.start + timedelta(minutes=1)
        while stamp <= session.end:
            result.append(stamp)
            stamp += timedelta(minutes=1)
    return tuple(result)


@dataclass
class MinuteDay:
    bars: dict
    observations: dict
    missing_ends: tuple


def normalize_day(rows, code, day, *, profile, calendar, timestamp_at,
                  observation_times=(), turnover_column=None):
    """Normalize a landing day, retaining explicitly excluded discrete observations.

    Index volume is reference data, potentially fractional. Its Bars have zero
    executable volume; raw rows stay intact in the snapshot. No rounding into
    tradable units. Tradable assets require explicit unit multipliers.
    Missing intraday rows are reported, never filled or removed from the clock.
    """
    reference = Asset(profile['asset']) == Asset.INDEX
    config = dict(timestamp_at=timestamp_at, instruments={code: profile},
                  columns=dict(open='open', high='high', low='low', close='close', volume='volume'))
    if turnover_column is not None:
        config['columns']['turnover'] = turnover_column
    normalizer = IFinDBarNormalizer(config, '1m', calendar)
    expected = session_minute_ends(calendar, profile['id'], day)
    expected_set = set(expected)
    bars, observations = {}, {}
    for row in rows:
        if row['instrument'] != code:
            raise ValueError('Wrong minute instrument')
        stamp = row['source_timestamp']
        stamp = stamp.replace(tzinfo=SHANGHAI) if stamp.tzinfo is None else stamp.astimezone(SHANGHAI)
        if stamp.date() != day or stamp.second or stamp.microsecond:
            raise ValueError('Wrong or unaligned minute day')
        if stamp.time() in observation_times:
            if stamp in observations:
                raise ValueError('Duplicate boundary observation')
            prices = [D(row[k]) for k in ('open', 'high', 'low', 'close')]
            if not 0 < prices[2] <= min(prices[0], prices[3]) <= max(prices[0], prices[3]) <= prices[1]:
                raise ValueError('Invalid boundary OHLC')
            observations[stamp] = row
            continue
        adapted = dict(row, thscode=code, time=stamp)
        if reference:
            adapted['volume'] = 0
        bar = normalizer(adapted)
        if bar.trading_date != day or bar.end not in expected_set or bar.end in bars:
            raise ValueError('Unexpected/duplicate normalized minute')
        bars[bar.end] = bar
    return MinuteDay(bars, observations, tuple(t for t in expected if t not in bars))
