"""Acquire confirmed iFinD daily fields into resumable, unpublished raw batches.

Run from the repository with python -m scripts.download_index_futures CONFIG.
No credentials or vendor error messages are written to cache or logs.
"""
from __future__ import annotations

import hashlib
import argparse
import json
from pathlib import Path

from backtest.ifind import IFinDSource
from backtest.storage import json_default
from backtest.ifind_futures_daily import INDICATORS, publish_cache



def acquire(config, source, progress=print):
    root = Path(config["cache"])
    root.mkdir(parents=True, exist_ok=True)
    codes = config["codes"]
    size = config.get("batch_size", 5)
    if type(size) is not int or size < 1 or len(set(codes)) != len(codes):
        raise ValueError("Invalid batch size or duplicate codes")
    for offset in range(0, len(codes), size):
        group = codes[offset:offset + size]
        request = dict(function="THS_HQ", codes=",".join(group), indicators=INDICATORS,
                       parameters="Interval:D,CPS:1,Fill:Omit", start=config["start"], end=config["end"])
        digest = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
        target = root / f"{digest}.json"
        if not target.exists():
            rows = source.fetch(request)
            if {r.get("thscode") for r in rows} != set(group):
                raise ValueError(f"Incomplete batch: {','.join(group)}")
            content = dict(request=request, records=rows)
            temp = target.with_suffix(".tmp")
            temp.write_text(json.dumps(content, default=json_default), encoding="utf-8")
            temp.replace(target)
        progress(f"{min(offset + size, len(codes))}/{len(codes)} contracts cached", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config")
    parser.add_argument("--root", default="data/index_futures")
    parser.add_argument("--publish-only", action="store_true", help="Validate and publish cached responses without login")
    parser.add_argument("--report", default="outputs/ifind-index-futures-validation.json")
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
    if not args.publish_only:
        with IFinDSource() as source:
            acquire(config, source)
    version, report = publish_cache(config, args.root)
    output = Path(args.report)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, default=json_default, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Published {version}: {report['total_rows']} rows")


if __name__ == "__main__":
    main()
