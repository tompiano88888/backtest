from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import duckdb
import pytest
from backtest.ifind_fees import (FIELDS, normalize_record, normalize_batch, publish_cached,
                                request_for, cache_path)
from backtest.fees import HistoricalFees
from backtest.models import D


def raw(day='2023-03-17',code='IF2306.CFE',normal='0.023',today='0.345'):
    return dict(thscode=code,time=day,**dict(zip(FIELDS,(normal,None,today,None))))


def batch():
    return dict(observed_at='2026-10-20T01:00:00+00:00',records=[raw(),raw('2023-03-20',today='0.23')])


def test_normalization_preserves_exact_fee_units():
    _,_,p=normalize_record(raw())
    assert p['open_rate']==p['close_rate']==D('.000023')
    assert p['close_today_rate']==D('.000345') and p['open_per_unit']==0
    assert p['matching']=='today_first'
    r=raw();r[FIELDS[0]]=None;r[FIELDS[1]]='2.5'
    assert normalize_record(r)[2]['open_per_unit']==D('2.5')


@pytest.mark.parametrize('value',['-1','NaN','Infinity'])
def test_bad_fee_numbers_rejected(value):
    r=raw(normal=value)
    with pytest.raises(ValueError):normalize_record(r)


def test_missing_pair_and_non_actual_contract_rejected():
    r=raw(normal=None)
    with pytest.raises(ValueError,match='Missing'):normalize_record(r)
    with pytest.raises(ValueError,match='actual'):normalize_record(raw(code='IF.CFE'))


def test_calendar_exactness_duplicates_and_segments():
    b=batch();days=[date(2023,3,17),date(2023,3,20)]
    _,_,segments=normalize_batch(b,'IF2306.CFE',days)
    assert len(segments)==2 and segments[0][1]==days[1]
    b['records'][1]=raw('2023-03-20')
    assert len(normalize_batch(b,'IF2306.CFE',days)[2])==1
    with pytest.raises(ValueError,match='coverage'):normalize_batch(b,'IF2306.CFE',days[:1])
    b['records'].append(b['records'][0])
    with pytest.raises(ValueError,match='duplicate'):normalize_batch(b,'IF2306.CFE',days)


def fixture(tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq
    daily=tmp_path/'daily';daily.mkdir()
    path=daily/'IF2306.parquet'
    pq.write_table(pa.table(dict(trading_date=[date(2023,3,17),date(2023,3,20)])),path)
    manifest=[dict(instrument='IF2306.CFE',path=path.name,sha256=hashlib.sha256(path.read_bytes()).hexdigest())]
    dates=dict(first='2023-03-17',last='2023-03-20',rows=2)
    with duckdb.connect(str(daily/'metadata.duckdb')) as db:
        db.execute('CREATE TABLE futures_daily_snapshots(version VARCHAR, report JSON,manifest JSON)')
        db.execute('INSERT INTO futures_daily_snapshots VALUES (?,?,?)',
                   ['daily-v',json.dumps(dict(contracts={'IF2306.CFE':dates})),json.dumps(manifest)])
    cache=tmp_path/'cache';cache.mkdir()
    b=batch();b['request']=request_for('IF2306.CFE',dates)
    target=cache_path(cache,b['request']);target.write_text(json.dumps(b),encoding='utf-8')
    return daily,cache,target


def test_transactional_publish_and_idempotence(tmp_path):
    daily,cache,_=fixture(tmp_path);root=tmp_path/'fees'
    report=publish_cached(daily,'daily-v',root,cache)
    assert report['contracts']==1 and report['rows']==2 and report['fee_rules']==2
    assert publish_cached(daily,'daily-v',root,cache)==report
    from scripts.verify_index_futures_fees import verify
    report_path=tmp_path/'report.json';report_path.write_text(json.dumps(report),encoding='utf-8')
    verified=verify(report_path,daily,root,cache)
    assert verified['rows']==2 and verified['all_fee_rules_reconciled']

    provider=HistoricalFees(root,report['version'],report['as_of'])
    assert provider.resolve('IF2306.CFE',date(2023,3,17)).close_today_rate==D('.000345')
    assert provider.resolve('IF2306.CFE',date(2023,3,20)).close_today_rate==D('.00023')
    with duckdb.connect(str(root/'metadata.duckdb'),read_only=True) as db:
        assert db.execute("select count(*) from metadata where kind='ifind_fee_observation'").fetchone()[0]==1


def test_incomplete_cache_never_publishes(tmp_path):
    daily,cache,target=fixture(tmp_path);root=tmp_path/'fees'
    b=json.loads(target.read_text(encoding='utf-8'));b['records'].pop()
    target.write_text(json.dumps(b),encoding='utf-8')
    with pytest.raises(ValueError,match='coverage'):publish_cached(daily,'daily-v',root,cache)
    assert not (root/'metadata.duckdb').exists()


def test_corrupt_daily_snapshot_never_publishes(tmp_path):
    daily,cache,_=fixture(tmp_path);root=tmp_path/'fees'
    (daily/'IF2306.parquet').write_bytes(b'corrupt')
    with pytest.raises(ValueError,match='SHA256'):publish_cached(daily,'daily-v',root,cache)
    assert not (root/'metadata.duckdb').exists()
