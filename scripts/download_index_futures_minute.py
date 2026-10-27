"""Resumable vendor-timestamp landing, anchored to an immutable daily snapshot."""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from backtest.ifind import IFinDSource
from backtest.storage import json_default

INDICATORS = 'open;high;low;close;volume;amount;change;sellVolume;buyVolume;openInterest;changeRatio_periodical'
FIELDS = INDICATORS.split(';')


def validate(records, code, days, *, fields=FIELDS, required=None, integer_volume=True):
    if required is None:
        required = ('open', 'high', 'low', 'close', 'volume', 'amount', 'openInterest')
    rows = {}
    for record in records:
        stamp = datetime.fromisoformat(record['time'])
        if record['thscode'] != code or stamp.date() not in days:
            raise ValueError(f'Unexpected contract/date: {code}/{stamp}')
        row = dict(instrument=code, source_timestamp=stamp)
        for field in fields:
            value = record.get(field)
            row[field] = None if value is None else Decimal(str(value))
            if row[field] is not None and not row[field].is_finite():
                raise ValueError(f'Nonfinite {field}: {code}/{stamp}')
        if any(row[f] is None for f in required):
            raise ValueError(f'Missing required field: {code}/{stamp}')
        if not (0 < row['low'] <= min(row['open'], row['close']) <= max(row['open'], row['close']) <= row['high']):
            raise ValueError(f'Invalid OHLC: {code}/{stamp}')
        for field in ('volume', 'amount', 'openInterest', 'sellVolume', 'buyVolume'):
            if row.get(field) is not None and row[field] < 0:
                raise ValueError(f'Negative {field}: {code}/{stamp}')
        if integer_volume and row['volume'] is not None and row['volume'] != row['volume'].to_integral_value():
            raise ValueError('Noninteger vendor volume')
        if stamp in rows and rows[stamp] != row:
            raise ValueError('Conflicting duplicate timestamp')
        rows[stamp] = row
    if {s.date() for s in rows} != days:
        raise ValueError(f'Missing daily-reference dates: {code}')
    return [rows[s] for s in sorted(rows)]


def main(dataset='futures'):
    is_spot = dataset == 'index_etf'
    namespace = 'index_etf' if is_spot else 'futures'
    indicators = 'open;high;low;close;volume;amount' if is_spot else INDICATORS
    fields = indicators.split(';')
    parameters = 'Interval:1,CPS:no,Fill:Original' if is_spot else 'Fill:Original'
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default='data/index_etf_minute' if is_spot else 'data/index_futures_minute')
    parser.add_argument('--plan', default='outputs/ifind-index-etf-minute-plan.json' if is_spot else 'outputs/ifind-index-futures-minute-plan.json')
    args = parser.parse_args()
    plan = json.loads(Path(args.plan).read_text(encoding='utf-8'))
    daily_root = Path('data/index_etf' if is_spot else 'data/index_futures')
    with duckdb.connect(str(daily_root / 'metadata.duckdb'), read_only=True) as db:
        result = db.execute(f'SELECT manifest FROM {namespace}_daily_snapshots WHERE version=?', [plan['source_daily_version']]).fetchone()
    if not result:
        raise ValueError('Missing pinned daily snapshot')
    expected = {}
    for entry in json.loads(result[0]):
        path = daily_root / entry['path']
        if hashlib.sha256(path.read_bytes()).hexdigest() != entry['sha256']:
            raise ValueError('Daily snapshot checksum mismatch')
        days = {d for d in pq.read_table(path, columns=['trading_date']).column('trading_date').to_pylist()
                if date.fromisoformat(plan['start']) <= d <= date.fromisoformat(plan['end'])}
        if days:
            expected[entry['instrument']] = days
    if set(expected) != {c['instrument'] for c in plan['contracts']}:
        raise ValueError('Plan contracts differ from daily reference')
    root = Path(args.root)
    root.mkdir(parents=True, exist_ok=True)
    schema = pa.schema([('instrument', pa.string()), ('source_timestamp', pa.timestamp('us'))] +
                       [(f, pa.string()) for f in fields])
    def validate_batch(records, code, days):
        if not is_spot:
            return validate(records, code, days)
        return validate(records, code, days, fields=fields,
                        required=('open', 'high', 'low', 'close'),
                        integer_volume=plan['instruments'][code]['asset'] == 'fund')
    tasks = []
    for code, days in sorted(expected.items()):
        start = min(days)
        while start <= max(days):
            end = min(start + timedelta(days=29), max(days))
            subset = {d for d in days if start <= d <= end}
            if subset:
                tasks.append((code, start, end, subset))
            start = end + timedelta(days=1)
    manifest = []
    with IFinDSource() as source:
        for index, (code, start, end, days) in enumerate(tasks, 1):
            request = dict(function='THS_HF', codes=code, indicators=indicators, parameters=parameters,
                           start=f'{start} 00:00:00', end=f'{end} 23:59:59')
            key = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
            raw = root / 'staging' / f'{key}.json'
            raw.parent.mkdir(parents=True, exist_ok=True)
            if raw.exists():
                cached = json.loads(raw.read_text(encoding='utf-8'))
                if cached['request'] != request:
                    raise ValueError('Cache request mismatch')
                records = cached['records']
            else:
                records = source.query(request)
                temp = raw.with_suffix('.tmp')
                temp.write_text(json.dumps(dict(request=request, records=records), default=json_default), encoding='utf-8')
                temp.replace(raw)
            rows = validate_batch(records, code, days)
            # String decimals preserve every supplier digit; no float conversion or rounding.
            encoded = [{k: str(v) if isinstance(v, Decimal) else v for k, v in r.items()} for r in rows]
            path = root / 'partitions' / f'{key}.parquet'
            path.parent.mkdir(parents=True, exist_ok=True)
            table = pa.Table.from_pylist(encoded, schema=schema)
            if not path.exists():
                temp = path.with_suffix('.tmp')
                pq.write_table(table, temp, compression='zstd')
                temp.replace(path)
            if pq.read_table(path).to_pylist() != encoded:
                raise ValueError('Parquet roundtrip/cache mismatch')
            manifest.append(dict(instrument=code, path=path.relative_to(root).as_posix(), rows=len(rows),
                                 first=str(rows[0]['source_timestamp']), last=str(rows[-1]['source_timestamp']),
                                 dates=len(days), sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                                 raw_path=raw.relative_to(root).as_posix(), raw_sha256=hashlib.sha256(raw.read_bytes()).hexdigest()))
            progress = dict(completed=index, total=len(tasks), rows=sum(e['rows'] for e in manifest), latest=manifest[-1])
            (root / 'progress.json').write_text(json.dumps(progress, indent=2), encoding='utf-8')
            print(f'{index}/{len(tasks)} {code} {start}..{end}: {len(rows)} rows', flush=True)
    report = dict(source_daily_version=plan['source_daily_version'], contracts=len(expected),
                  contract_days=sum(len(d) for d in expected.values()), rows=sum(e['rows'] for e in manifest),
                  start=plan['start'], end=plan['end'], source='iFinD THS_HF', parameters=parameters,
                  limitations='Vendor timestamps retained without start/end interpretation; not Engine Bars. '
                  'Date coverage matches pinned daily data; intraday completeness and vendor volume/amount units not independently verified.',
                  decimal_encoding='Exact decimal strings in Parquet; no float conversion', manifest=manifest)
    fingerprint = hashlib.sha256(json.dumps(report, sort_keys=True).encode()).hexdigest()
    with duckdb.connect(str(root / 'metadata.duckdb')) as db:
        db.execute(f'CREATE TABLE IF NOT EXISTS {namespace}_minute_snapshots(version VARCHAR PRIMARY KEY, created_at TIMESTAMPTZ, report JSON)')
        db.execute(f'INSERT INTO {namespace}_minute_snapshots VALUES (?, ?, ?) ON CONFLICT DO NOTHING',
                   [fingerprint, datetime.now(timezone.utc), json.dumps(report)])
    report['version'] = fingerprint
    (root / 'validation.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(f'Published {fingerprint}: {report["rows"]} rows', flush=True)


if __name__ == '__main__':
    main()
