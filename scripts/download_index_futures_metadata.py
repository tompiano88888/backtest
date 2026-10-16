"""Download confirmed contract fields for a fixed futures daily snapshot."""
from datetime import datetime, timezone
import argparse
import hashlib
import json
from pathlib import Path

from backtest.ifind import IFinDSource
from backtest.ifind_contracts import publish_contracts
from backtest.storage import Store, json_default


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="data/index_futures")
    parser.add_argument("--daily-version", required=True)
    parser.add_argument("--request", default="examples/ifind.futures-contracts.json")
    parser.add_argument("--cache", default="data/index_futures/contract-staging/20261009")
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--publish-only", action="store_true")
    args = parser.parse_args()
    if args.batch_size < 1:
        parser.error("Positive batch size required")
    request = json.loads(Path(args.request).read_text(encoding="utf-8-sig"))["request"]
    if request["function"] != "THS_BD" or len(request["indicators"].split(";")) != len(request["parameters"].split(";")):
        parser.error("Confirmed THS_BD request with aligned parameters required")
    with Store(args.root) as store:
        row = store.db.execute("SELECT report FROM futures_daily_snapshots WHERE version=?", [args.daily_version]).fetchone()
        if not row:
            parser.error("Unknown pinned daily snapshot")
        codes = sorted(json.loads(row[0])["contracts"])
    cache = Path(args.cache)
    cache.mkdir(parents=True, exist_ok=True)
    batches = []
    source = None
    try:
        for offset in range(0, len(codes), args.batch_size):
            group = codes[offset:offset + args.batch_size]
            query = dict(request, codes=",".join(group))
            digest = hashlib.sha256(json.dumps(query, sort_keys=True).encode()).hexdigest()
            target = cache / f"{digest}.json"
            if not target.exists():
                if args.publish_only:
                    raise ValueError("Missing cache batch")
                if source is None:
                    source = IFinDSource()
                    source.login()
                records = source.fetch(query)
                if {r.get("thscode") for r in records} != set(group):
                    raise ValueError("Incomplete contract batch")
                content = dict(request=query, observed_at=datetime.now(timezone.utc).isoformat(), records=records)
                temp = target.with_suffix(".tmp")
                temp.write_text(json.dumps(content, default=json_default, ensure_ascii=False), encoding="utf-8")
                temp.replace(target)
            batch = json.loads(target.read_text(encoding="utf-8"))
            if batch["request"] != query:
                raise ValueError("Cached request mismatch")
            batches.append(batch)
            print(f"{min(offset + args.batch_size, len(codes))}/{len(codes)} contracts cached", flush=True)
    finally:
        if source is not None:
            source.close()
    with Store(args.root) as store:
        version, report = publish_contracts(store, batches, codes, args.daily_version)
    output = Path("outputs/ifind-index-futures-metadata-validation.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Published {version}: {report['contracts']} contracts")


if __name__ == "__main__":
    main()
