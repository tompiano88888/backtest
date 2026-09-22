from __future__ import annotations

import importlib
import json
import os
import time
import uuid
from datetime import datetime, timezone

from .storage import bar_from_record


class IFinDSource:
    """SDK transport only. Vendor fields/units/calendar mapping are explicit config."""

    def __init__(self, sdk=None):
        self.sdk = sdk or importlib.import_module("iFinDPy")

    def login(self):
        result = self.sdk.THS_iFinDLogin(os.environ["IFIND_USERNAME"], os.environ["IFIND_PASSWORD"])
        if result != 0:
            raise RuntimeError(f"iFinD login failed: {result}")

    def fetch(self, request):
        method = {"1d": "THS_HQ", "1m": "THS_HF"}[request["frequency"]]
        result = getattr(self.sdk, method)(request["codes"], request["indicators"], request["parameters"], request["start"], request["end"])
        if getattr(result, "errorcode", None) != 0:
            raise RuntimeError(f"iFinD query failed: {getattr(result, 'errorcode', 'unknown response')}")
        if not hasattr(result, "data"):
            raise ValueError("SDK response has no data table")
        return result.data.to_dict("records")


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
