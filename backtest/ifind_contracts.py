"""Confirmed iFinD contract fields and conservative observation-time metadata."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import InvalidOperation
import hashlib
import json
from pathlib import Path
import re
import uuid

from .models import D
from .storage import json_default

SHANGHAI = timezone(timedelta(hours=8))
FIELDS = {
    "name": "ths_future_short_name_future",
    "listed_date": "ths_contract_listed_date_future",
    "first_trade_date": "ths_start_trade_date_future",
    "expiry": "ths_last_td_date_future",
    "delivery": "ths_last_delivery_date_future",
    "multiplier": "ths_contract_multiplier",
    "tick": "ths_mini_chg_price_future",
    "currency": "ths_currency_type_future",
    "underlying": "ths_underlying_thscode_sif_future",
    "exchange": "ths_exchange_name_eng_future",
    "lot": "ths_min_order_qty_cfe_future",
}
SESSION_FIELDS = ["ths_open_time_day_future", "ths_close_time_am_future",
                  "ths_open_time_pm_future", "ths_close_time_day_future"]


def parse_date(value):
    if not isinstance(value, str):
        raise ValueError("Missing or invalid contract date")
    if re.fullmatch(r"\d{8}", value):
        value = f"{value[:4]}-{value[4:6]}-{value[6:]}"
    return date.fromisoformat(value)


def normalize_contract(record):
    code = record.get("thscode")
    if not isinstance(code, str) or not re.fullmatch(r"(?:IF|IC|IM|IH)\d{2}(?:0[1-9]|1[0-2])\.CFE", code):
        raise ValueError("Expected actual stock-index futures contract")
    if record.get("ths_thscode_future") != code:
        raise ValueError("Contract identifier mismatch")
    payload = {target: record.get(source) for target, source in FIELDS.items()}
    for key in ("listed_date", "first_trade_date", "expiry", "delivery"):
        payload[key] = parse_date(payload[key])
    if not payload["listed_date"] <= payload["first_trade_date"] <= payload["expiry"] <= payload["delivery"]:
        raise ValueError(f"Invalid lifecycle: {code}")
    for key in ("multiplier", "tick", "lot"):
        if payload[key] is None:
            raise ValueError(f"Missing contract units: {code}/{key}")
        try:
            payload[key] = D(payload[key])
        except (ValueError, InvalidOperation):
            raise ValueError(f"Invalid contract units: {code}/{key}") from None
        if payload[key] <= 0:
            raise ValueError(f"Nonpositive contract units: {code}/{key}")
    if payload["lot"] != int(payload["lot"]):
        raise ValueError("Lot must be integer")
    payload["lot"] = int(payload["lot"])
    if payload["exchange"] != "CFFEX" or payload["currency"] != "CNY" or not payload["underlying"] or not payload["name"]:
        raise ValueError(f"Missing/mismatched identity: {code}")
    session = [time.fromisoformat(record.get(field) or "invalid") for field in SESSION_FIELDS]
    if not session[0] < session[1] < session[2] < session[3]:
        raise ValueError(f"Invalid observed trading hours: {code}")
    night = [record.get(k) for k in ("ths_open_time_night_future", "ths_close_time_night_future")]
    if any(v not in (None, "") for v in night):
        raise ValueError("Unexpected night session; explicit mapping required")
    payload.update(id=code, product=code[:2], asset="future", account="futures", source="iFinD THS_BD")
    payload["observed_sessions"] = [[session[0].isoformat(), session[1].isoformat()],
                                    [session[2].isoformat(), session[3].isoformat()]]
    payload["session_scope"] = "vendor current template; not historical trading hours"
    payload["pit_status"] = "known_at is retrieval time; historical announcement verification pending"
    return payload


def publish_contracts(store, batches, codes, daily_version):
    """Atomic complete import; preserve raw fields separately from core facts."""
    wanted, normalized, raw, observed = set(codes), {}, {}, {}
    if len(wanted) != len(codes):
        raise ValueError("Duplicate expected contracts")
    for batch in batches:
        stamp = datetime.fromisoformat(batch["observed_at"])
        if stamp.tzinfo is None:
            raise ValueError("Observation requires timezone")
        for record in batch["records"]:
            payload = normalize_contract(record)
            code = payload["id"]
            if code not in wanted or code in normalized:
                raise ValueError("Unexpected or duplicate contract")
            normalized[code], raw[code], observed[code] = payload, record, stamp
    if set(normalized) != wanted:
        raise ValueError("Missing contract metadata; no snapshot published")
    daily = store.db.execute("SELECT report FROM futures_daily_snapshots WHERE version=?", [daily_version]).fetchone()
    if not daily:
        raise ValueError("Unknown daily snapshot")
    coverage = json.loads(daily[0])["contracts"]
    if set(coverage) != wanted:
        raise ValueError("Contract universe differs from pinned daily snapshot")
    terminal_gaps = []
    for code, payload in normalized.items():
        first, last = (parse_date(coverage[code][k]) for k in ("first", "last"))
        if not payload["first_trade_date"] <= first <= last <= payload["expiry"]:
            raise ValueError(f"Daily history lies outside contract lifecycle: {code}")
        if last < payload["expiry"] and payload["expiry"] < min(s.date() for s in observed.values()):
            terminal_gaps.append(code)
    if terminal_gaps:
        raise ValueError(f"Expired contract missing final trading day: {terminal_gaps[0]}")
    fingerprint = hashlib.sha256(json.dumps([daily_version, batches], default=json_default, sort_keys=True).encode()).hexdigest()
    store.db.execute("""CREATE TABLE IF NOT EXISTS futures_contract_snapshots (
        version VARCHAR PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL, daily_version VARCHAR NOT NULL,
        fingerprint VARCHAR NOT NULL, revisions JSON NOT NULL, report JSON NOT NULL)""")
    found = store.db.execute("SELECT version, report FROM futures_contract_snapshots WHERE fingerprint=?", [fingerprint]).fetchone()
    if found:
        return found[0], json.loads(found[1])
    version, revisions = uuid.uuid4().hex, {}
    report = dict(version=version, daily_version=daily_version, contracts=len(normalized),
                  lifecycle_errors=0, daily_outside_lifecycle=0, expired_final_day_missing=0,
                  pit_policy="retrieved observation; no backdating known_at", historical_sessions_ready=False)
    store.db.execute("BEGIN TRANSACTION")
    try:
        for code in sorted(normalized):
            payload, stamp = normalized[code], observed[code]
            valid_from = datetime.combine(payload["listed_date"], time.min, SHANGHAI)
            valid_to = datetime.combine(payload["delivery"] + timedelta(days=1), time.min, SHANGHAI)
            core = store.put_metadata("instrument", code, valid_from, stamp, payload, valid_to)
            original = store.put_metadata("ifind_contract_observation", code, stamp, stamp,
                                          dict(record=raw[code], provider_fields_are_native=True))
            revisions[code] = dict(instrument=core, raw=original)
        store.db.execute("INSERT INTO futures_contract_snapshots VALUES (?, ?, ?, ?, ?, ?)",
                         [version, datetime.now(timezone.utc), daily_version, fingerprint,
                          json.dumps(revisions), json.dumps(report)])
        store.db.execute("COMMIT")
    except Exception:
        store.db.execute("ROLLBACK")
        raise
    return version, report


def contract_from_snapshot(store, version, code, effective_at, as_of):
    if effective_at.tzinfo is None or as_of.tzinfo is None:
        raise ValueError("Timezone-aware PIT bounds required")
    found = store.db.execute("SELECT revisions FROM futures_contract_snapshots WHERE version=?", [version]).fetchone()
    if not found or code not in json.loads(found[0]):
        raise KeyError("Unknown snapshot/contract")
    revision = json.loads(found[0])[code]["instrument"]
    row = store.db.execute("""SELECT payload FROM metadata WHERE revision=? AND valid_from<=?
        AND (valid_to IS NULL OR ?<valid_to) AND known_at<=?""", [revision, effective_at, effective_at, as_of]).fetchone()
    if not row:
        raise KeyError("Contract not valid/known at requested time")
    return json.loads(row[0])
