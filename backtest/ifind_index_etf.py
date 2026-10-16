"""Date-keyed index/ETF observations; no invented volume units or sessions."""
from datetime import date, datetime, timezone
from decimal import InvalidOperation
import hashlib
import json
import uuid

from .models import D
from .storage import json_default


def nav_enabled(config):
    enabled = config.get("include_etf_nav", False)
    if type(enabled) is not bool:
        raise ValueError("include_etf_nav must be boolean")
    return enabled


def unit_nav_value(raw):
    if "netAssetValue" not in raw:
        raise ValueError("Requested ETF NAV field missing from response")
    try:
        nav = D(raw["netAssetValue"]) if raw["netAssetValue"] is not None else None
    except (ValueError, InvalidOperation):
        raise ValueError("Invalid ETF unit NAV") from None
    if nav is not None and (not nav.is_finite() or nav <= 0):
        raise ValueError("ETF unit NAV must be finite and positive")
    return nav


def normalize_nav_batches(batches, config):
    """Keep NAV-only dates, including pre-listing observations, out of market bars."""
    grouped = {code: {} for code, profile in config["instruments"].items() if profile["asset"] == "fund"}
    lower, upper = date.fromisoformat(config["start"]), date.fromisoformat(config["end"])
    for batch in batches:
        observed = datetime.fromisoformat(batch["observed_at"])
        if observed.tzinfo is None:
            raise ValueError("Aware observation time required")
        for raw in batch["records"]:
            code = raw.get("thscode")
            if code not in config["instruments"]:
                raise ValueError("Unexpected security")
            if code not in grouped:
                continue
            day = date.fromisoformat(raw["time"])
            if not lower <= day <= upper:
                raise ValueError("Out-of-range NAV date")
            nav = unit_nav_value(raw)
            row = dict(instrument=code, trading_date=day, unit_nav=nav,
                       unit_nav_known_at=observed if nav is not None else None)
            if day in grouped[code] and grouped[code][day] != row:
                raise ValueError("Conflicting duplicate NAV record")
            grouped[code][day] = row
    for code, rows in grouped.items():
        if not any(r["unit_nav"] is not None for r in rows.values()):
            raise ValueError(f"No ETF unit NAV returned: {code}")
    return {code: [rows[day] for day in sorted(rows)] for code, rows in sorted(grouped.items())}


def normalize_batches(batches, config):
    lower, upper = date.fromisoformat(config["start"]), date.fromisoformat(config["end"])
    if lower > upper or not config["instruments"]:
        raise ValueError("Invalid date range/universe")
    with_nav = nav_enabled(config)
    records, observations = {}, {}
    for batch in batches:
        observed = datetime.fromisoformat(batch["observed_at"])
        if observed.tzinfo is None:
            raise ValueError("Aware observation time required")
        for raw in batch["records"]:
            code = raw.get("thscode")
            if code not in config["instruments"]:
                raise ValueError("Unexpected security")
            profile = config["instruments"][code]
            if profile["asset"] not in ("index", "fund"):
                raise ValueError("Only index and fund supported")
            day = date.fromisoformat(raw["time"])
            if not lower <= day <= upper:
                raise ValueError("Out-of-range date")
            if with_nav and profile["asset"] == "fund" and all(raw.get(k) is None for k in ("open", "high", "low", "close")):
                if unit_nav_value(raw) is None or any(raw.get(k) is not None for k in ("volume", "amount")):
                    raise ValueError("Missing price without a valid NAV-only observation")
                continue
            row = dict(instrument=code, asset=profile["asset"], product=profile["product"], trading_date=day)
            for target, source in [(f, f) for f in ("open", "high", "low", "close")] + [("source_volume", "volume"), ("source_amount", "amount")]:
                value = raw.get(source)
                if value is None:
                    if target in ("open", "high", "low", "close"):
                        raise ValueError("Missing price")
                    row[target] = None
                    continue
                try:
                    row[target] = D(value)
                except (ValueError, InvalidOperation):
                    raise ValueError("Invalid numeric daily field") from None
            if (row["low"] <= 0 or row["low"] > min(row["open"], row["close"]) or
                row["high"] < max(row["open"], row["close"]) or row["low"] > row["high"]):
                raise ValueError("Invalid OHLC")
            if any(row[k] is not None and row[k] < 0 for k in ("source_volume", "source_amount")):
                raise ValueError("Negative volume/amount")
            if row["asset"] == "fund" and row["source_volume"] is not None and row["source_volume"] != int(row["source_volume"]):
                raise ValueError("ETF volume requires unit review before fractional quantities can be accepted")
            row["ohlcv_complete"] = row["source_volume"] is not None and row["source_amount"] is not None
            if with_nav:
                nav = unit_nav_value(raw) if row["asset"] == "fund" else None
                row.update(unit_nav=nav, unit_nav_known_at=observed if nav is not None else None)
            key = code, day
            if key in records and records[key] != row:
                raise ValueError("Conflicting duplicate daily record")
            records[key] = row
            observations[code] = max(observed, observations.get(code, observed))
    if {code for code, day in records} != set(config["instruments"]):
        raise ValueError("Missing security; no complete snapshot")
    grouped = {code: [] for code in sorted(config["instruments"])}
    for (code, day), row in sorted(records.items()):
        grouped[code].append(row)
    return grouped, observations


def publish_daily(store, batches, config):
    import pyarrow as pa
    import pyarrow.parquet as pq
    grouped, observed = normalize_batches(batches, config)
    nav_grouped = normalize_nav_batches(batches, config) if nav_enabled(config) else {}
    content = [config["instruments"], grouped]
    if nav_grouped:
        content.append(nav_grouped)
    fingerprint = hashlib.sha256(json.dumps(content, sort_keys=True, default=json_default).encode()).hexdigest()
    store.db.execute("""CREATE TABLE IF NOT EXISTS index_etf_daily_snapshots (
        version VARCHAR PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL, fingerprint VARCHAR NOT NULL,
        manifest JSON NOT NULL, report JSON NOT NULL)""")
    found = store.db.execute("SELECT version, manifest, report FROM index_etf_daily_snapshots WHERE fingerprint=?", [fingerprint]).fetchone()
    if found:
        saved_report = json.loads(found[2])
        for entry in json.loads(found[1]) + saved_report.get("nav_manifest", []):
            path = (store.root / entry["path"]).resolve()
            if not path.is_relative_to(store.root.resolve()) or hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
                raise ValueError("Corrupt index/ETF partition")
        return found[0], saved_report
    version, manifest, symbols = uuid.uuid4().hex, [], {}
    fields = ([pa.field("instrument", pa.string()), pa.field("asset", pa.string()),
                        pa.field("product", pa.string()), pa.field("trading_date", pa.date32()),
                        pa.field("ohlcv_complete", pa.bool_())] +
                       [pa.field(k, pa.decimal128(30, 8)) for k in ("open", "high", "low", "close", "source_volume", "source_amount")])
    if nav_enabled(config):
        fields += [pa.field("unit_nav", pa.decimal128(30, 8)), pa.field("unit_nav_known_at", pa.timestamp("us", tz="UTC"))]
    schema = pa.schema(fields)
    for code, rows in grouped.items():
        relative = f"daily/{version}/{code}.parquet"
        path = store.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        pq.write_table(pa.Table.from_pylist(rows, schema=schema), temp, compression="zstd")
        temp.replace(path)
        if pq.read_table(path).to_pylist() != rows:
            raise ValueError("Decimal/date Parquet roundtrip changed data")
        manifest.append(dict(instrument=code, path=relative, rows=len(rows), sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
        complete = [r for r in rows if r["ohlcv_complete"]]
        symbols[code] = dict(config["instruments"][code], rows=len(rows), first=rows[0]["trading_date"], last=rows[-1]["trading_date"],
                             missing_volume=sum(r["source_volume"] is None for r in rows),
                             missing_amount=sum(r["source_amount"] is None for r in rows),
                             incomplete_dates=[r["trading_date"] for r in rows if not r["ohlcv_complete"]],
                             first_complete_ohlcv=complete[0]["trading_date"] if complete else None)
        if nav_enabled(config) and config["instruments"][code]["asset"] == "fund":
            available = [r for r in rows if r["unit_nav"] is not None]
            symbols[code].update(nav_rows=len(available), missing_nav=len(rows)-len(available),
                                 missing_nav_dates=[r["trading_date"] for r in rows if r["unit_nav"] is None],
                                 first_nav=available[0]["trading_date"], last_nav=available[-1]["trading_date"])
    report = dict(version=version, total_rows=sum(len(v) for v in grouped.values()), symbols=symbols,
                  price_adjustment="none", fill="omit", source="iFinD THS_HQ",
                  source_volume_unit="vendor native, index may be fractional", source_amount_unit="vendor native",
                  incomplete_policy="null values preserved and flagged; incomplete ETF records not usable for matching",
                  limitation="No verified listing dates, index publication dates, intraday calendar or corporate actions; no Engine Frame assembled",
                  parquet_roundtrip_equal=True)
    if nav_enabled(config):
        report.update(unit_nav_source="iFinD THS_HQ netAssetValue", unit_nav_kind="daily fund unit NAV; not intraday IOPV",
                      unit_nav_known_at_policy="retrieval time; historical publication times unverified",
                      unit_nav_null_policy="preserve null; no forward fill or substitution with market close")
        nav_schema = pa.schema([pa.field("instrument", pa.string()), pa.field("trading_date", pa.date32()),
                                pa.field("unit_nav", pa.decimal128(30, 8)), pa.field("unit_nav_known_at", pa.timestamp("us", tz="UTC"))])
        report["nav_manifest"] = []
        for code, rows in nav_grouped.items():
            relative = f"nav_daily/{version}/{code}.parquet"
            path = store.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(".tmp")
            pq.write_table(pa.Table.from_pylist(rows, schema=nav_schema), temp, compression="zstd")
            temp.replace(path)
            if pq.read_table(path).to_pylist() != rows:
                raise ValueError("NAV Parquet roundtrip changed data")
            report["nav_manifest"].append(dict(instrument=code, path=relative, rows=len(rows), sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
            market_dates = {r["trading_date"] for r in grouped[code]}
            symbols[code].update(nav_series_rows=len(rows), nav_series_nonnull=sum(r["unit_nav"] is not None for r in rows),
                                 nav_only_dates=[r["trading_date"] for r in rows if r["trading_date"] not in market_dates],
                                 nav_series_first=rows[0]["trading_date"], nav_series_last=rows[-1]["trading_date"])
    store.db.execute("BEGIN TRANSACTION")
    try:
        store.db.execute("INSERT INTO index_etf_daily_snapshots VALUES (?, ?, ?, ?, ?)",
                         [version, datetime.now(timezone.utc), fingerprint, json.dumps(manifest), json.dumps(report, default=json_default)])
        for code in grouped:
            store.put_metadata("observed_index_etf_catalog", code, observed[code], observed[code],
                               dict(config["instruments"][code], snapshot=version, source="iFinD"))
        store.db.execute("COMMIT")
    except Exception:
        store.db.execute("ROLLBACK")
        raise
    return version, report


def nav_from_snapshot(store, version, code, *, as_of=None):
    """Read complete date-keyed unit NAVs; optional conservative PIT bounds."""
    import pyarrow.parquet as pq
    if as_of is not None and as_of.tzinfo is None:
        raise ValueError("as_of requires timezone")
    row = store.db.execute("SELECT report FROM index_etf_daily_snapshots WHERE version=?", [version]).fetchone()
    if not row:
        raise KeyError(version)
    entries = [e for e in json.loads(row[0]).get("nav_manifest", []) if e["instrument"] == code]
    if not entries:
        raise KeyError(f"No NAV series in snapshot: {code}")
    entry = entries[0]
    path = (store.root / entry["path"]).resolve()
    if not path.is_relative_to(store.root.resolve()) or hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
        raise ValueError("Corrupt NAV partition")
    rows = pq.read_table(path).to_pylist()
    if as_of is not None:
        rows = [r for r in rows if r["unit_nav_known_at"] is not None and r["unit_nav_known_at"] <= as_of]
    return rows
