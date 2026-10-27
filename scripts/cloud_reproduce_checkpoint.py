"""Portable launcher for the saved legacy research; delegates all execution/accounting."""
import argparse
import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
from decimal import Decimal
from pathlib import Path


def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):h.update(block)
    return h.hexdigest()


def prepare_market_files(root,manifest,data_root=None):
    """Validate all uploaded immutable prices before attaching any missing file."""
    missing=[];pending=[]
    for entry in manifest['market_files']:
        relative=Path(entry['path']);target=root/relative
        source=target if target.exists() else ((data_root/relative.relative_to('data')) if data_root else target)
        if not source.is_file():missing.append(entry['path']);continue
        if digest(source)!=entry['sha256']:raise ValueError(f'Market file checksum mismatch: {entry["path"]}')
        if source!=target:pending.append((source,target))
    if missing:raise FileNotFoundError(f'Missing {len(missing)} fixed-snapshot Parquet files; first 12: '+', '.join(missing[:12]))
    for source,target in pending:
        target.parent.mkdir(parents=True,exist_ok=True)
        try:os.link(source,target)
        except OSError:shutil.copyfile(source,target)


def run_checked(arguments,root):
    subprocess.run([sys.executable,*arguments],cwd=root,check=True)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--data-root',type=Path,help='Uploaded data directory containing index_futures/ and index_etf/')
    parser.add_argument('--output',default='outputs/cloud_reproduction')
    parser.add_argument('--check-only',action='store_true')
    parser.add_argument('--fresh-search',action='store_true',help='Recompute the original rolling 35-way choices instead of replaying frozen choices')
    args=parser.parse_args();root=Path(__file__).resolve().parents[1]
    data_root=args.data_root.resolve() if args.data_root else None
    manifest=json.loads((root/'checkpoint_manifest.json').read_text(encoding='utf-8'))
    for name,expected in manifest['bundle_files'].items():
        if digest(root/name)!=expected:raise ValueError(f'Bundle integrity mismatch: {name}')
    prepare_market_files(root,manifest,data_root)
    os.environ.setdefault('MPLBACKEND','Agg')
    print(f'Preflight passed: {len(manifest["market_files"])} fixed market files; metadata and code integrity verified.',flush=True)
    if args.check_only:return
    output=(root/args.output).resolve();reference=root/'outputs/arbitrage_pooled_optimized_floors'
    if not output.is_relative_to(root/'outputs') or output==reference:
        raise ValueError('Use a new output directory below the bundle outputs/ directory')
    if output.exists() and any(output.iterdir()):raise FileExistsError(f'Output exists; choose another --output: {output}')
    command=['-m','scripts.test_pooled_arbitrage','--funding-exit','--etf','--cancel-new-openings',
        '--dividends','--minimum-return-basis','zero_convergence',
        '--entry-floors','configs/arbitrage_entry_floor_best.json','--output',str(output)]
    if not args.fresh_search:command+=['--rolling-source','outputs/arbitrage_pooled_optimized_floors']
    run_checked(command,root)
    checked={}
    for name,expected in manifest['expected_core_csv_sha256'].items():
        checked[name]=digest(output/name)==expected
    if not all(checked.values()):raise ValueError('Output differs from saved checkpoint: '+str([k for k,v in checked.items() if not v]))
    print('All 19 saved core CSV hashes matched.',flush=True)
    run_checked(['-m','scripts.verify_pooled_outputs','--output',str(output),'--repeat',str(reference),
        '--etf','--cancel-new-openings','--dividends','--original-signal','--require-no-gap'],root)
    with (output/'summary.csv').open(encoding='utf-8-sig',newline='') as stream:summary=next(csv.DictReader(stream))
    with (output/'yearly.csv').open(encoding='utf-8-sig',newline='') as stream:yearly=list(csv.DictReader(stream))
    money=lambda key:f'{Decimal(summary[key])/10000:.2f}'
    report=['# Cloud reproduction of saved research checkpoint','',
        f"Period: {summary['start']} to {summary['end']}; initial capital CNY 10,000,000.",
        f"Final equity: CNY {summary['final_equity']}; PnL: CNY {summary['total_pnl']}; CAGR: {Decimal(summary['cagr']):.4%}.",
        'All 19 saved core CSV checksums matched; independent two-leg and daily accounting reconciliation passed.',
        '2026 is year-to-date through October 8. This is the legacy in-sample daily study, not a unified minute-backtest validation.','',
        '|Year|PnL (CNY)|Account return|','|---|---:|---:|']
    report += [f"|{r['year']}|{Decimal(r['total_pnl']):.2f}|{Decimal(r['actual_return']):.2%}|" for r in yearly]
    report+=['','![NAV and PnL](portfolio.png)']
    (output/'cloud_summary.md').write_text('\n'.join(report),encoding='utf-8')
    (output/'cloud_reproduction_check.json').write_text(json.dumps(dict(core_csv_matches=checked,
        fresh_search=args.fresh_search,all_match=True),indent=2),encoding='utf-8')
    print(f'Reproduced checkpoint: final equity {money("final_equity")} wan CNY; PnL {money("total_pnl")} wan CNY.',flush=True)


if __name__=='__main__':main()
