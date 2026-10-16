from datetime import datetime, timedelta, timezone
from dataclasses import replace

import pytest

from backtest.models import Bar, D
from backtest.pipeline import update
from backtest.storage import Store

T = datetime(2026, 9, 21, 1, tzinfo=timezone.utc)


def bar(close=100):
    return Bar("F", T, T + timedelta(minutes=1), T.date(), D(100), D(110), D(90), D(close), 10)


def test_versioned_upsert_and_old_snapshot_immutable(tmp_path):
    with Store(tmp_path) as s:
        old = s.publish([bar()], "test")
        new = s.publish([bar(105)], "test", parent=old)
        assert s.bars(old, ["F"], T, T + timedelta(days=1))[0].close == 100
        assert s.bars(new, ["F"], T, T + timedelta(days=1))[0].close == 105
        assert len(s.bars(new, ["F"], T, T + timedelta(days=1))) == 1


def test_pit_and_revision_cutoff(tmp_path):
    with Store(tmp_path) as s:
        s.put_metadata("main", "P", T, T, {"instrument": "F"})
        old = s.publish([bar()], "test")
        s.put_metadata("main", "P", T, T + timedelta(hours=1), {"instrument": "G"})
        assert s.metadata("main", "P", T, T)["instrument"] == "F"
        assert s.metadata("main", "P", T, T + timedelta(days=1))["instrument"] == "G"
        assert s.metadata("main", "P", T, T + timedelta(days=1), old)["instrument"] == "F"


def test_duplicate_batch_rejected_and_tampering_detected(tmp_path):
    with Store(tmp_path) as s:
        with pytest.raises(ValueError, match="Duplicate"):
            s.publish([bar(), bar()], "test")
        version = s.publish([bar()], "test")
        entry = next(iter(s.manifest(version).values()))
        (tmp_path / entry["path"]).write_bytes(b"broken")
        with pytest.raises(ValueError, match="Corrupt"):
            s.bars(version, ["F"], T, T + timedelta(days=1))


def test_failed_fetch_does_not_advance_dataset(tmp_path):
    with Store(tmp_path) as s:
        version = s.publish([bar()], "test")
        def fail():
            raise RuntimeError("offline")
        with pytest.raises(RuntimeError):
            update(s, fail, lambda x: x, "1m", sleep=lambda _: None)
        assert s.latest() == version
        assert s.db.execute("SELECT status FROM update_runs").fetchone()[0] == "failed"


def test_previous_settlement_survives_storage_without_replacing_daily_settlement(tmp_path):
    original = replace(bar(), settlement_price=D("101.20"), previous_settlement_price=D("98.60"))
    with Store(tmp_path) as store:
        version = store.publish([original], "test", frequency="1d")
        loaded = store.bars(version, ["F"], T, T + timedelta(days=1), frequency="1d")
        assert loaded == [original]
        assert loaded[0].settlement_price - loaded[0].previous_settlement_price == D("2.60")
