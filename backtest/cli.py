"""Public CLI for the AKShare daily branch."""
import argparse
import json
from pathlib import Path
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from .models import Bar, D

from .akshare_daily import run, update, validate
from .runner import save_result
from .storage import json_default


def main():
    parser = argparse.ArgumentParser(description='AKShare 股票、期货日线回测')
    commands = parser.add_subparsers(dest='command', required=True)
    pull = commands.add_parser('update', help='Download raw daily AKShare data into an immutable snapshot')
    pull.add_argument('config')
    pull.add_argument('--root', default='data/akshare')
    pull.add_argument('--validate-only', action='store_true')
    replay = commands.add_parser('run', help='Replay a fixed AKShare daily snapshot offline')
    replay.add_argument('config')
    replay.add_argument('--root', default='data/akshare')
    replay.add_argument('--version', required=True)
    replay.add_argument('--output', default='outputs/akshare-result.json')
    args = parser.parse_args()
    try:
        config = json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
        if args.command == 'update':
            if args.validate_only:
                validate(config)
                print('Valid AKShare daily configuration')
            else:
                print(update(config, args.root))
        else:
            engine, cfg = run(config, args.root, args.version)
            output = Path(args.output)
            save_result(engine, cfg, output, extra_assumptions=[
                'daily_bar_liquidity_not_intraday_path', 'explicit_historical_contract_rules',
                'no_automatic_corporate_actions'])
            print(f'Result: {output.resolve()}')
    except (ValueError, KeyError, TypeError, OSError, ImportError) as error:
        parser.exit(2, f'AKShare daily task failed: {error}\n')


if __name__ == '__main__':
    main()


# Synthetic core regression fixture; not exposed as a CLI command.
def demo_config():
    start = datetime(2026, 9, 21, 1, tzinfo=timezone.utc)
    instruments = [dict(id=i, asset="future", account="futures", exchange="DEMO", product="PAIR", multiplier="10", tick="1", expiry="2026-09-23", delivery="2026-09-23") for i in ("A", "B")]
    frames = []
    for n in range(1, 5):
        # B lacks a bar during the first three opportunities: trigger cleanup.
        ids = ["A"] if n < 4 else ["A", "B"]
        bars = [asdict(Bar(i, start + timedelta(minutes=n-1), start + timedelta(minutes=n), start.date(), D(100 + n), D(110), D(90), D(101 + n), 100)) for i in ids]
        frames.append(dict(time=start + timedelta(minutes=n), open_instruments=["A", "B"], bars=bars))
    cfg = dict(initial_cash="10000", instruments=instruments,
               margin_rules={i: dict(model="future", rate="0.1") for i in ("A", "B")},
               execution=dict(participation="1", fee_per_unit="2"),
               instructions=[dict(at=start, combination=True, legs=[dict(instrument="A", side=1, quantity=2, group="demo-pair"), dict(instrument="B", side=-1, quantity=2, group="demo-pair")])], frames=frames)
    return json.loads(json.dumps(cfg, default=json_default))
