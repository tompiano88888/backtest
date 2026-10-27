from datetime import date
from backtest.expiry_spread_research import HoldingPolicy
from backtest.factor_simulation import simulate
from backtest.full_history_spread import ExpandingPolicy
from test_expiry_spread_research import fixture


def test_reverse_entry_preserves_original_mean_exit_and_one_lot():
    inst, days, prices, rows, profile = fixture()
    for band in profile.values():
        band.update(mean=0, std=.05)
    rows[1]['relative_spread'] = -.02
    rows[2]['relative_spread'] = 0
    result = simulate(rows, prices, {'IF': days}, inst, {'IF': {}}, days[1], days[3],
                      sizing='one', fee_rate=0, slip=0, details=True,
                      target_policy=HoldingPolicy({'IF': profile}, inst, 'mean_std', trade_sign=-1))
    assert len(result['fills']) == 4
    assert all(f['quantity'] == 1 for f in result['fills'])
    assert next(f for f in result['fills'] if f['instrument'] == 'IF2502.CFE')['side'] == -1
    assert result['decisions'][1]['reason'] == 'hold_until_mean'
    assert result['decisions'][2]['reason'] == 'exit_at_mean'
    assert result['terminal_positions'] == []


def test_expanding_profile_excludes_current_expiry_and_future_rows():
    inst, days, _, rows, _ = fixture()
    policy = ExpandingPolicy(rows, inst, min_samples=2, min_contracts=1)
    class EmptyLedger:
        lots = []
    row = rows[0] | dict(day=date(2025, 1, 5), expiry_days=0)
    policy('IF', date(2025, 1, 6), row, EmptyLedger(), 10000, 'one', .1)
    assert policy.profiles['IF'] == {}
    row = row | dict(day=date(2025, 1, 6))
    policy('IF', date(2025, 1, 7), row, EmptyLedger(), 10000, 'one', .1)
    original = policy.profiles['IF']
    assert original[0]['count'] == 5
    changed = rows + [rows[0] | dict(day=date(2026, 1, 1), relative_spread=10000)]
    other = ExpandingPolicy(changed, inst, min_samples=2, min_contracts=1)
    other('IF', date(2025, 1, 7), row, EmptyLedger(), 10000, 'one', .1)
    assert other.profiles['IF'] == original
