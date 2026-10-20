"""Download confirmed iFinD historical fees for the pinned actual-contract universe."""
import argparse
from datetime import datetime, timezone
import json
import time
from pathlib import Path
from backtest.ifind import IFinDSource, IFinDError
from backtest.storage import json_default

from backtest.ifind_fees import DAILY_VERSION, request_for, cache_path, universe


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--daily-root', default='data/index_futures')
    parser.add_argument('--daily-version', default=DAILY_VERSION)
    parser.add_argument('--root', default='data/execution_fees')
    parser.add_argument('--cache', default='data/execution_fees/staging/20261020')
    parser.add_argument('--output', default='outputs/ifind_futures_fees_import')
    parser.add_argument('--download-only', action='store_true')
    parser.add_argument('--publish-only', action='store_true')
    args = parser.parse_args()
    if args.download_only and args.publish_only:
        parser.error('Choose download-only or publish-only')
    coverage, _ = universe(args.daily_root, args.daily_version)
    cache, output = Path(args.cache), Path(args.output)
    cache.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    status = dict(daily_version=args.daily_version, expected_contracts=len(coverage), completed=0,
                  rows=0, status='downloading')
    def save():
        status['updated_at'] = datetime.now(timezone.utc).isoformat()
        tmp = output / 'download_status.tmp'
        tmp.write_text(json.dumps(status, indent=2), encoding='utf-8')
        for attempt in range(6):
            try:
                tmp.replace(output / 'download_status.json')
                break
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(.1 * (attempt + 1))
    source = None
    try:
        for code, dates in sorted(coverage.items()):
            request = request_for(code, dates)
            target = cache_path(cache, request)
            if not target.exists():
                if args.publish_only:
                    raise ValueError(f'Missing cached contract: {code}')
                if source is None:
                    source = IFinDSource()
                    source.login()
                records = source.fetch(request)
                batch = dict(request=request, observed_at=datetime.now(timezone.utc).isoformat(), records=records)
                if not records or any(r.get('thscode') != code for r in records):
                    raise ValueError(f'Incomplete/unexpected fee response: {code}')
                tmp = target.with_suffix('.tmp')
                tmp.write_text(json.dumps(batch, default=json_default, ensure_ascii=False), encoding='utf-8')
                tmp.replace(target)
            batch = json.loads(target.read_text(encoding='utf-8'))
            if batch['request'] != request:
                raise ValueError('Cached request mismatch')
            status['completed'] += 1
            status['rows'] += len(batch['records'])
            if status['completed'] % 25 == 0 or status['completed'] == len(coverage):
                save()
                print(f"{status['completed']}/{len(coverage)} contracts cached; rows={status['rows']}", flush=True)
    except Exception as error:
        status['status'] = 'failed'
        status['error'] = str(error) if isinstance(error, (IFinDError, ValueError, KeyError)) else type(error).__name__
        save()
        raise
    finally:
        if source is not None:
            source.close()
    status['status'] = 'download_complete'
    save()
    if not args.download_only:
        try:
            from backtest.ifind_fees import publish_cached
            report = publish_cached(args.daily_root, args.daily_version, args.root, args.cache)
            (output / 'validation.json').write_text(json.dumps(report, default=json_default, indent=2), encoding='utf-8')
            status.update(status='published', version=report['version'])
            save()
            print(f"Published {report['version']}: {report['contracts']} contracts", flush=True)
        except Exception as error:
            status.update(status='publication_failed', error=str(error) if isinstance(error, (ValueError, KeyError)) else type(error).__name__)
            save()
            raise


if __name__ == '__main__':
    main()
