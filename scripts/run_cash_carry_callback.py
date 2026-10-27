"""Export the recorded baseline schedule and replay normalized Engine frames."""
import argparse
import json
from pathlib import Path

from backtest.cash_carry_callback import BASELINE, export_parameters, run
from backtest.storage import json_default


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--export", action="store_true")
    parser.add_argument("--source", default=BASELINE)
    parser.add_argument("--parameters", default="configs/cash_carry_best.json")
    parser.add_argument("--scenario")
    parser.add_argument("--output", default="outputs/cash_carry_callback/result.json")
    args = parser.parse_args()
    if args.export:
        config = export_parameters(args.source, args.parameters)
        print(f"Exported {len(config['parameters'])} daily parameter rows: {args.parameters}")
    if args.scenario:
        config = json.loads(Path(args.scenario).read_text(encoding="utf-8-sig"))
        output = run(config, args.parameters)
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(output, ensure_ascii=False, indent=2, default=json_default), encoding="utf-8")
        from backtest.performance import export_scenario_report
        export_scenario_report(output, config, path.parent / path.stem)
        print(f"Standard result: {path.resolve()}; PnL: {output['final']['pnl']}")
    elif not args.export:
        parser.error("Supply --export and/or --scenario")


if __name__ == "__main__":
    main()
