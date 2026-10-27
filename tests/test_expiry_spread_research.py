from datetime import date, timedelta

import numpy as np

from backtest.expiry_spread_research import HoldingPolicy, build_profile, direction
from backtest.factor_simulation import simulate
from backtest.models import Account, Asset, D, Instrument


def fixture():
    inst = {c: Instrument(c, Asset.FUTURE, Account.FUTURES, 'CFFEX', 'IF', D(10), D('.1'),
                           expiry=date(2025, month, 5), delivery=date(2025, month, 5))
            for c, month in [('IF2501.CFE', 1), ('IF2502.CFE', 2)]}
    days = [date(2025, 1, i) for i in range(1, 7)]
    prices = {(c, d): dict(open=D(100 if c == 'IF2501.CFE' else 110),
                          close=D(100 if c == 'IF2501.CFE' else 110),
                          settlement_price=D(100 if c == 'IF2501.CFE' else 110)) for d in days for c in inst}
    rows = [dict(product='IF', day=d, execution_day=e, near='IF2501.CFE', far='IF2502.CFE',
                 expiry_days=(date(2025, 1, 5) - d).days, relative_spread=ratio,
                 known_notional=D(2100), risk=1) for d, e, ratio in zip(days, days[1:], [-.1, 0, .1, .1, -.1])]
    profile = {i: dict(low=-.05, high=.05, count=50) for i in range(6)}
    return inst, days, prices, rows, profile


def test_neutral_holds_reverse_then_expiry_closes_pair():
    inst, days, prices, rows, profile = fixture()
    result = simulate(rows, prices, {'IF': days}, inst, {'IF': {}}, days[1], days[-1], sizing='one',
                      fee_rate=0, slip=0, details=True, target_policy=HoldingPolicy({'IF': profile}, inst))
    fills = result['fills']
    assert len(fills) == 7  # two entry; four reversal; far close; near cash delivery is not a fill
    assert not any(r['day'] == days[2] for r in fills)
    assert sum(r['day'] == days[3] for r in fills) == 4
    assert fills[-1]['day'] == days[4] and fills[-1]['instrument'] == 'IF2502.CFE'
    assert result['terminal_positions'] == []
    assert result['daily'][-1]['equity'] == D(10000000)
    assert any(r['kind'] == 'delivery' for r in result['entries'])
    assert result['decisions'][1]['reason'] == 'hold_fixed_quantity'


def test_expiry_missing_far_close_persists_until_next_open():
    inst, days, prices, rows, profile = fixture()
    prices['IF2502.CFE', days[4]]['close'] = None
    result = simulate(rows, prices, {'IF': days}, inst, {'IF': {}}, days[1], days[-1], sizing='one',
                      fee_rate=0, slip=0, details=True, target_policy=HoldingPolicy({'IF': profile}, inst))
    assert any(w['reason'] == 'expiry_far_missing_close' for w in result['warnings'])
    assert result['fills'][-1]['day'] == days[5]
    assert result['decisions'][-1]['reason'] == 'retry_residual_exit'
    assert result['terminal_positions'] == []


def test_historical_distribution_excludes_future_and_unexpired_cycles():
    inst, _, _, _, _ = fixture()
    rows = [dict(day=date(2025, 1, 1) + timedelta(days=i), near='IF2501.CFE',
                 expiry_days=4 - i, relative_spread=i / 100) for i in range(5)]
    rows += [dict(day=date(2025, 1, 4), near='IF2502.CFE', expiry_days=32, relative_spread=1000)]
    profile = build_profile(rows, inst, date(2025, 1, 5), 5, .2, 0, min_samples=2, min_contracts=1)
    assert profile[2]['count'] == 5
    assert profile[2]['low'] == .008 and profile[2]['high'] == .032
    again = build_profile(rows + [dict(day=date(2026, 1, 1), near='IF2501.CFE', expiry_days=2, relative_spread=-1000)],
                          inst, date(2025, 1, 5), 5, .2, 0, min_samples=2, min_contracts=1)
    assert profile == again
    assert direction(dict(expiry_days=2, relative_spread=.001), profile) == 1
    assert direction(dict(expiry_days=2, relative_spread=.04), profile) == -1
    assert direction(dict(expiry_days=2, relative_spread=.02), profile) == 0


def test_entry_size_stays_fixed_when_risk_changes():
    inst, days, prices, rows, profile = fixture()
    rows[1]['risk'] = 1000000
    rows[2]['relative_spread'] = 0
    rows[3]['relative_spread'] = 0
    rows[0]['risk'] = 1000
    result = simulate(rows, prices, {'IF': days}, inst, {'IF': {}}, days[1], days[3], details=True,
                      fee_rate=0, slip=0, target_policy=HoldingPolicy({'IF': profile}, inst))
    assert len(result['fills']) == 2
    assert result['terminal_positions'][0]['quantity'] == result['fills'][0]['quantity']


def test_mean_std_exits_at_mean_without_same_bar_reversal():
    for sign in (1, -1):
        inst, days, prices, rows, profile = fixture()
        for band in profile.values():
            band.update(mean=0, std=.05)
        for row, value in zip(rows, [-.1, -.02, .1, .1, -.1]):
            row['relative_spread'] = value * sign
        result = simulate(rows, prices, {'IF': days}, inst, {'IF': {}}, days[1], days[3], sizing='one',
                          fee_rate=0, slip=0, details=True,
                          target_policy=HoldingPolicy({'IF': profile}, inst, 'mean_std'))
        assert len(result['fills']) == 4
        assert result['decisions'][1]['reason'] == 'hold_until_mean'
        assert result['decisions'][2]['reason'] == 'exit_at_mean'
        assert all(f['effect'] == 'close' for f in result['fills'] if f['day'] == days[3])
        assert result['terminal_positions'] == []


def test_mean_std_entry_threshold_is_strict_and_exit_is_inclusive():
    inst, days, prices, rows, profile = fixture()
    for band in profile.values():
        band.update(mean=0, std=.05)
    assert direction(dict(expiry_days=2, relative_spread=.05), profile) == 0
    assert direction(dict(expiry_days=2, relative_spread=-.05), profile) == 0
    rows[1]['relative_spread'] = 0
    result = simulate(rows, prices, {'IF': days}, inst, {'IF': {}}, days[1], days[2], sizing='one',
                      fee_rate=0, slip=0, details=True,
                      target_policy=HoldingPolicy({'IF': profile}, inst, 'mean_std'))
    assert result['decisions'][-1]['reason'] == 'exit_at_mean'
    assert len(result['fills']) == 4
