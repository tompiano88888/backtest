"""Synthetic API smoke example, not a trading signal or a fitted strategy."""
import argparse

from backtest.cli import demo_config
from backtest.runner import run_scenario


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default='outputs/external_callback/result.json')
    args = parser.parse_args()
    config = demo_config()
    config['instructions'] = []
    submitted = False

    def callback(ctx):
        nonlocal submitted
        # One fixed synthetic target demonstrates submission after a completed bar.
        # Replace this callback in your external project; do not change the engine.
        if not submitted:
            ctx.target('A', 1)
            submitted = True

    engine = run_scenario(config, callback, output=args.output,
                          strategy={'name': 'synthetic-interface-example', 'params': {'quantity': 1}})
    print(f'Result: {args.output}; PnL: {engine.ledger.snapshot(engine.margin_rules).pnl}')


if __name__ == '__main__':
    main()
