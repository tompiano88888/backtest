from datetime import date
from decimal import Decimal as D

import pytest

from backtest.futures_spread import pair_rows, statistics, read_deliveries


def row(code, day, close='100', settlement='101'):
    return dict(instrument=code, product=code[:2], trading_date=date.fromisoformat(day),
                close=D(close) if close else None,
                settlement_price=D(settlement) if settlement else None)


def test_delivery_day_roll_missing_leg_and_cross_year():
    data = [row('IF2612.CFE', '2026-12-18'), row('IF2701.CFE', '2026-12-18', '103'),
            row('IF2701.CFE', '2026-12-21'), row('IF2702.CFE', '2026-12-21', '98'),
            row('IF2701.CFE', '2027-01-04'), row('IF2703.CFE', '2027-01-04', '120')]
    results = pair_rows(data, {'IF2612.CFE': date(2026, 12, 18), 'IF2701.CFE': date(2027, 1, 15)})
    assert [(r['near'], r['far'], r['spread']) for r in results] == [
        ('IF2612.CFE', 'IF2701.CFE', D(3)), ('IF2701.CFE', 'IF2702.CFE', D(-2)),
        ('IF2701.CFE', 'IF2702.CFE', None)]
    assert [r['roll'] for r in results] == [False, True, False]
    assert results[0]['relative_spread'] == D('.03')
    assert results[2]['issues'] == 'missing_far_contract'


def test_missing_price_not_filled_and_duplicates_rejected():
    data = [row('IH2610.CFE', '2026-10-08'), row('IH2611.CFE', '2026-10-08', None, '104')]
    deliveries = {'IH2610.CFE': date(2026, 10, 16)}
    result = pair_rows(data, deliveries)[0]
    assert result['spread'] is None and result['settlement_spread'] == D(3)
    assert result['issues'] == 'missing_far_close'
    with pytest.raises(ValueError, match='Duplicate'):
        pair_rows(data + data[:1], deliveries)
    with pytest.raises(ValueError, match='Missing verified'):
        pair_rows(data, {})


def test_statistics_hand_calculation():
    rows = [dict(trading_date=date(2026, 1, i + 1), spread=v)
            for i, v in enumerate([D(-2), D(0), D(2), None])]
    result = statistics(rows, 'spread')
    assert result['count'] == 3 and result['missing_days'] == 1
    assert result['mean'] == result['median'] == 0
    assert result['std'] == 2
    assert result['p25'] == -1 and result['p95'] == D('1.8')
    assert result['positive_ratio'] == D(1) / 3
    assert result['min_dates'] == '2026-01-01'
    assert statistics(rows[-1:], 'spread')['mean'] is None
    assert statistics(rows[:1], 'spread')['std'] is None


def test_delivery_provenance_required(tmp_path):
    path = tmp_path / 'deliveries.csv'
    path.write_text('instrument,delivery,source\nIF2610.CFE,2026-10-16,\n', encoding='utf-8')
    with pytest.raises(ValueError, match='missing delivery source'):
        read_deliveries(path)


def test_repeated_analysis_exports_and_preserves_inputs(tmp_path, monkeypatch):
    import backtest.futures_spread as module
    data = [row('IC2610.CFE', '2026-10-08'), row('IC2611.CFE', '2026-10-08', '105')]
    monkeypatch.setattr(module, 'load_snapshot', lambda root, version: ('fixed-version', data))
    monkeypatch.setattr(module, 'snapshot_deliveries', lambda *args: ({}, None, {}))
    monkeypatch.setattr(module, 'plot', lambda rows, output: None)
    output = tmp_path / 'output'
    blocked = module.analyze(tmp_path, output)
    assert blocked['status'] == 'blocked'
    assert blocked['missing_deliveries'] == ['IC2610.CFE']
    deliveries = tmp_path / 'verified.csv'
    deliveries.write_text('instrument,delivery,source\nIC2610.CFE,2026-10-16,test fixture\n', encoding='utf-8')
    original = deliveries.read_bytes()
    assert module.analyze(tmp_path, output, deliveries)['status'] == 'complete'
    first = (output / 'daily.csv').read_bytes(), (output / 'statistics.csv').read_bytes()
    module.analyze(tmp_path, output, deliveries, 'fixed-version')
    assert first == ((output / 'daily.csv').read_bytes(), (output / 'statistics.csv').read_bytes())
    assert deliveries.read_bytes() == original


def test_launch_month_uses_first_listed_contract():
    data = [row('IF1005.CFE', '2010-04-16'), row('IF1006.CFE', '2010-04-16', '104')]
    result = pair_rows(data, {'IF1005.CFE': date(2010, 5, 21)}, {'IF': date(2010, 5, 1)})[0]
    assert result['near'] == 'IF1005.CFE' and result['spread'] == 4
