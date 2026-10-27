"""Read-only 09:30 auction-vs-daily-open audit on pinned landed snapshots."""
from collections import defaultdict
from datetime import time
import hashlib
import json
from pathlib import Path

import duckdb
import pyarrow.compute as pc
import pyarrow.parquet as pq

from backtest.futures_spread import load_snapshot
from backtest.ifind_auction import index_futures_auction
DAILY='0fff65706ce54991a3725f1bcbb6c6c9'
MINUTE='df7196984c186d24c4c24fdb1aa87ecbbfcde14d35b20ea12be05c8600ce41c1'


def main():
    root=Path('data/index_futures_minute')
    _,daily=load_snapshot('data/index_futures',DAILY)
    reference={(r['instrument'],r['trading_date']):r['open'] for r in daily}
    with duckdb.connect(str(root/'metadata.duckdb'),read_only=True) as db:
        report=json.loads(db.execute('SELECT report FROM futures_minute_snapshots WHERE version=?',[MINUTE]).fetchone()[0])
    seen=set()
    counts=defaultdict(int)
    mismatches=[]
    expected=set()
    for item in report['manifest']:
        path=(root/item['path']).resolve()
        if not path.is_relative_to(root.resolve()) or hashlib.sha256(path.read_bytes()).hexdigest()!=item['sha256']:
            raise ValueError('Pinned minute checksum/path mismatch')
        table=pq.read_table(path,columns=['instrument','source_timestamp','open','high','low','close','volume'])
        dates=pc.cast(table['source_timestamp'],'date32').unique().to_pylist()
        expected.update((item['instrument'],d) for d in dates)
        opening=table.filter(pc.and_(pc.equal(pc.hour(table['source_timestamp']),9),pc.equal(pc.minute(table['source_timestamp']),30)))
        for row in opening.to_pylist():
            key=row['instrument'],row['source_timestamp'].date()
            if key in seen:
                raise ValueError(f'Duplicate auction {key}')
            seen.add(key)
            if key not in reference:
                raise ValueError(f'Missing daily reference {key}')
            try:
                index_futures_auction(row,key[1],reference[key])
            except ValueError as exc:
                mismatches.append(dict(instrument=key[0],day=str(key[1]),daily_open=str(reference[key]),
                                       observation={k:str(v) for k,v in row.items()},error=str(exc)))
            counts[key[0][:2]]+=1
    result=dict(minute_version=MINUTE,daily_version=DAILY,contract_days=len(expected),auction_rows=len(seen),
                by_product=dict(counts),missing_auction=len(expected-seen),mismatches=mismatches,
                status='passed' if expected==seen and not mismatches else 'differences',
                semantics='User-confirmed 09:30 auction; discrete result equals daily open, zero auction slippage')
    output=Path('outputs/index_futures_auction_audit.json')
    output.write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k!='mismatches'},indent=2))
    print(f'Differences: {len(mismatches)}; full report: {output.resolve()}')


if __name__=='__main__':
    main()
