from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import asdict
from datetime import datetime, timezone, date
from decimal import Decimal
from pathlib import Path

from .models import Bar, D


def json_default(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    raise TypeError(type(value).__name__)


class Store:
    """Single-writer metadata DB + immutable, hash-verified Parquet snapshots.

    ``read_only=True`` opens the DuckDB file read-only so several replay processes (for
    example a parameter grid) can read one snapshot concurrently. DuckDB refuses a
    read-write open while any other process holds the file, so only ``update`` jobs
    should open the store for writing.
    """

    def __init__(self, root, read_only=False):
        import duckdb
        self.root = Path(root)
        self.read_only = bool(read_only)
        if self.read_only:
            path = self.root / "metadata.duckdb"
            if not path.exists():
                raise FileNotFoundError(f"No store at {self.root}")
            self.db = duckdb.connect(str(path), read_only=True)
            return
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = duckdb.connect(str(self.root / "metadata.duckdb"))
        self.db.execute("""CREATE TABLE IF NOT EXISTS metadata (
            revision VARCHAR PRIMARY KEY, kind VARCHAR NOT NULL, key VARCHAR NOT NULL,
            valid_from TIMESTAMPTZ NOT NULL, valid_to TIMESTAMPTZ,
            known_at TIMESTAMPTZ NOT NULL, recorded_at TIMESTAMPTZ NOT NULL, payload JSON NOT NULL)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS datasets (
            version VARCHAR PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL, source VARCHAR NOT NULL,
            manifest JSON NOT NULL)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS update_runs (
            id VARCHAR, started_at TIMESTAMPTZ, status VARCHAR, detail VARCHAR)""")

    def _require_writable(self):
        if self.read_only:
            raise PermissionError("Store was opened read-only")

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def put_metadata(self, kind, key, valid_from, known_at, payload, valid_to=None):
        self._require_writable()
        if valid_from.tzinfo is None or known_at.tzinfo is None or valid_to is not None and (valid_to.tzinfo is None or valid_to <= valid_from):
            raise ValueError("Metadata requires aware, valid temporal bounds")
        content = json.dumps([kind, key, valid_from, valid_to, known_at, payload], default=json_default, sort_keys=True)
        revision = hashlib.sha256(content.encode()).hexdigest()
        self.db.execute("INSERT INTO metadata VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                        [revision, kind, key, valid_from, valid_to, known_at, datetime.now(timezone.utc), json.dumps(payload, default=json_default)])
        return revision

    def metadata(self, kind, key, effective_at, as_of, version=None):
        if effective_at.tzinfo is None or as_of.tzinfo is None:
            raise ValueError("PIT queries require timezone-aware timestamps")
        cutoff = datetime.now(timezone.utc)
        if version:
            row = self.db.execute("SELECT created_at FROM datasets WHERE version=?", [version]).fetchone()
            if not row:
                raise KeyError(version)
            cutoff = row[0]
        rows = self.db.execute("""SELECT payload FROM metadata WHERE kind=? AND key=?
            AND valid_from<=? AND (valid_to IS NULL OR ?<valid_to) AND known_at<=? AND recorded_at<=?
            ORDER BY known_at DESC, recorded_at DESC, revision DESC LIMIT 1""", [kind, key, effective_at, effective_at, as_of, cutoff]).fetchone()
        if not rows:
            raise KeyError(f"Missing point-in-time metadata: {kind}/{key}")
        return json.loads(rows[0])

    def latest(self):
        row = self.db.execute("SELECT version FROM datasets ORDER BY created_at DESC, version DESC LIMIT 1").fetchone()
        return row[0] if row else None

    def manifest(self, version):
        row = self.db.execute("SELECT manifest FROM datasets WHERE version=?", [version]).fetchone()
        if not row:
            raise KeyError(version)
        return json.loads(row[0])

    def publish(self, bars: list[Bar], source: str, frequency="1m", parent=None):
        self._require_writable()
        import pyarrow as pa
        import pyarrow.parquet as pq
        if frequency not in ("1m", "1d") or not bars:
            raise ValueError("Nonempty 1m/1d batch required")
        keys = [(b.instrument, b.end) for b in bars]
        if len(set(keys)) != len(keys):
            raise ValueError("Duplicate bars in input batch")
        manifest = dict(self.manifest(parent)) if parent else {}
        partitions = {}
        for b in bars:
            key = json.dumps([frequency, b.instrument, str(b.trading_date)])
            partitions.setdefault(key, []).append(b)
        version = uuid.uuid4().hex
        for key, batch in partitions.items():
            merged = {}
            if key in manifest:
                for bar in self._read_partition(manifest[key]):
                    merged[bar.end] = bar
            merged.update({b.end: b for b in batch})
            rows = [asdict(b) for b in sorted(merged.values(), key=lambda b: b.end)]
            # Arrow's inference preserves Decimal values; each daily partition is independent.
            table = pa.Table.from_pylist(rows)
            relative = f"bars/{frequency}/{version}/{hashlib.sha256(key.encode()).hexdigest()}.parquet"
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(".tmp")
            pq.write_table(table, temporary, compression="zstd")
            os.replace(temporary, target)
            manifest[key] = dict(path=relative, sha256=hashlib.sha256(target.read_bytes()).hexdigest(), rows=len(rows))
        # Only a successfully committed manifest is visible. Orphan files are harmless.
        self.db.execute("INSERT INTO datasets VALUES (?, ?, ?, ?)", [version, datetime.now(timezone.utc), source, json.dumps(manifest)])
        return version

    def _read_partition(self, entry):
        import pyarrow.parquet as pq
        path = (self.root / entry["path"]).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise ValueError("Invalid partition path")
        if hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
            raise ValueError("Corrupt Parquet partition")
        return [Bar(**row) for row in pq.read_table(path).to_pylist()]

    def bars(self, version, instruments, start, end, frequency="1m"):
        result = []
        for key, entry in self.manifest(version).items():
            freq, instrument, day = json.loads(key)
            if freq == frequency and instrument in instruments:
                # Trading date may differ from timestamp date for night sessions.
                result.extend(b for b in self._read_partition(entry) if start <= b.end <= end)
        return sorted(result, key=lambda b: (b.end, b.instrument))


PRICE_FIELDS = {"open", "high", "low", "close", "settlement_price", "previous_settlement_price", "upper_limit", "lower_limit", "turnover", "open_interest"}


def bar_from_record(record):
    row = dict(record)
    for key in PRICE_FIELDS:
        if row.get(key) is not None:
            row[key] = D(row[key])
    for key in ("start", "end"):
        if not isinstance(row[key], datetime):
            row[key] = datetime.fromisoformat(row[key])
    if not isinstance(row["trading_date"], date):
        row["trading_date"] = date.fromisoformat(row["trading_date"])
    volume = D(row["volume"])
    if volume != int(volume):
        raise ValueError("Volume must be normalized to integer units")
    row["volume"] = int(volume)
    return Bar(**row)
