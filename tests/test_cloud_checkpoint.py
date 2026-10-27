import hashlib
from pathlib import Path

import pytest

from scripts.cloud_reproduce_checkpoint import prepare_market_files


def test_cloud_data_preflight_uses_exact_snapshot_without_partial_attach(tmp_path):
    uploaded=tmp_path/'uploaded';bundle=tmp_path/'bundle'
    source=uploaded/'index_futures/daily/f.parquet';source.parent.mkdir(parents=True);source.write_bytes(b'pinned')
    entry=dict(path='data/index_futures/daily/f.parquet',sha256=hashlib.sha256(b'pinned').hexdigest())
    missing=dict(path='data/index_etf/daily/e.parquet',sha256='missing')
    with pytest.raises(FileNotFoundError):prepare_market_files(bundle,dict(market_files=[entry,missing]),uploaded)
    assert not (bundle/entry['path']).exists()
    prepare_market_files(bundle,dict(market_files=[entry]),uploaded)
    assert (bundle/entry['path']).read_bytes()==b'pinned'
    assert source.read_bytes()==b'pinned'


def test_cloud_data_preflight_rejects_revised_prices_and_preserves_source(tmp_path):
    uploaded=tmp_path/'uploaded';source=uploaded/'index_etf/e.parquet'
    source.parent.mkdir(parents=True);source.write_bytes(b'revised')
    manifest=dict(market_files=[dict(path='data/index_etf/e.parquet',sha256=hashlib.sha256(b'old').hexdigest())])
    with pytest.raises(ValueError,match='checksum'):prepare_market_files(tmp_path/'bundle',manifest,uploaded)
    assert source.read_bytes()==b'revised'
