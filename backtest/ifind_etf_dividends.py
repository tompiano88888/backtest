"""Historical ETF dividend observations; provider fields stay at this boundary."""
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import re

from .storage import json_default

DATES = {
    'announcement_date': 'ths_fund_dividend_announcement_date_fund',
    'record_date': 'ths_fund_equity_registration_date_fund',
    'payment_date': 'ths_fund_bonus_date_fund',
    'otc_ex_date': 'ths_fund_otc_dx_date_fund',
    'exchange_ex_date': 'ths_fund_exchange_dx_date_fund',
    'base_date': 'ths_base_date_of_dividends_fund',
}
COUNT = 'ths_accum_dividend_times_fund'
UNIT = 'ths_unit_accum_dividend_fund'
PLAN = 'ths_fund_income_distribution_plan_fund'
SHANGHAI = timezone(timedelta(hours=8))


def parse_date(value):
    if value in (None, ''):
        return None
    if not isinstance(value, str):
        raise ValueError('Invalid dividend date')
    return date.fromisoformat(value if '-' in value else f'{value[:4]}-{value[4:6]}-{value[6:]}')


def event(record):
    dates = {k: parse_date(record.get(v)) for k, v in DATES.items()}
    if not any(dates.values()) and record.get(PLAN) in (None, ''):
        return None
    if any(dates[k] is None for k in ('announcement_date', 'record_date', 'payment_date', 'exchange_ex_date')):
        raise ValueError(f"Incomplete dividend dates: {record['thscode']}")
    plan = record.get(PLAN) or ''
    match = re.fullmatch(r'每([0-9]+(?:\.[0-9]+)?)份基金(?:份额)?(?:派发(?:红利|现金人民币|现金红利|现金收益)|发放红利|单位派)([0-9]+(?:\.[0-9]+)?)元[。.]?', plan.strip())
    if not match or Decimal(match[1]) <= 0:
        raise ValueError(f"Unsupported dividend plan (preserved in cache): {record['thscode']}")
    if not dates['announcement_date'] <= dates['record_date'] <= dates['exchange_ex_date'] <= dates['payment_date']:
        raise ValueError(f"Invalid dividend timeline: {record['thscode']}")
    return dict(instrument=record['thscode'], **dates, cash_per_unit=Decimal(match[2])/Decimal(match[1]),
                currency='CNY', distribution_plan=plan, source='iFinD THS_BD')


def request_at(template, codes, day):
    """Only substitute the date/year parameters supplied by the user."""
    params = template['parameters'].split(';')
    fields = template['indicators'].split(';')
    if len(params) != len(fields) or len(fields) != 17:
        raise ValueError('Expected the confirmed 17-field request')
    for i, value in enumerate(params):
        if i < 3:
            params[i] = str(day.year)
        elif value:
            params[i] = ','.join(day.isoformat() for _ in value.split(','))
    return dict(template, codes=','.join(codes), parameters=';'.join(params))


def publish(store, histories, cutoff):
    """Publish the complete universe atomically after count/amount reconciliation."""
    if not histories:
        raise ValueError('Nonempty ETF universe required')
    events, observations = {}, {}
    for code, batches in sorted(histories.items()):
        if not batches:
            raise ValueError('Empty history')
        if date.fromisoformat(batches[0]['query_date']) != cutoff:
            raise ValueError('Latest observation must match cutoff')
        seen, rows = {}, []
        previous = cutoff + timedelta(days=1)
        for batch in batches:
            stamp = datetime.fromisoformat(batch['observed_at'])
            if stamp.tzinfo is None:
                raise ValueError('Observation requires timezone')
            record = batch['record']
            if record.get('thscode') != code:
                raise ValueError('History code mismatch')
            query_day = date.fromisoformat(batch['query_date'])
            if query_day >= previous:
                raise ValueError('History dates must decrease')
            item = event(record)
            rows.append(batch)
            if item is None:
                if batch is not batches[-1]:
                    raise ValueError('Premature empty observation')
                break
            if item['announcement_date'] > query_day or item['announcement_date'] in seen:
                raise ValueError('Nonhistorical/repeated dividend response')
            item['known_at'] = stamp
            seen[item['announcement_date']] = item
            previous = item['announcement_date']
        else:
            raise ValueError('History has no terminal empty observation')
        latest = batches[0]['record']
        # The supplied cutoff also returns already announced future distributions.
        # Preserve them, but do not add them to completed historical totals.
        completed = [x for x in seen.values() if x['exchange_ex_date'] <= cutoff]
        count = latest.get(COUNT)
        if (count is None and completed) or (count is not None and Decimal(str(count)) != len(completed)):
            raise ValueError(f'Cumulative dividend count mismatch: {code}')
        amount = latest.get(UNIT)
        total = sum((x['cash_per_unit'] for x in completed), Decimal(0))
        if (completed and amount is None) or (amount is not None and total != Decimal(str(amount))):
            raise ValueError(f'Cumulative unit dividend mismatch: {code}')
        events[code] = sorted(seen.values(), key=lambda x: x['announcement_date'])
        observations[code] = rows
    fingerprint = hashlib.sha256(json.dumps([cutoff, histories], sort_keys=True, default=json_default).encode()).hexdigest()
    store.db.execute('''CREATE TABLE IF NOT EXISTS etf_dividend_snapshots (
        version VARCHAR PRIMARY KEY, created_at TIMESTAMPTZ, fingerprint VARCHAR,
        revisions JSON, report JSON)''')
    old = store.db.execute('SELECT version, report FROM etf_dividend_snapshots WHERE fingerprint=?', [fingerprint]).fetchone()
    if old:
        return old[0], json.loads(old[1])
    report = dict(version=fingerprint, cutoff=str(cutoff), instruments=len(histories),
                  events=sum(map(len, events.values())), no_reported_events=sum(not v for v in events.values()),
                  announced_future_events=sum(x['exchange_ex_date'] > cutoff for items in events.values() for x in items),
                  count_and_amount_reconciled=True, known_at_policy='retrieval time; no historical PIT backdating')
    report['coverage'] = {code: dict(events=len(items),
        first_announcement=str(items[0]['announcement_date']) if items else None,
        last_announcement=str(items[-1]['announcement_date']) if items else None,
        cash_per_unit_total=str(sum((x['cash_per_unit'] for x in items), Decimal(0))),
        completed_events=sum(x['exchange_ex_date'] <= cutoff for x in items),
        completed_cash_per_unit_total=str(sum((x['cash_per_unit'] for x in items if x['exchange_ex_date'] <= cutoff), Decimal(0))),
        vendor_cumulative_count=observations[code][0]['record'].get(COUNT),
        status='reconciled_events' if items else 'no_reported_events') for code, items in events.items()}
    revisions = {}
    store.db.execute('BEGIN TRANSACTION')
    try:
        for code, items in events.items():
            revisions[code] = []
            for item in items:
                start = datetime.combine(item['exchange_ex_date'], time.min, SHANGHAI)
                revision = store.put_metadata('etf_dividend', f"{code}/{item['announcement_date']}", start,
                                              item['known_at'], item, start + timedelta(days=1))
                revisions[code].append(revision)
            for batch in observations[code]:
                stamp = datetime.fromisoformat(batch['observed_at'])
                store.put_metadata('ifind_etf_dividend_observation', f"{code}/{batch['query_date']}", stamp, stamp, batch)
        store.db.execute('INSERT INTO etf_dividend_snapshots VALUES (?, ?, ?, ?, ?)',
                         [fingerprint, datetime.now(timezone.utc), fingerprint, json.dumps(revisions), json.dumps(report)])
        store.db.execute('COMMIT')
    except Exception:
        store.db.execute('ROLLBACK')
        raise
    return fingerprint, report


def read_events(store, version, code, *, as_of=None):
    """Pinned ex-post accounting data; supply as_of for conservative PIT filtering."""
    row = store.db.execute('SELECT revisions FROM etf_dividend_snapshots WHERE version=?', [version]).fetchone()
    if not row:
        raise KeyError(version)
    revisions = json.loads(row[0])
    if code not in revisions:
        raise KeyError(code)
    if as_of is not None and as_of.tzinfo is None:
        raise ValueError('as_of requires timezone')
    result = []
    for revision in revisions[code]:
        payload, known = store.db.execute('SELECT payload, known_at FROM metadata WHERE revision=?', [revision]).fetchone()
        if as_of is None or known <= as_of:
            item = json.loads(payload)
            item['cash_per_unit'] = Decimal(item['cash_per_unit'])
            for key in DATES:
                item[key] = parse_date(item[key])
            result.append(item)
    return result
