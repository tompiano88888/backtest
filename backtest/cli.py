from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path

from .models import Bar, D
from .pipeline import IFinDSource, configured_normalizer, update
from .runner import result, run_scenario
from .storage import Store, bar_from_record, json_default


def demo_config():
    start = datetime(2026, 9, 21, 1, tzinfo=timezone.utc)
    instruments = [dict(id=i, asset="future", account="futures", exchange="DEMO", product="PAIR", multiplier="10", tick="1", expiry="2026-09-23", delivery="2026-09-23") for i in ("A", "B")]
    frames = []
    for n in range(1, 5):
        # B lacks a bar during the first three opportunities: trigger cleanup.
        ids = ["A"] if n < 4 else ["A", "B"]
        bars = [asdict(Bar(i, start + timedelta(minutes=n-1), start + timedelta(minutes=n), start.date(), D(100 + n), D(110), D(90), D(101 + n), 100)) for i in ids]
        frames.append(dict(time=start + timedelta(minutes=n), open_instruments=["A", "B"], bars=bars))
    cfg = dict(initial_cash="10000", instruments=instruments,
               margin_rules={i: dict(model="future", rate="0.1") for i in ("A", "B")},
               execution=dict(participation="1", fee_per_unit="2"),
               instructions=[dict(at=start, combination=True, legs=[dict(instrument="A", side=1, quantity=2, group="demo-pair"), dict(instrument="B", side=-1, quantity=2, group="demo-pair")])], frames=frames)
    return json.loads(json.dumps(cfg, default=json_default))


def main():
    parser = argparse.ArgumentParser(description="Chinese multi-asset historical backtest")
    commands = parser.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo", help="Run synthetic combination timeout example")
    demo.add_argument("--output", default="outputs/demo.json")
    run = commands.add_parser("run", help="Replay normalized explicit-frame JSON scenario")
    run.add_argument("config")
    run.add_argument("--output", default="outputs/result.json")
    ingest = commands.add_parser("ingest", help="Import normalized JSONL bars")
    ingest.add_argument("file")
    ingest.add_argument("--root", default="data")
    ingest.add_argument("--frequency", choices=["1m", "1d"], required=True)
    meta = commands.add_parser("metadata", help="Import temporal metadata JSONL")
    meta.add_argument("file")
    meta.add_argument("--root", default="data")
    pull = commands.add_parser("update", help="Run configured iFinD SDK request and column normalization")
    pull.add_argument("config")
    pull.add_argument("--root", default="data")
    args = parser.parse_args()
    if args.command in ("demo", "run"):
        cfg = demo_config() if args.command == "demo" else json.loads(Path(args.config).read_text(encoding="utf-8"))
        engine = run_scenario(cfg)
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result(engine, cfg), default=json_default, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Result: {output.resolve()}")
        print(f"PnL: {engine.ledger.snapshot(engine.margin_rules).pnl}; warnings: {len(engine.warnings)}")
    elif args.command == "ingest":
        records = [json.loads(line) for line in Path(args.file).read_text(encoding="utf-8").splitlines() if line.strip()]
        with Store(args.root) as store:
            print(store.publish([bar_from_record(r) for r in records], "normalized-jsonl", args.frequency, parent=store.latest()))
    elif args.command == "metadata":
        records = [json.loads(line) for line in Path(args.file).read_text(encoding="utf-8").splitlines() if line.strip()]
        with Store(args.root) as store:
            for row in records:
                for key in ("valid_from", "valid_to", "known_at"):
                    if row.get(key):
                        row[key] = datetime.fromisoformat(row[key])
                store.put_metadata(**row)
        print(f"Imported {len(records)} metadata records")
    else:
        config = json.loads(Path(args.config).read_text(encoding="utf-8"))
        source = IFinDSource()
        source.login()
        with Store(args.root) as store:
            print(update(store, lambda: source.fetch(config["request"]), configured_normalizer(config["column_mapping"]), config["request"]["frequency"]))


if __name__ == "__main__":
    main()
