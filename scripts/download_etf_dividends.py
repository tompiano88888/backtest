"""Cache every prior dividend, reconcile cumulative totals, publish one fixed snapshot."""
import argparse
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

from backtest.ifind import IFinDSource
from backtest.ifind_etf_dividends import DATES, parse_date, publish, request_at
from backtest.storage import Store, json_default


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request', default='examples/ifind.etf-dividends.json')
    parser.add_argument('--root', default='data/etf_dividends')
    parser.add_argument('--cache', help='Separate cache directory for a fresh provider revision')
    parser.add_argument('--cutoff', default='2026-10-12')
    parser.add_argument('--batch-size', type=int, default=50)
    parser.add_argument('--codes', help='Optional sample subset of the supplied universe')
    parser.add_argument('--publish-only', action='store_true')
    args = parser.parse_args()
    if args.batch_size < 1:
        parser.error('Positive batch size required')
    template = json.loads(Path(args.request).read_text(encoding='utf-8-sig'))['request']
    codes = template['codes'].split(',')
    if args.codes:
        subset = args.codes.split(',')
        if not set(subset) <= set(codes):
            parser.error('Sample codes must belong to the supplied universe')
        codes = subset
    if len(set(codes)) != len(codes):
        parser.error('Duplicate codes')
    cutoff = date.fromisoformat(args.cutoff)
    cache = Path(args.cache) if args.cache else Path(args.root) / 'staging'
    cache.mkdir(parents=True, exist_ok=True)
    histories = {code: [] for code in codes}
    pending = {code: cutoff for code in codes}
    source = None
    queries = 0
    try:
        while pending:
            groups = {}
            for code, day in pending.items():
                groups.setdefault(day, []).append(code)
            next_pending = {}
            for day, group in sorted(groups.items(), reverse=True):
                for offset in range(0, len(group), args.batch_size):
                    selected = sorted(group[offset:offset+args.batch_size])
                    request = request_at(template, selected, day)
                    digest = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
                    path = cache / f'{digest}.json'
                    if not path.exists():
                        if args.publish_only:
                            raise ValueError('Missing cached request')
                        if source is None:
                            source = IFinDSource()
                            source.login()
                        records = source.fetch(request)
                        if len(records) != len(selected) or {r.get('thscode') for r in records} != set(selected):
                            raise ValueError('Incomplete/duplicate provider batch; not published')
                        content = dict(request=request, observed_at=datetime.now(timezone.utc).isoformat(), records=records)
                        temp = path.with_suffix('.tmp')
                        temp.write_text(json.dumps(content, default=json_default, ensure_ascii=False), encoding='utf-8')
                        temp.replace(path)
                    content = json.loads(path.read_text(encoding='utf-8'))
                    if content['request'] != request or len(content['records']) != len(selected) or {r.get('thscode') for r in content['records']} != set(selected):
                        raise ValueError('Invalid cache batch')
                    for record in content['records']:
                        code = record['thscode']
                        histories[code].append(dict(query_date=str(day), observed_at=content['observed_at'], record=record))
                        announcement = parse_date(record.get(DATES['announcement_date']))
                        if announcement:
                            if announcement > day:
                                raise ValueError('Provider returned future event')
                            next_pending[code] = announcement - timedelta(days=1)
                    queries += 1
                    if queries == 1 or queries % 25 == 0:
                        print(f'queries={queries}, histories={sum(len(v) for v in histories.values())}, remaining_wave={len(pending)}', flush=True)
            pending = next_pending
    finally:
        if source is not None:
            source.close()
    with Store(args.root) as store:
        version, report = publish(store, histories, cutoff)
    output = Path(args.root) / f'validation-{version}.json'
    output.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in report.items() if k != 'coverage'}), flush=True)


if __name__ == '__main__':
    main()
