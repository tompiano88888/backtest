from copy import deepcopy
import json

import pytest

from backtest.ifind_index_etf import normalize_batches, publish_daily
from backtest.storage import Store
from backtest.ifind_index_etf import nav_from_snapshot
from datetime import datetime, timezone
from decimal import Decimal


def inputs():
    config = dict(start="2000-01-01", end="2026-10-08", instruments={
        "000300.SH":dict(asset="index",product="IF",name="index"),
        "510300.SH":dict(asset="fund",product="IF",name="ETF")})
    raw = dict(open="1.01",high="1.05",low="1.00",close="1.03",volume="10.25",amount="10.5",time="2026-09-01")
    records = [dict(raw,thscode="000300.SH"),dict(raw,thscode="510300.SH",volume=None,amount=None)]
    return [dict(observed_at="2026-10-09T00:00:00+00:00", records=records)],config


def test_source_fractional_index_volume_and_null_etf_are_preserved(tmp_path):
    batches, config = inputs()
    with Store(tmp_path) as store:
        version, report = publish_daily(store, batches, config)
        assert report["total_rows"] == 2
        assert report["symbols"]["510300.SH"]["missing_volume"] == 1
        manifest = json.loads(store.db.execute("SELECT manifest FROM index_etf_daily_snapshots").fetchone()[0])
        import pyarrow.parquet as pq
        values = {e["instrument"]:pq.read_table(store.root/e["path"]).to_pylist()[0] for e in manifest}
        assert str(values["000300.SH"]["source_volume"]) == "10.25000000"
        assert values["510300.SH"]["source_volume"] is None
        assert values["510300.SH"]["ohlcv_complete"] is False
        assert publish_daily(store,batches,config)[0] == version


@pytest.mark.parametrize("field,value", [("open",None),("high","0.9"),("volume","-1"),("time","1990-01-01")])
def test_invalid_input_does_not_publish(tmp_path,field,value):
    batches, config = inputs()
    batches[0]["records"][0][field] = value
    with Store(tmp_path) as store:
        with pytest.raises(ValueError):
            publish_daily(store,batches,config)
        assert not list(tmp_path.rglob("*.parquet"))


def test_duplicate_conflict_and_missing_security_fail():
    batches,config = inputs()
    batches[0]["records"].append(dict(batches[0]["records"][0],close="1.04"))
    with pytest.raises(ValueError,match="duplicate"):
        normalize_batches(batches,config)
    batches,config = inputs()
    batches[0]["records"].pop()
    with pytest.raises(ValueError,match="Missing security"):
        normalize_batches(batches,config)


def nav_inputs():
    batches, config = inputs()
    config['include_etf_nav'] = True
    batches[0]['records'][1]['netAssetValue'] = '1.03123456'
    fund = batches[0]['records'][1]
    batches[0]['records'].append(dict(fund, time='2026-09-02', netAssetValue=None))
    batches[0]['records'].append(dict(fund, time='2026-08-28', open=None, high=None, low=None, close=None, netAssetValue='1.0012'))
    return batches, config


def test_nav_precision_null_prelisting_pit_and_old_snapshot(tmp_path):
    import pyarrow.parquet as pq
    batches, config = inputs()
    with Store(tmp_path) as store:
        old, _ = publish_daily(store, batches, config)
        old_manifest = json.loads(store.db.execute('SELECT manifest FROM index_etf_daily_snapshots WHERE version=?', [old]).fetchone()[0])
        old_bytes = [(store.root/e['path']).read_bytes() for e in old_manifest]
        batches, config = nav_inputs()
        version, report = publish_daily(store, batches, config)
        assert version != old
        assert report['total_rows'] == 3  # NAV-only date is not a market row.
        assert report['symbols']['510300.SH']['missing_nav'] == 1
        rows = nav_from_snapshot(store, version, '510300.SH')
        assert len(rows) == 3
        assert rows[0]['unit_nav'] == Decimal('1.0012')
        assert rows[1]['unit_nav'] == Decimal('1.03123456')
        assert rows[2]['unit_nav'] is None
        assert rows[2]['unit_nav_known_at'] is None
        assert nav_from_snapshot(store, version, '510300.SH', as_of=datetime(2026,9,2,tzinfo=timezone.utc)) == []
        assert [(store.root/e['path']).read_bytes() for e in old_manifest] == old_bytes
        assert publish_daily(store, batches, config)[0] == version
        with pytest.raises(KeyError):
            nav_from_snapshot(store, old, '510300.SH')
        newer = deepcopy(batches)
        newer[0]['records'][-1]['netAssetValue'] = '1.0022'
        new_version, _ = publish_daily(store, newer, config)
        assert new_version != version  # NAV-only revision must change fingerprint.
        assert nav_from_snapshot(store, version, '510300.SH')[0]['unit_nav'] == Decimal('1.0012')
        assert nav_from_snapshot(store, new_version, '510300.SH')[0]['unit_nav'] == Decimal('1.0022')
        file = store.root / report['nav_manifest'][0]['path']
        file.write_bytes(b'corrupt')
        with pytest.raises(ValueError, match='Corrupt'):
            nav_from_snapshot(store, version, '510300.SH')
        with pytest.raises(ValueError, match='Corrupt'):
            publish_daily(store, batches, config)


@pytest.mark.parametrize('value', ['0', '-1', 'NaN', 'Infinity', 'bad'])
def test_invalid_nav_fails_without_publication(tmp_path, value):
    batches, config = nav_inputs()
    batches[0]['records'][1]['netAssetValue'] = value
    with Store(tmp_path) as store:
        with pytest.raises(ValueError, match='NAV'):
            publish_daily(store, batches, config)
        assert not list(tmp_path.rglob('*.parquet'))


def test_missing_or_all_null_nav_cannot_publish(tmp_path):
    batches, config = nav_inputs()
    batches[0]['records'][1].pop('netAssetValue')
    with Store(tmp_path) as store:
        with pytest.raises(ValueError, match='field missing'):
            publish_daily(store, batches, config)
        batches[0]['records'] = batches[0]['records'][:3]
        for r in batches[0]['records'][1:]:
            r['netAssetValue'] = None
        with pytest.raises(ValueError, match='No ETF unit NAV'):
            publish_daily(store, batches, config)
        assert not list(tmp_path.rglob('*.parquet'))


def test_download_requests_nav_only_for_funds_and_reuses_cache(tmp_path, monkeypatch):
    from scripts.download_index_etf_daily import acquire
    calls = []
    class FakeSource:
        def login(self): pass
        def close(self): pass
        def fetch(self, request):
            calls.append(request)
            return [dict(thscode=request['codes'])]
    monkeypatch.setattr('scripts.download_index_etf_daily.IFinDSource', FakeSource)
    _, config = nav_inputs()
    config['cache'] = str(tmp_path)
    acquire(config)
    assert calls[0]['indicators'] == 'open;high;low;close;volume;amount'
    assert calls[1]['indicators'].endswith(';netAssetValue')
    acquire(config, publish_only=True)
    assert len(calls) == 2
