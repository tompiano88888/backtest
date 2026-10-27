"""Download date-keyed raw index/ETF history, no adjustment or synthetic fill."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from backtest.ifind import IFinDSource
from backtest.storage import Store, json_default
from backtest.ifind_index_etf import nav_enabled


def acquire(config, publish_only=False):
    with_nav = nav_enabled(config)
    cache = Path(config["cache"])
    cache.mkdir(parents=True, exist_ok=True)
    source = None
    batches = []
    try:
        for code, profile in config["instruments"].items():
            indicators = "open;high;low;close;volume;amount"
            if with_nav and profile["asset"] == "fund":
                indicators += ";netAssetValue"
            request = dict(function="THS_HQ", codes=code, indicators=indicators,
                           parameters="Interval:D,CPS:1,Fill:Omit", start=config["start"], end=config["end"])
            key = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
            target = cache / f"{key}.json"
            if not target.exists():
                if publish_only:
                    raise ValueError("Missing cached response; publication-only mode makes no API calls")
                if source is None:
                    source = IFinDSource()
                    source.login()
                records = source.fetch(request)
                if not records or {r.get("thscode") for r in records} != {code}:
                    raise ValueError(f"Missing/unexpected data: {code}")
                content = dict(request=request, records=records, observed_at=datetime.now(timezone.utc).isoformat())
                temp = target.with_suffix(".tmp")
                temp.write_text(json.dumps(content, default=json_default), encoding="utf-8")
                temp.replace(target)
            batch = json.loads(target.read_text(encoding="utf-8"))
            if batch["request"] != request:
                raise ValueError("Cache request mismatch")
            batches.append(batch)
            print(f"{code}: {len(batch['records'])} rows cached", flush=True)
    finally:
        if source:
            source.close()
    return batches


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", nargs="?", default="examples/ifind.index-etf-daily.json")
    parser.add_argument("--root", default="data/index_etf")
    parser.add_argument("--publish-only", action="store_true")
    parser.add_argument("--report", default="outputs/ifind-index-etf-validation.json")
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
    batches = acquire(config, args.publish_only)
    from backtest.ifind_index_etf import publish_daily
    with Store(args.root) as store:
        version, report = publish_daily(store, batches, config)
    output = Path(args.report)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, default=json_default, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Published {version}: {report['total_rows']} rows")


if __name__ == "__main__":
    main()
