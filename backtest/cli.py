from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path

from .models import Bar, D
from .pipeline import IFinDSource, prepare_ifind_update, update_ifind
from .ifind import IFinDError, credentials_present, load_sdk, request_function
from .runner import result, run_scenario, save_result
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
    pull.add_argument("--validate-only", action="store_true", help="Validate request and calendar locally; no login/download")
    check = commands.add_parser("ifind-check", help="Check official SDK and credential presence without exposing secrets")
    check.add_argument("--login", action="store_true", help="Also perform a real login/logout")
    query = commands.add_parser("ifind-query", help="Query verified SDK functions into a local raw JSON file")
    query.add_argument("config", help="JSON containing a request object")
    query.add_argument("--output", default="outputs/ifind-query.json")
    args = parser.parse_args()
    if args.command in ("demo", "run"):
        cfg = demo_config() if args.command == "demo" else json.loads(Path(args.config).read_text(encoding="utf-8"))
        engine = run_scenario(cfg)
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        save_result(engine, cfg, output)
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
        try:
            if args.command == "ifind-check":
                sdk = load_sdk()
                info = {"sdk_available": True, "credentials_present": credentials_present(),
                        "functions": [name for name in ("THS_HQ", "THS_HF", "THS_BD", "THS_DS", "THS_DP", "THS_DateQuery") if hasattr(sdk, name)]}
                if args.login:
                    with IFinDSource(sdk) as source:
                        info["login"] = "success"
                print(json.dumps(info, ensure_ascii=False))
                return
            config_path = Path(args.config)
            config = json.loads(config_path.read_text(encoding="utf-8-sig"))
            if args.command == "update":
                requests, normalize = prepare_ifind_update(config, config_path.parent)
                if args.validate_only:
                    print(json.dumps({"valid": True, "batches": len(requests), "frequency": normalize.frequency}))
                    return
                with IFinDSource(retries=config.get("retries", 3), retry_codes=config.get("retry_codes", [])) as source:
                    with Store(args.root) as store:
                        print(update_ifind(store, source, requests, normalize))
            else:
                request_function(config["request"])
                with IFinDSource(retries=config.get("retries", 3), retry_codes=config.get("retry_codes", [])) as source:
                    records = source.query(config["request"])
                output = Path(args.output)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(json.dumps({"records": records}, default=json_default, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"Saved {len(records)} records to {output.resolve()}")
        except IFinDError as error:
            parser.exit(2, f"{error}\n")
        except (KeyError, ValueError, TypeError, OSError):
            parser.exit(2, "Invalid/unreadable iFinD task configuration; see docs/IFIND.md\n")


if __name__ == "__main__":
    main()
