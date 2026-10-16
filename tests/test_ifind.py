import json
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from backtest.ifind import (IFinDSource, IFinDError, IFinDConfigurationError,
                           IFinDQueryError, decode_response, records_from_payload, market_requests)
from backtest.ifind_normalize import IFinDBarNormalizer, SessionCalendar, SHANGHAI
from backtest.pipeline import prepare_ifind_update, update_ifind
from backtest.storage import Store


def payload(code="F", times=None, **columns):
    return {"errorcode": 0, "tables": [{"thscode": code, "time": times or ["2026-09-18 21:01:00"],
                                       "table": columns or {"close": [100]}}]}


class SDK:
    def __init__(self, login=0, responses=None):
        self.login_code, self.responses = login, list(responses or [payload()])
        self.calls, self.logouts = [], 0

    def THS_iFinDLogin(self, username, password):
        return self.login_code

    def THS_iFinDLogout(self):
        self.logouts += 1
        return 0

    def __getattr__(self, name):
        def call(*args):
            self.calls.append((name, args))
            answer = self.responses.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return SimpleNamespace(errorcode=0, data=json.dumps(answer).encode())
        return call


@pytest.fixture(autouse=True)
def credentials(monkeypatch):
    monkeypatch.setenv("IFIND_USERNAME", "unit-test-user")
    monkeypatch.setenv("IFIND_PASSWORD", "unit-test-secret")


def request(frequency="1m", **overrides):
    result = dict(frequency=frequency, codes="F", indicators="open,high,low,close,volume",
                  parameters="Interval:1,CPS:no,Fill:Original", start="2026-09-18 21:01:00", end="2026-09-18 21:03:00")
    if frequency == "1d":
        result.update(parameters="Interval:D,CPS:1,Fill:Omit", start="2026-09-21", end="2026-09-22")
    return dict(result, **overrides)


def calendar():
    return SessionCalendar([
        dict(instrument="F", trading_date="2026-09-21", start="2026-09-18T21:00:00+08:00", end="2026-09-18T23:00:00+08:00"),
        dict(instrument="F", trading_date="2026-09-21", start="2026-09-21T09:00:00+08:00", end="2026-09-21T15:00:00+08:00"),
    ])


def normalize_config(frequency="1m"):
    columns = {f: f for f in ("open", "high", "low", "close", "volume")}
    if frequency == "1d":
        columns["settlement_price"] = "vendor_settle"
    return {"timestamp_at": "end", "instruments": {"F": {"id": "F", "asset": "future", "volume_multiplier": "1"}}, "columns": columns}


def record(stamp="2026-09-18 21:01:00", **changes):
    return dict(dict(thscode="F", time=stamp, open=100, high=102, low=99, close=101, volume=10), **changes)


def test_sdk_json_calls_and_owned_session_logout():
    sdk = SDK()
    with IFinDSource(sdk) as source:
        records = source.fetch(request())
        assert records == [{"thscode": "F", "time": "2026-09-18 21:01:00", "close": 100}]
    assert sdk.calls[0][0] == "THS_HF"
    assert sdk.calls[0][1][-1] == "format:json"
    assert sdk.logouts == 1


def test_reused_session_is_not_logged_out():
    sdk = SDK(login=-201)
    with IFinDSource(sdk):
        pass
    assert sdk.logouts == 0


def test_missing_credentials_fail_without_sdk_call(monkeypatch):
    monkeypatch.delenv("IFIND_PASSWORD")
    with pytest.raises(IFinDConfigurationError, match="Set IFIND_USERNAME"):
        IFinDSource(SDK()).login()


def test_login_failure_redacts_vendor_details():
    with pytest.raises(IFinDQueryError) as error:
        IFinDSource(SDK(login=-2)).login()
    assert "unit-test-secret" not in str(error.value)
    assert "-2" in str(error.value)


@pytest.mark.parametrize("function, fields", [
    ("THS_HQ", request("1d")),
    ("THS_BD", {"codes": "F", "indicators": "verified_indicator", "parameters": ""}),
    ("THS_DS", {"codes": "F", "indicators": "verified_indicator", "parameters": "", "global_parameters": "", "start": "2026-09-21", "end": "2026-09-22"}),
    ("THS_DP", {"name": "verified_pool", "parameters": "", "options": ""}),
    ("THS_DateQuery", {"exchange": "SSE", "parameters": "dateType:0,period:D,dateFormat:0", "start": "2026-09-21", "end": "2026-09-22"}),
])
def test_supported_functions_route_with_correct_signature(function, fields):
    sdk = SDK(responses=[{"errorcode": 0, "tables": {"time": ["2026-09-21"]}}] if function == "THS_DateQuery" else None)
    with IFinDSource(sdk) as source:
        rows = source.query(dict(fields, function=function))
    assert sdk.calls[0][0] == function
    if function == "THS_DateQuery":
        assert len(sdk.calls[0][1]) == 4
        assert rows == [{"time": "2026-09-21"}]
    else:
        assert sdk.calls[0][1][-1] == "format:json"


def test_native_dictionary_calendar_and_decimal_precision():
    assert records_from_payload(decode_response({"errorcode": 0, "tables": {"time": []}}, "calendar")) == []
    data = b'{"errorcode":0,"tables":[{"thscode":"F","time":["2026-09-21"],"table":{"close":[0.123456789012345678]}}]}'
    assert records_from_payload(decode_response(data, "HQ"))[0]["close"] == Decimal("0.123456789012345678")


@pytest.mark.parametrize("bad", [
    {"tables": []}, {"errorcode": 0}, {"errorcode": "0", "tables": []},
    {"errorcode": 0, "tables": [{"thscode": "F", "time": ["a"], "table": {"close": [1, 2]}}]},
    {"errorcode": 0, "tables": [{"thscode": "F", "table": {"close": [1], "volume": []}}]},
])
def test_bad_response_is_never_silently_truncated(bad):
    with pytest.raises(IFinDError):
        records_from_payload(decode_response(bad, "HQ"))


def test_only_configured_vendor_errors_are_retried():
    sdk = SDK(responses=[{"errorcode": -123, "errmsg": "unit-test-secret"}, payload()])
    with IFinDSource(sdk, retry_codes=[-123], sleep=lambda _: None) as source:
        assert source.fetch(request())
    assert len(sdk.calls) == 2
    sdk = SDK(responses=[{"errorcode": -2, "errmsg": "unit-test-secret"}])
    with IFinDSource(sdk, sleep=lambda _: None) as source:
        with pytest.raises(IFinDError) as error:
            source.fetch(request())
    assert len(sdk.calls) == 1
    assert "unit-test-secret" not in str(error.value)


def test_transport_retry_and_exception_redaction():
    sdk = SDK(responses=[TimeoutError("unit-test-secret"), payload()])
    with IFinDSource(sdk, sleep=lambda _: None) as source:
        assert source.fetch(request())
    sdk = SDK(responses=[RuntimeError("unit-test-secret")])
    with IFinDSource(sdk) as source:
        with pytest.raises(IFinDError) as error:
            source.fetch(request())
    assert "unit-test-secret" not in str(error.value)


def test_chunked_requests_are_inclusive_and_nonoverlapping():
    jobs = list(market_requests(request(codes="A,B", start="2026-09-18 21:00:00", end="2026-09-20 21:00:00"), batch_size=1, chunk_days=1))
    assert len(jobs) == 6
    a = [r for r in jobs if r["codes"] == "A"]
    assert a[-1]["end"] == "2026-09-20 21:00:00"
    for prev, nxt in zip(a, a[1:]):
        assert datetime.fromisoformat(nxt["start"]) - datetime.fromisoformat(prev["end"]) == timedelta(seconds=1)
    assert len(list(market_requests(request("1d"), chunk_days=1))) == 2


@pytest.mark.parametrize("parameters", ["Interval:1,CPS:no,Fill:Previous", "Interval:5,CPS:no,Fill:Original", "Interval:1,CPS:forward,Fill:Original"])
def test_adjusted_filled_or_wrong_frequency_requests_rejected(parameters):
    with pytest.raises(IFinDConfigurationError):
        list(market_requests(request(parameters=parameters)))


def test_night_session_uses_calendar_not_natural_date():
    norm = IFinDBarNormalizer(normalize_config(), "1m", calendar())
    bar = norm(record())
    assert str(bar.trading_date) == "2026-09-21"
    assert bar.start.isoformat() == "2026-09-18T21:00:00+08:00"
    assert bar.end.isoformat() == "2026-09-18T21:01:00+08:00"
    with pytest.raises(IFinDError, match="No containing"):
        norm(record("2026-09-18 21:00:00"))


def test_units_and_daily_settlement_requirements():
    cfg = normalize_config()
    cfg["instruments"]["F"]["volume_multiplier"] = "100"
    norm = IFinDBarNormalizer(cfg, "1m", calendar())
    assert norm(record(volume="2" )).volume == 200
    with pytest.raises(IFinDConfigurationError, match="settlement"):
        IFinDBarNormalizer(cfg, "1d", calendar())
    daily = IFinDBarNormalizer(normalize_config("1d"), "1d", calendar())
    with pytest.raises(IFinDError, match="null"):
        daily(record("2026-09-21", vendor_settle=None))
    bar = daily(record("2026-09-21", vendor_settle="100.5"))
    assert bar.settlement_price == Decimal("100.5")
    assert bar.start.date().isoformat() == "2026-09-18"
    assert bar.end.date().isoformat() == "2026-09-21"


@pytest.mark.parametrize("changes", [{"volume": "0.1"}, {"close": None}, {"high": 98}, {"volume": "NaN"}])
def test_invalid_market_values_are_rejected(changes):
    norm = IFinDBarNormalizer(normalize_config(), "1m", calendar())
    with pytest.raises(IFinDError):
        norm(record(**changes))


class Source:
    def __init__(self, batches):
        self.batches = iter(batches)

    def fetch(self, _):
        value = next(self.batches)
        if isinstance(value, Exception):
            raise value
        return value


def test_update_end_to_end_old_version_survives_correction(tmp_path):
    norm = IFinDBarNormalizer(normalize_config(), "1m", calendar())
    with Store(tmp_path) as store:
        first = update_ifind(store, Source([[record()]]), [request()], norm)
        second = update_ifind(store, Source([[record(close=102), record(close=102)]]), [request()], norm)
        start = datetime(2026, 9, 18, 20, tzinfo=SHANGHAI)
        end = start + timedelta(days=1)
        assert store.bars(first, ["F"], start, end)[0].close == 101
        assert store.bars(second, ["F"], start, end)[0].close == 102
        assert len(store.bars(second, ["F"], start, end)) == 1


def test_failed_later_batch_does_not_publish_any_rows(tmp_path):
    norm = IFinDBarNormalizer(normalize_config(), "1m", calendar())
    with Store(tmp_path) as store:
        old = update_ifind(store, Source([[record()]]), [request()], norm)
        with pytest.raises(IFinDError):
            update_ifind(store, Source([[record(close=102)], IFinDError("offline")]), [request(), request()], norm)
        assert store.latest() == old
        assert store.db.execute("SELECT count(*) FROM datasets").fetchone()[0] == 1
        assert store.db.execute("SELECT count(*) FROM update_runs WHERE status='failed'").fetchone()[0] == 1


@pytest.mark.parametrize("rows", [[], [record("2026-09-18 22:00:00")], [record(), record(close=102)], [record(thscode="UNKNOWN")]])
def test_empty_out_of_range_conflicting_or_unknown_records_do_not_publish(tmp_path, rows):
    norm = IFinDBarNormalizer(normalize_config(), "1m", calendar())
    with Store(tmp_path) as store:
        with pytest.raises(IFinDError):
            update_ifind(store, Source([rows]), [request()], norm)
        assert store.latest() is None


def test_partial_security_response_is_rejected(tmp_path):
    norm = IFinDBarNormalizer(normalize_config(), "1m", calendar())
    with Store(tmp_path) as store:
        with pytest.raises(IFinDError, match="incomplete security"):
            update_ifind(store, Source([[record()]]), [request(codes="F,G")], norm)


def test_example_configs_validate_without_sdk_login():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "examples"
    for name in ("ifind.template.json", "ifind.daily.json"):
        config = json.loads((root / name).read_text())
        requests, normalizer = prepare_ifind_update(config, root)
        assert len(requests) == 1
        assert normalizer.instruments["300033.SZ"]["asset"] == "equity"
