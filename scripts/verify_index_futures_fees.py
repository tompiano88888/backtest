"""Independently reconcile cached iFinD fee rows, DuckDB rules and pinned daily dates."""
from datetime import date,datetime
from decimal import Decimal
import argparse
import hashlib
import json
from pathlib import Path
import duckdb
from backtest.fees import HistoricalFees


def verify(report_path, daily_root, root, cache):
    report=json.loads(Path(report_path).read_text(encoding='utf-8'))
    provider=HistoricalFees(root,report['version'],report['as_of'])
    with duckdb.connect(str(Path(root)/'metadata.duckdb'),read_only=True) as db:
        row=db.execute('SELECT daily_version,revisions FROM futures_fee_snapshots WHERE version=?',[report['version']]).fetchone()
        assert row and row[0]==report['daily_version']
        revisions=json.loads(row[1])
        raw={}
        for code,ids in revisions.items():
            found=db.execute('SELECT key,payload FROM metadata WHERE revision=?',[ids['raw']]).fetchone()
            assert found[0]==code
            raw[code]=json.loads(found[1])
    with duckdb.connect(str(Path(daily_root)/'metadata.duckdb'),read_only=True) as db:
        manifest=json.loads(db.execute('SELECT manifest FROM futures_daily_snapshots WHERE version=?',[report['daily_version']]).fetchone()[0])
    assert set(raw)=={r['instrument'] for r in manifest}==set(report['cache_sha256'])
    daily_entries={r['instrument']:r for r in manifest}
    cached={hashlib.sha256(p.read_bytes()).hexdigest():p for p in Path(cache).glob('*.json')}
    rows=0; by_product_day={}; sample={}
    with duckdb.connect() as db:
        for code,observation in raw.items():
            digest=report['cache_sha256'][code]
            assert digest==observation['sha256'] and digest in cached
            batch=json.loads(cached[digest].read_text(encoding='utf-8'))
            assert observation['batch']==batch
            entry=daily_entries[code]; path=Path(daily_root)/entry['path']
            assert hashlib.sha256(path.read_bytes()).hexdigest()==entry['sha256']
            expected={r[0] for r in db.execute('SELECT trading_date FROM read_parquet(?)',[str(path)]).fetchall()}
            observed=set()
            for r in batch['records']:
                day=date.fromisoformat(r['time']);assert day not in observed and r['thscode']==code
                observed.add(day)
                native=(r['ths_transaction_procedure_rate_future'],r['ths_transaction_procedure_fee_future'],
                        r['ths_to_transaction_procedure_rate_future'],r['ths_to_transaction_procedure_fee_future'])
                assert any(v is not None for v in native[:2]) and any(v is not None for v in native[2:])
                nr,nu,tr,tu=(Decimal(0) if v is None else Decimal(str(v)) for v in native)
                expected_values=nr/1000,nu,tr/1000,tu
                rule=provider.resolve(code,day)
                assert (rule.open_rate,rule.open_per_unit,rule.close_today_rate,rule.close_today_per_unit)==expected_values
                assert (rule.close_rate,rule.close_per_unit)==expected_values[:2]
                assert rule.matching=='today_first'
                rows+=1
                by_product_day.setdefault((code[:2],str(day)),set()).add(tuple(str(v) for v in expected_values))
                if (code,str(day)) in [('IF1005.CFE','2010-04-16'),('IF2306.CFE','2023-03-17'),('IF2306.CFE','2023-03-20')]:
                    sample[code+'/'+str(day)]=dict(normal=str(nr/1000),close_today=str(tr/1000))
            assert observed==expected
    assert rows==report['rows']
    disagreements=[dict(product=k[0],day=k[1],rates=sorted(v)) for k,v in sorted(by_product_day.items()) if len(v)>1]
    bd_checked=0
    recheck_path=Path(report_path).with_name('recheck.json')
    if recheck_path.exists():
        for batch in json.loads(recheck_path.read_text(encoding='utf-8')):
            day=batch['request']['parameters'].split(';')[0]
            for row in batch['records']:
                records=raw[row['thscode']]['batch']['records']
                matches=[r for r in records if r['time']==day]
                assert len(matches)==1
                for field in ('ths_transaction_procedure_rate_future','ths_transaction_procedure_fee_future',
                              'ths_to_transaction_procedure_rate_future','ths_to_transaction_procedure_fee_future'):
                    assert row[field]==matches[0][field]
                bd_checked+=1

    return dict(version=report['version'],contracts=len(raw),rows=rows,coverage_equal=True,
                raw_cache_sha256_equal=True,all_fee_rules_reconciled=True,independent_bd_rows_checked=bd_checked,
                cross_contract_differing_product_days=len(disagreements),
                cross_contract_differences=disagreements,unit_reference_samples=sample)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--report',default='outputs/ifind_futures_fees_import/validation.json')
    p.add_argument('--daily-root',default='data/index_futures')
    p.add_argument('--root',default='data/execution_fees')
    p.add_argument('--cache',default='data/execution_fees/staging/20261020')
    p.add_argument('--output',default='outputs/ifind_futures_fees_import/independent_verification.json')
    a=p.parse_args();result=verify(a.report,a.daily_root,a.root,a.cache)
    Path(a.output).write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k!='cross_contract_differences'},indent=2))


if __name__=='__main__':main()
