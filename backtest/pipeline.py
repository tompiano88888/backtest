from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .storage import bar_from_record
from .ifind import IFinDSource, IFinDConfigurationError, IFinDError, market_requests
from .ifind_normalize import IFinDBarNormalizer, SessionCalendar


def update(store, fetch, normalize, frequency, retries=3, sleep=time.sleep):
    """Atomic publication, bounded retries, no watermark advance on errors."""
    run = uuid.uuid4().hex
    started = datetime.now(timezone.utc)
    for attempt in range(retries):
        try:
            records = fetch()
            break
        except Exception:
            if attempt + 1 == retries:
                store.db.execute("INSERT INTO update_runs VALUES (?, ?, ?, ?)", [run, started, "failed", "fetch failed; no dataset published"])
                raise
            sleep(min(2 ** attempt, 8))
    else:
        raise ValueError("retries must be positive")
    try:
        bars = [normalize(row) for row in records]
        if not bars:
            raise ValueError("Empty response: explicit no-data review required")
        version = store.publish(bars, source="iFinD", frequency=frequency, parent=store.latest())
    except Exception:
        store.db.execute("INSERT INTO update_runs VALUES (?, ?, ?, ?)", [run, started, "failed", "validation/publication failed"])
        raise
    store.db.execute("INSERT INTO update_runs VALUES (?, ?, ?, ?)", [run, started, "completed", version])
    return version


def configured_normalizer(mapping):
    """Column mapping must supply explicit timezone-aware timestamps/trading dates.

    Production iFinD normalization often needs calendar-derived trading dates;
    use a project adapter rather than infer a night session's date from its clock.
    """
    def normalize(row):
        normalized = {target: row[source] for target, source in mapping.items()}
        return bar_from_record(normalized)
    return normalize


def prepare_ifind_update(config, config_dir="."):
    """Validate all local inputs before login, quota use or publication."""
    requests = list(market_requests(config["request"], config.get("batch_size", 20), config.get("chunk_days", 5)))
    if "normalization" not in config or "calendar_file" not in config:
        raise IFinDConfigurationError("Update requires normalization and calendar_file; migrate legacy column_mapping (docs/IFIND.md)")
    path = Path(config_dir) / config["calendar_file"]
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    normalizer = IFinDBarNormalizer(config["normalization"], config["request"]["frequency"], SessionCalendar(rows))
    codes = {code for request in requests for code in request["codes"].split(",")}
    if not codes <= normalizer.instruments.keys():
        raise IFinDConfigurationError("Some requested codes have no normalization profile")
    for code in codes:
        if normalizer.instruments[code]["id"] not in normalizer.calendar.by_instrument:
            raise IFinDConfigurationError("Some requested instruments have no trading sessions")
    return requests, normalizer


def update_ifind(store, source, requests, normalizer):
    """All batches succeed and validate before one manifest becomes visible."""
    run, started = uuid.uuid4().hex, datetime.now(timezone.utc)
    try:
        bars = {}
        for request in requests:
            records = source.fetch(request)
            if not records:
                raise IFinDError("Empty batch: check permissions/calendar; no dataset published")
            allowed_codes = set(request["codes"].split(","))
            if {row.get("thscode") for row in records} != allowed_codes:
                raise IFinDError("iFinD returned an incomplete security batch; check permissions or split the request")
            lower, upper = (datetime.fromisoformat(request[k]) for k in ("start", "end"))
            for row in records:
                if row.get("thscode") not in allowed_codes:
                    raise IFinDError("iFinD returned an unrequested security")
                try:
                    stamp = datetime.fromisoformat(str(row["time"]))
                    if stamp.tzinfo is not None:
                        from .ifind_normalize import SHANGHAI
                        stamp = stamp.astimezone(SHANGHAI).replace(tzinfo=None)
                except (KeyError, ValueError):
                    raise IFinDError("Invalid response timestamp") from None
                compared = stamp.date() if normalizer.frequency == "1d" else stamp
                bounds = (lower.date(), upper.date()) if normalizer.frequency == "1d" else (lower, upper)
                if not bounds[0] <= compared <= bounds[1]:
                    raise IFinDError("iFinD returned a timestamp outside the requested range")
                bar = normalizer(row)
                key = bar.instrument, bar.end
                if key in bars and bars[key] != bar:
                    raise IFinDError("Conflicting duplicate iFinD bars")
                bars[key] = bar
        if not bars:
            raise IFinDError("No bars to publish")
        # Couple the manifest and success record in one database transaction.
        store.db.execute("BEGIN TRANSACTION")
        try:
            version = store.publish(list(bars.values()), "iFinD", normalizer.frequency, parent=store.latest())
            store.db.execute("INSERT INTO update_runs VALUES (?, ?, ?, ?)", [run, started, "completed", version])
            store.db.execute("COMMIT")
        except Exception:
            store.db.execute("ROLLBACK")
            raise
    except Exception:
        store.db.execute("INSERT INTO update_runs VALUES (?, ?, ?, ?)", [run, started, "failed", "iFinD fetch/validation/publication failed; no dataset published"])
        raise
    return version
