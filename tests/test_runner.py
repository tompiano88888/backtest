from backtest.cli import demo_config
from backtest.runner import result, run_scenario


def test_demo_reproducible_and_reconciled():
    config = demo_config()
    first = result(run_scenario(config), config)
    second = result(run_scenario(config), config)
    assert first == second
    assert first["final"]["cash"] == 10052
    assert first["final"]["pnl"] == 52
    assert sum(first["group_pnl"].values()) == 52
    assert len(first["warnings"]) == 4
    assert not first["positions"]
