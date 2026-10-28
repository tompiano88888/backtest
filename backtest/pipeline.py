from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone

from .storage import bar_from_record


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
        version = store.publish(bars, source="normalized", frequency=frequency, parent=store.latest())
    except Exception:
        store.db.execute("INSERT INTO update_runs VALUES (?, ?, ?, ?)", [run, started, "failed", "validation/publication failed"])
        raise
    store.db.execute("INSERT INTO update_runs VALUES (?, ?, ?, ?)", [run, started, "completed", version])
    return version


def configured_normalizer(mapping):
    """Column mapping must supply explicit timezone-aware timestamps/trading dates.

    Normalization needs explicit calendar-derived trading dates;
    use a project adapter rather than infer a night session's date from its clock.
    """
    def normalize(row):
        normalized = {target: row[source] for target, source in mapping.items()}
        return bar_from_record(normalized)
    return normalize
