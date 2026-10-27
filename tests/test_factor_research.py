from copy import deepcopy
from datetime import date, timedelta

import numpy as np
import pytest

from backtest.accounting import Ledger
from backtest.factor_data import make_features, fit_model, predict
from backtest.factor_research import registry, training_rows, sharpe_interval
from backtest.factor_simulation import execute, simulate
from backtest.models import Account, Asset, D, Instrument


def instruments():
    return {code: Instrument(code, Asset.FUTURE, Account.FUTURES, 'CFFEX', 'IF', D(10), D('.1'),
                              expiry=date(2025, month, 28), delivery=date(2025, month, 28))
            for code, month in [('IF2501.CFE', 1), ('IF2502.CFE', 2), ('IF2503.CFE', 3)]}


def data():
    inst = instruments()
    facts = {c: dict(first_trade_date='2025-01-01') for c in inst}
    raw = []
    for i in range(15):
        day = date(2025, 1, 1) + timedelta(days=i)
        for j, code in enumerate(inst):
            price = D(100 + j * 400 + i)
            raw.append(dict(instrument=code, product='IF', trading_date=day,
                            open=price, close=price, high=price + 2, low=price - 1,
                            volume=10, open_interest=D(200 if (j == 0 and i < 7) or (j == 1 and i >= 7) else 100),
                            settlement_price=price, previous_settlement_price=price - 1))
    return raw, facts, inst


def test_main_roll_and_causal_factors():
    raw, facts, inst = data()
    rows, _, _ = make_features(raw, facts, inst, windows=(5,))
    assert rows[6]['main'] == 'IF2501.CFE' and rows[7]['main'] == 'IF2502.CFE'
    expected = np.prod([1 + 1 / (100 + i - 1) for i in range(3, 7)] + [1 + 1 / 506]) - 1
    assert rows[7]['return_5'] == pytest.approx(expected)
    assert rows[7]['return_5'] < .1  # No artificial 400-point main-contract jump.
    assert rows[7]['expiry_days'] == 20
    altered = deepcopy(raw)
    for row in altered:
        if row['trading_date'] > date(2025, 1, 8):
            for key in ('open', 'close', 'high', 'low', 'settlement_price'):
                row[key] *= 3
    again, _, _ = make_features(altered, facts, inst, windows=(5,))
    for a, b in zip(rows[:8], again[:8]):
        for key in ('main', 'near', 'far', 'expiry_days', 'return_5', 'amplitude_5', 'volatility_5'):
            assert a[key] == b[key] or (isinstance(a[key], float) and np.isnan(a[key]) and np.isnan(b[key]))
    assert rows[5]['label_1'] == 0  # Both real legs rise one point; spread is unchanged.


def test_missing_active_quote_resets_main_sequence():
    raw, facts, inst = data()
    raw = [r for r in raw if not (r['instrument'] == 'IF2502.CFE' and r['trading_date'] == date(2025, 1, 8))]
    rows, _, _ = make_features(raw, facts, inst, windows=(5,))
    assert rows[7]['main_complete'] is False
    assert np.isnan(rows[7]['return_5']) and np.isnan(rows[8]['return_5'])


def test_real_contract_label_does_not_roll_to_another_contract():
    raw, facts, inst = data()
    # Force near expiry and verify labels end at that date, or use the explicitly rolled pair.
    inst['IF2501.CFE'] = Instrument('IF2501.CFE', Asset.FUTURE, Account.FUTURES, 'CFFEX', 'IF', D(10), D('.1'),
                                   expiry=date(2025, 1, 10), delivery=date(2025, 1, 10))
    rows, _, _ = make_features(raw, facts, inst, windows=(5,))
    assert np.isnan(rows[4]['label_10'])
    assert rows[8]['near'] == 'IF2502.CFE'
    assert rows[8]['observed_near'] == 'IF2501.CFE'


def test_decimal_fills_slippage_fees_and_settlement_no_double_count():
    inst = instruments()
    ledger = Ledger(inst, 10000)
    execute(ledger, 'IF2501.CFE', 1, 2, 'open', 'IF', date(2025, 1, 2), D(100), D('.00005'), 1)
    assert ledger.fills[0].price == D('100.1')
    assert ledger.fills[0].fee == D('.1001')
    ledger.settle('IF2501.CFE', date(2025, 1, 2), D(101))
    execute(ledger, 'IF2501.CFE', 1, 2, 'close', 'IF', date(2025, 1, 3), D(104), D('.00005'), 1)
    assert ledger.fills[1].price == D('103.9')
    assert ledger.cash == D('10075.7960')
    assert sum(ledger.group_pnl().values()) == ledger.cash - D(10000)


def test_next_open_residual_exit_and_terminal_mark():
    inst = instruments()
    a, b = 'IF2501.CFE', 'IF2502.CFE'
    day1, day2, day3, day4 = [date(2025, 1, i) for i in range(1, 5)]
    model = dict(names=['z'], mean=[0], scale=[1], beta=[1000], intercept=0)
    rows = [dict(product='IF', day=d, execution_day=e, near=a, far=b, main_complete=True,
                 known_notional=D(2200), z=z, risk=1) for d, e, z in [(day1, day2, 1), (day2, day3, 0), (day3, day4, 0)]]
    prices = {(c, d): dict(open=D(100 if c == a else 120), settlement_price=D(101 if c == a else 123))
              for d in (day2, day3, day4) for c in (a, b)}
    prices[a, day3]['open'] = None
    result = simulate(rows, prices, {'IF': [day1, day2, day3, day4]}, inst,
                      {'IF': dict(model=model, threshold=1)}, day2, day4, sizing='one', details=True, fee_rate=0, slip=0)
    assert [f['day'] for f in result['fills']] == [day2, day2, day3, day4]
    assert result['warnings'][0]['reason'] == 'exit_missing_open'
    assert result['daily'][0]['equity'] == D(10000020)
    assert result['daily'][-1]['equity'] == D(10000000)
    assert result['terminal_positions'] == []


def test_train_scaling_purge_and_registry():
    rows = [dict(day=date(2020, 1, 1) + timedelta(days=i), main_complete=True, z=float(i), label_1=float(2 * i),
                 label_end_1=date(2020, 1, 3) + timedelta(days=i)) for i in range(300)]
    train = training_rows(rows, rows[-1]['day'])
    assert max(r['label_end_1'] for r in train) < rows[-10]['day']
    model = fit_model(train, ['z'], .1)
    assert model['mean'][0] == np.mean([r['z'] for r in train])
    assert predict(model, dict(main_complete=True, z=10)) == pytest.approx(20, abs=.2)
    assert fit_model(train[:251], ['z'], 1) is None
    assert len(registry((5, 10, 20, 60))) == 122
    daily = [dict(return_=float(np.sin(i)) / 100) for i in range(100)]
    daily = [{'return': r['return_']} for r in daily]
    assert sharpe_interval(daily, 30, 12) == sharpe_interval(daily, 30, 12)


def test_future_prices_do_not_change_earlier_orders_and_stress_directions():
    inst = instruments()
    raw, _, _ = data()
    prices = {(r['instrument'], r['trading_date']): r for r in raw}
    days = [date(2025, 1, i) for i in range(1, 6)]
    model = dict(names=['z'], mean=[0], scale=[1], beta=[10], intercept=0)
    # Prediction exceeds baseline cost but not a greatly increased hypothetical tariff.
    rows = [dict(product='IF', day=d, execution_day=e, near='IF2501.CFE', far='IF2502.CFE',
                 main_complete=True, known_notional=D(6000), z=1, risk=10) for d, e in zip(days, days[1:])]
    kwargs = dict(features=rows, prices=prices, calendars={'IF': days}, instruments=inst,
                  models={'IF': dict(model=model, threshold=1)}, start=days[1], end=days[-1],
                  sizing='one', details=True, fee_rate='0.00005', slip=1)
    original = simulate(**kwargs)
    changed = deepcopy(prices)
    for (code, day), record in changed.items():
        if day > days[2]:
            record['open'] += 30
            record['settlement_price'] += 30
    altered = simulate(**(kwargs | dict(prices=changed)))
    assert [r for r in original['fills'] if r['day'] <= days[2]] == [r for r in altered['fills'] if r['day'] <= days[2]]
    assert original['daily'][:2] == altered['daily'][:2]
    repeat = simulate(**kwargs)
    assert original == repeat
    stress = simulate(**(kwargs | dict(fee_rate='.01', slip=2, decision_fee_rate='0.00005', decision_slip=1)))
    assert [(r['day'], r['instrument'], r['side'], r['effect']) for r in original['fills']] == [
        (r['day'], r['instrument'], r['side'], r['effect']) for r in stress['fills']]
    assert stress['daily'][-1]['equity'] < original['daily'][-1]['equity']


def test_roll_executes_old_close_and_new_open_without_gap_pnl():
    inst = instruments()
    first, second, third = list(inst)
    days = [date(2025, 1, i) for i in range(1, 5)]
    prices = {(c, day): dict(open=D(100 + j * 100), settlement_price=D(100 + j * 100))
              for j, c in enumerate(inst) for day in days}
    rows = [dict(product='IF', day=d, execution_day=e,
                 near=first if i == 0 else second, far=second if i == 0 else third,
                 main_complete=True, known_notional=D(5000), z=0, risk=10)
            for i, (d, e) in enumerate(zip(days, days[1:]))]
    result = simulate(rows, prices, {'IF': days}, inst, {'IF': dict(carry=True)}, days[1], days[-1],
                      fee_rate=0, slip=0, sizing='one', details=True)
    assert result['daily'][-1]['equity'] == D(10000000)
    assert len(result['fills']) == 6
    assert len(result['terminal_positions']) == 2
    assert sorted((r['instrument'], r['side']) for r in result['terminal_positions']) == [(second, -1), (third, 1)]
