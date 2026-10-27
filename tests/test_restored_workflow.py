import json

import pytest

from backtest.pooled_rolling_arbitrage import load_source_snapshots


def test_frozen_replay_does_not_need_deleted_historical_output(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / 'frozen'
    source.mkdir()
    expected = dict(daily_version='daily-pinned', metadata_version='contracts-pinned',
                    spot_version='spot-pinned', provenance='preserved')
    (source / 'config.json').write_text(json.dumps({'source_snapshots': expected}))
    assert load_source_snapshots(source) == expected


def test_incomplete_frozen_snapshot_does_not_fall_back(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / 'frozen'
    source.mkdir()
    (source / 'config.json').write_text(json.dumps({'source_snapshots': {'daily_version': 'x'}}))
    with pytest.raises(ValueError, match='metadata_version'):
        load_source_snapshots(source)
