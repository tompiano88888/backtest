from datetime import datetime, timezone
import json

import pytest

from backtest.ifind_contracts import normalize_contract, publish_contracts, contract_from_snapshot
from backtest.storage import Store


NOW = datetime(2026, 10, 9, tzinfo=timezone.utc)


def record(code="IF2609.CFE"):
    return dict(thscode=code, ths_thscode_future=code, ths_future_short_name_future="test contract",
                ths_contract_listed_date_future="2026-03-23", ths_start_trade_date_future="20260323",
                ths_last_td_date_future="20260918", ths_last_delivery_date_future="20260918",
                ths_contract_multiplier="300.0", ths_mini_chg_price_future="0.2",
                ths_currency_type_future="CNY", ths_underlying_thscode_sif_future="000300.SH",
                ths_exchange_name_eng_future="CFFEX", ths_min_order_qty_cfe_future="1.0",
                ths_open_time_day_future="09:30:00", ths_close_time_am_future="11:30:00",
                ths_open_time_pm_future="13:00:00", ths_close_time_day_future="15:00:00")


def seed(store, end="2026-09-18"):
    store.db.execute("CREATE TABLE futures_daily_snapshots(version VARCHAR, report JSON)")
    store.db.execute("INSERT INTO futures_daily_snapshots VALUES (?,?)",
                     ["daily", json.dumps(dict(contracts={"IF2609.CFE":dict(first="2026-03-23",last=end)}))])


def batch(records=None):
    return [dict(observed_at=NOW.isoformat(), records=records if records is not None else [record()])]


def test_contract_snapshot_does_not_backdate_knowledge(tmp_path):
    with Store(tmp_path) as store:
        seed(store)
        version, report = publish_contracts(store, batch(), ["IF2609.CFE"], "daily")
        assert report["historical_sessions_ready"] is False
        effective = datetime(2026, 9, 18, tzinfo=timezone.utc)
        with pytest.raises(KeyError, match="known"):
            contract_from_snapshot(store, version, "IF2609.CFE", effective, effective)
        payload = contract_from_snapshot(store, version, "IF2609.CFE", effective, NOW)
        assert payload["expiry"] == payload["delivery"] == "2026-09-18"
        assert payload["multiplier"] == "300.0"
        assert "not historical" in payload["session_scope"]
        assert publish_contracts(store, batch(), ["IF2609.CFE"], "daily")[0] == version
        assert store.db.execute("SELECT COUNT(*) FROM metadata").fetchone()[0] == 2


@pytest.mark.parametrize("field,value", [("ths_contract_multiplier",None),
    ("ths_last_delivery_date_future","20260917"), ("ths_min_order_qty_cfe_future","0.5"),
    ("ths_thscode_future","IF2610.CFE"), ("ths_open_time_day_future","")])
def test_bad_metadata_rejected_before_write(tmp_path, field, value):
    broken = record()
    broken[field] = value
    with Store(tmp_path) as store:
        seed(store)
        with pytest.raises(ValueError):
            publish_contracts(store, batch([broken]), ["IF2609.CFE"], "daily")
        assert store.db.execute("SELECT COUNT(*) FROM metadata").fetchone()[0] == 0


def test_daily_terminal_gap_is_not_accepted(tmp_path):
    with Store(tmp_path) as store:
        seed(store, end="2026-09-17")
        with pytest.raises(ValueError, match="final trading day"):
            publish_contracts(store, batch(), ["IF2609.CFE"], "daily")
        assert store.db.execute("SELECT COUNT(*) FROM metadata").fetchone()[0] == 0


def test_current_sessions_not_claimed_as_historical():
    payload = normalize_contract(record())
    assert payload["observed_sessions"] == [["09:30:00", "11:30:00"], ["13:00:00", "15:00:00"]]
    assert payload["account"] == "futures"
