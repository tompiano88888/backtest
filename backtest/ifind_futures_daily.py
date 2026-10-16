"""Date-keyed daily landing snapshots; historical intraday calendars are separate.

Keep vendor amount units explicit instead of inventing a conversion. This dataset
is not an Engine Frame: no unverified session timestamp is attached to a date.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import uuid
from decimal import InvalidOperation

from .models import D
from .storage import Store, json_default

FIELDS = {
    "open": "open", "high": "high", "low": "low", "close": "close",
    "settlement_price": "settlement", "previous_settlement_price": "preSettlement",
    "source_amount": "amount", "open_interest": "openInterest",
}
INDICATORS = "open;high;low;close;volume;amount;preSettlement;settlement;openInterest"


def validate_records(records, codes, start, end):
    start, end = date.fromisoformat(start), date.fromisoformat(end)
    if start > end or len(set(codes)) != len(codes):
        raise ValueError("Invalid date range or duplicate contracts")
    wanted = set(codes)
    if any(not re.fullmatch(r"(?:IF|IC|IM|IH)\d{2}(?:0[1-9]|1[0-2])\.CFE", c) for c in wanted):
        raise ValueError("Only actual IF/IC/IM/IH month contracts are allowed")
    by_key = {}
    for record in records:
        code = record["thscode"]
        day = date.fromisoformat(record["time"])
        if code not in wanted or not start <= day <= end:
            raise ValueError("Unexpected contract or date")
        row = dict(instrument=code, product=code[:2], trading_date=day)
        for target, source in FIELDS.items():
            if record.get(source) is None:
                raise ValueError(f"Missing {source}: {code}/{day}")
            try:
                row[target] = D(record[source])
            except (ValueError, InvalidOperation):
                raise ValueError(f"Invalid numeric field {source}: {code}/{day}") from None
        if record.get("volume") is None:
            raise ValueError(f"Missing volume: {code}/{day}")
        try:
            volume = D(record["volume"])
        except (ValueError, InvalidOperation):
            raise ValueError("Invalid numeric volume") from None
        if volume < 0 or volume != int(volume):
            raise ValueError("Invalid integer volume")
        row["volume"] = int(volume)
        if (row["low"] > min(row["open"], row["close"]) or
                row["high"] < max(row["open"], row["close"]) or row["low"] > row["high"] or
                min(row["low"], row["settlement_price"], row["previous_settlement_price"]) <= 0 or
                row["source_amount"] < 0 or row["open_interest"] < 0):
            raise ValueError(f"Invalid OHLCV/settlement: {code}/{day}")
        key = code, day
        if key in by_key and by_key[key] != row:
            raise ValueError("Conflicting duplicate daily record")
        by_key[key] = row
    by_code = defaultdict(list)
    for key in sorted(by_key):
        by_code[key[0]].append(by_key[key])
    if set(by_code) != wanted:
        raise ValueError("Missing contracts; no complete snapshot published")
    product_days = defaultdict(set)
    for rows in by_code.values():
        product_days[rows[0]["product"]].update(r["trading_date"] for r in rows)
    summary = {}
    for code, rows in by_code.items():
        observed = {r["trading_date"] for r in rows}
        missing = sorted(d for d in product_days[code[:2]] if rows[0]["trading_date"] <= d <= rows[-1]["trading_date"] and d not in observed)
        if missing:
            raise ValueError(f"Internal date gaps: {code}/{missing[0]}")
        for previous, current in zip(rows, rows[1:]):
            if previous["settlement_price"] != current["previous_settlement_price"]:
                raise ValueError(f"Previous settlement discontinuity: {code}/{current['trading_date']}")
        summary[code] = dict(rows=len(rows), first=rows[0]["trading_date"], last=rows[-1]["trading_date"])
    return by_code, summary


def publish_daily(store: Store, records, config):
    """Validate complete input, write immutable files, then atomically publish manifest.

    Dedicated date-keyed manifests do not enter Store.bars until historical
    sessions and contract metadata can be assembled by a Provider.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq
    by_code, summary = validate_records(records, config["codes"], config["start"], config["end"])
    fingerprint = hashlib.sha256(json.dumps(
        [config["start"], config["end"], [row for rows in by_code.values() for row in rows]],
        default=json_default, sort_keys=True).encode()).hexdigest()
    store.db.execute("""CREATE TABLE IF NOT EXISTS futures_daily_snapshots (
        version VARCHAR PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL,
        fingerprint VARCHAR NOT NULL, manifest JSON NOT NULL, report JSON NOT NULL)""")
    existing = store.db.execute("SELECT version, report, manifest FROM futures_daily_snapshots WHERE fingerprint=? ORDER BY created_at LIMIT 1", [fingerprint]).fetchone()
    if existing:
        for entry in json.loads(existing[2]):
            path = (store.root / entry["path"]).resolve()
            if not path.is_relative_to(store.root.resolve()) or hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
                raise ValueError("Corrupt daily partition")
        return existing[0], json.loads(existing[1])
    version = uuid.uuid4().hex
    schema = pa.schema([pa.field("instrument", pa.string()), pa.field("product", pa.string()),
                        pa.field("trading_date", pa.date32()), pa.field("volume", pa.int64())] +
                       [pa.field(field, pa.decimal128(28, 8)) for field in FIELDS])
    manifest = []
    for code, rows in by_code.items():
        relative = f"daily/{version}/{code}.parquet"
        path = store.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        pq.write_table(pa.Table.from_pylist(rows, schema=schema), temp, compression="zstd")
        temp.replace(path)
        restored = pq.read_table(path).to_pylist()
        if restored != rows:
            raise ValueError("Parquet decimal/date roundtrip changed values")
        manifest.append(dict(instrument=code, path=relative, rows=len(rows), sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
    products = {}
    for product in ("IF", "IC", "IM", "IH"):
        entries = [v for c, v in summary.items() if c.startswith(product)]
        if entries:
            products[product] = dict(contracts=len(entries), rows=sum(e["rows"] for e in entries),
                                     first=min(e["first"] for e in entries), last=max(e["last"] for e in entries))
    report = dict(version=version, products=products, contracts=summary,
                  total_rows=sum(len(v) for v in by_code.values()), null_required_fields=0,
                  conflicting_duplicates=0, internal_product_calendar_gaps=0,
                  previous_settlement_discontinuities=0, parquet_roundtrip_equal=True,
                  source="iFinD THS_HQ", source_amount_unit="vendor native; not converted",
                  volume_unit="vendor native integer; contract-count convention not independently verified",
                  coverage="all returned dates; observed local catalog plus verified recent contracts",
                  limitation="Independent exchange calendar and official listing/delivery metadata not yet reconciled; no intraday timestamps generated")
    now = datetime.now(timezone.utc)
    store.db.execute("BEGIN TRANSACTION")
    try:
        store.db.execute("INSERT INTO futures_daily_snapshots VALUES (?, ?, ?, ?, ?)",
                         [version, now, fingerprint, json.dumps(manifest), json.dumps(report, default=json_default)])
        for code in by_code:
            store.put_metadata("observed_futures_daily_catalog", code, now, now,
                               dict(source="iFinD", instrument=code, product=code[:2], snapshot=version))
        store.db.execute("COMMIT")
    except Exception:
        store.db.execute("ROLLBACK")
        raise
    return version, report


def publish_cache(config, root):
    records = []
    size = config.get("batch_size", 5)
    for offset in range(0, len(config["codes"]), size):
        request = dict(function="THS_HQ", codes=",".join(config["codes"][offset:offset + size]),
                       indicators=INDICATORS, parameters="Interval:D,CPS:1,Fill:Omit", start=config["start"], end=config["end"])
        digest = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
        cached = json.loads((Path(config["cache"]) / f"{digest}.json").read_text(encoding="utf-8"))
        if cached["request"] != request:
            raise ValueError("Cache request mismatch")
        records.extend(cached["records"])
    with Store(root) as store:
        return publish_daily(store, records, config)
