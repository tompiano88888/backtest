from copy import deepcopy
import json

import pytest

from backtest.ifind_futures_daily import publish_daily, validate_records
from backtest.storage import Store


def rows(code="IF2609.CFE"):
    return [dict(thscode=code, time=day, open="100.2", high="102.4", low="99.8", close="101.6",
                 volume=2, amount="6.06", openInterest=3, preSettlement=previous, settlement=settlement)
            for day, previous, settlement in [("2026-09-01", "99.6", "100.4"), ("2026-09-02", "100.4", "101.2")]]


def config():
    return dict(codes=["IF2609.CFE"], start="2010-01-01", end="2026-10-08")


def test_daily_exact_settlements_repeat_and_revision(tmp_path):
    with Store(tmp_path) as store:
        version, report = publish_daily(store, rows(), config())
        assert report["total_rows"] == 2
        assert report["previous_settlement_discontinuities"] == 0
        assert publish_daily(store, rows(), config())[0] == version
        altered = deepcopy(rows())
        altered[1]["settlement"] = "101.4"
        new_version, _ = publish_daily(store, altered, config())
        assert new_version != version
        assert store.db.execute("SELECT COUNT(*) FROM futures_daily_snapshots").fetchone()[0] == 2
        manifest = json.loads(store.db.execute("SELECT manifest FROM futures_daily_snapshots WHERE version=?", [version]).fetchone()[0])
        import pyarrow.parquet as pq
        old = pq.read_table(store.root / manifest[0]["path"]).to_pylist()
        assert str(old[1]["settlement_price"]) == "101.20000000"


@pytest.mark.parametrize("field", ["preSettlement", "settlement", "open", "volume"])
def test_missing_required_field_does_not_publish(tmp_path, field):
    broken = rows()
    broken[0][field] = None
    with Store(tmp_path) as store:
        with pytest.raises((ValueError, TypeError)):
            publish_daily(store, broken, config())
        assert not list(tmp_path.rglob("*.parquet"))


def test_gaps_and_settlement_discontinuity_rejected():
    first = rows()
    first[1]["preSettlement"] = "100.6"
    with pytest.raises(ValueError, match="discontinuity"):
        validate_records(first, config()["codes"], config()["start"], config()["end"])
    other = rows("IF2612.CFE")
    other.append(dict(other[-1], time="2026-09-03", preSettlement="101.2"))
    first = [rows()[0], dict(rows()[1], time="2026-09-03")]
    with pytest.raises(ValueError, match="gaps"):
        validate_records(first + other, ["IF2609.CFE", "IF2612.CFE"], config()["start"], config()["end"])


def test_conflicting_duplicate_and_continuous_code_rejected():
    with pytest.raises(ValueError, match="duplicate"):
        validate_records(rows() + [dict(rows()[0], close="101.8")], config()["codes"], config()["start"], config()["end"])
    with pytest.raises(ValueError, match="month"):
        validate_records([], ["IF8888.CFE"], config()["start"], config()["end"])


def test_corrupt_snapshot_not_silently_reused(tmp_path):
    with Store(tmp_path) as store:
        publish_daily(store, rows(), config())
        next(tmp_path.rglob("*.parquet")).write_bytes(b"broken")
        with pytest.raises(ValueError, match="Corrupt"):
            publish_daily(store, rows(), config())
