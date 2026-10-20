"""iFinD fee adapter: preserve observations, validate coverage, publish temporal rules."""
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import hashlib
import json
import re
import duckdb
from .models import D
from .fees import FeeRule, publish_fee_snapshot
from .storage import Store, json_default

FIELDS = ('ths_transaction_procedure_rate_future', 'ths_transaction_procedure_fee_future',
          'ths_to_transaction_procedure_rate_future', 'ths_to_transaction_procedure_fee_future')
DAILY_VERSION = '0fff65706ce54991a3725f1bcbb6c6c9'


def request_for(code, coverage):
    return dict(function='THS_DS', codes=code, indicators=';'.join(FIELDS), parameters=';;;',
                global_parameters='Interval:D,Days:Tradedays,Fill:Blank',
                start=coverage['first'], end=coverage['last'])


def cache_path(cache, request):
    digest = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
    return Path(cache) / (digest + '.json')


def universe(root, version):
    with duckdb.connect(str(Path(root) / 'metadata.duckdb'), read_only=True) as db:
        row = db.execute('SELECT report,manifest FROM futures_daily_snapshots WHERE version=?', [version]).fetchone()
    if not row:
        raise KeyError('Missing pinned daily snapshot')
    return json.loads(row[0])['contracts'], json.loads(row[1])


RATE_DIVISOR = D(1000)
SOURCE = 'iFinD THS_DS historical contract fees; native rate / 1000; observed knowledge time'
SHANGHAI = ZoneInfo('Asia/Shanghai')


def normalize_record(record):
    code = record.get('thscode', '')
    if not re.fullmatch(r'(IF|IH|IC|IM)\d{4}\.CFE', code):
        raise ValueError('Expected actual stock-index futures contract')
    day = date.fromisoformat(record['time'])
    values = []
    for rate_field, unit_field in (FIELDS[:2], FIELDS[2:]):
        rate, unit = record.get(rate_field), record.get(unit_field)
        if rate is None and unit is None:
            raise ValueError(f'Missing fee pair: {code}/{day}/{rate_field}')
        rate = D(0) if rate is None else D(rate) / RATE_DIVISOR
        unit = D(0) if unit is None else D(unit)
        if min(rate, unit) < 0:
            raise ValueError('Negative fee')
        values.append((rate, unit))
    normal, today = values
    payload = dict(open_rate=normal[0], open_per_unit=normal[1], close_rate=normal[0],
                   close_per_unit=normal[1], close_today_rate=today[0],
                   close_today_per_unit=today[1], matching='today_first', source=SOURCE)
    FeeRule(**payload)
    return code, day, payload


def normalize_batch(batch, code, expected_days):
    stamp = datetime.fromisoformat(batch['observed_at'])
    if stamp.tzinfo is None:
        raise ValueError('Observation must have aware timestamp')
    by_day = {}
    for raw in batch['records']:
        actual, day, payload = normalize_record(raw)
        if actual != code or day in by_day:
            raise ValueError(f'Unexpected/duplicate fee row: {code}/{day}')
        by_day[day] = payload
    if set(by_day) != set(expected_days):
        missing = sorted(set(expected_days) - set(by_day))
        extra = sorted(set(by_day) - set(expected_days))
        raise ValueError(f'Fee/calendar coverage mismatch: {code} missing={missing[:3]} extra={extra[:3]}')
    # Segments span verified trading days. No unverified observations are forward-filled.
    segments = []
    for day, payload in sorted(by_day.items()):
        if not segments or segments[-1][2] != payload:
            if segments:
                segments[-1][1] = day
            segments.append([day, day + timedelta(days=1), payload])
        else:
            segments[-1][1] = day + timedelta(days=1)
    return stamp, by_day, segments


def publish_cached(daily_root, daily_version, root, cache):
    import pyarrow.parquet as pq
    coverage, manifest = universe(daily_root, daily_version)
    entries = {row['instrument']: row for row in manifest}
    if set(entries) != set(coverage):
        raise ValueError('Daily manifest contract mismatch')
    prepared, hashes, product_rows = {}, {}, {}
    for code, dates in sorted(coverage.items()):
        request = request_for(code, dates)
        target = cache_path(cache, request)
        content = target.read_bytes()
        hashes[code] = hashlib.sha256(content).hexdigest()
        batch = json.loads(content)
        if batch['request'] != request:
            raise ValueError('Fee request mismatch')
        entry = entries[code]
        path = (Path(daily_root) / entry['path']).resolve()
        if not path.is_relative_to(Path(daily_root).resolve()):
            raise ValueError('Invalid daily path')
        if hashlib.sha256(path.read_bytes()).hexdigest() != entry['sha256']:
            raise ValueError('Daily SHA256 mismatch')
        expected = pq.read_table(path, columns=['trading_date']).column('trading_date').to_pylist()
        if len(set(expected)) != dates['rows']:
            raise ValueError('Daily row count mismatch')
        stamp, by_day, segments = normalize_batch(batch, code, expected)
        prepared[code] = batch, stamp, by_day, segments
        product_rows[code[:2]] = product_rows.get(code[:2], 0) + len(by_day)
    fingerprint = hashlib.sha256(json.dumps([daily_version, hashes, str(RATE_DIVISOR)], sort_keys=True).encode()).hexdigest()
    with Store(root) as store:
        store.db.execute("""CREATE TABLE IF NOT EXISTS futures_fee_snapshots (
            version VARCHAR PRIMARY KEY, created_at TIMESTAMPTZ, daily_version VARCHAR,
            fingerprint VARCHAR UNIQUE, revisions JSON, report JSON)""")
        existing = store.db.execute('SELECT report FROM futures_fee_snapshots WHERE fingerprint=?', [fingerprint]).fetchone()
        if existing:
            return json.loads(existing[0])
        report = dict(daily_version=daily_version, contracts=len(prepared), rows=sum(product_rows.values()),
                      product_rows=product_rows, raw_observations=len(prepared),
                      fee_rules=sum(len(v[3]) for v in prepared.values()),
                      as_of=max(v[1] for v in prepared.values()).isoformat(),
                      first=min(min(v[2]) for v in prepared.values()).isoformat(),
                      last=max(max(v[2]) for v in prepared.values()).isoformat(),
                      rate_divisor=str(RATE_DIVISOR), cache_sha256=hashes,
                      coverage_errors=0, duplicates=0,
                      limitation='Historical vendor observations; known_at is retrieval time, not historical announcement time. '
                                 'Transaction fees only; delivery/declaration fees and historical margin not covered.')
        revisions = {}
        store.db.execute('BEGIN TRANSACTION')
        try:
            for code, (batch, stamp, by_day, segments) in prepared.items():
                raw_revision = store.put_metadata('ifind_fee_observation', code, stamp, stamp,
                                                 dict(batch=batch, daily_version=daily_version,
                                                      sha256=hashes[code]))
                rule_revisions = []
                for start, end, payload in segments:
                    rule_revisions.append(store.put_metadata('fee_rule', code,
                        datetime.combine(start, time(), SHANGHAI), stamp, payload,
                        valid_to=datetime.combine(end, time(), SHANGHAI)))
                revisions[code] = dict(raw=raw_revision, rules=rule_revisions)
            version = publish_fee_snapshot(store, 'iFinD historical index futures fees; ' + fingerprint)
            report['version'] = version
            store.db.execute('INSERT INTO futures_fee_snapshots VALUES (?, ?, ?, ?, ?, ?)',
                             [version, datetime.now(timezone.utc), daily_version, fingerprint,
                              json.dumps(revisions), json.dumps(report, default=json_default)])
            store.db.execute('COMMIT')
        except Exception:
            store.db.execute('ROLLBACK')
            raise
    return json.loads(json.dumps(report, default=json_default))
