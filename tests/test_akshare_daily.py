from copy import deepcopy
from types import SimpleNamespace

import pytest

from backtest.akshare_daily import download, run, update, validate
from backtest.models import D
from backtest.storage import Store


class Table:
    def __init__(self, rows):
        self.rows = rows
        self.empty = not rows

    def to_dict(self, orient):
        assert orient == 'records'
        return self.rows


def config():
    return dict(initial_cash='10000', frequency='1d',
                instruments=[dict(id='600000.SH', asset='equity', account='security', exchange='SSE', product='stock', lot=100, sell_delay_days=1)],
                symbols={'600000.SH': '600000'}, execution=dict(participation='1'),
                instructions=[dict(at='2024-01-02T15:00:00+08:00', instrument='600000.SH', quantity=100)],
                sessions=[dict(instrument='600000.SH', trading_date=f'2024-01-0{day}', start=f'2024-01-0{day}T09:30:00+08:00', end=f'2024-01-0{day}T15:00:00+08:00', upper_limit='20', lower_limit='1') for day in (2, 3)])


def stock_api(rows=None):
    def query(**kwargs):
        assert kwargs['adjust'] == '' and kwargs['period'] == 'daily'
        return Table(rows if rows is not None else [dict(日期=f'2024-01-0{day}', 开盘=10, 最高=12, 最低=9, 收盘=11, 成交量=10) for day in (2, 3)])
    return SimpleNamespace(stock_zh_a_hist=query)


def test_equity_snapshot_next_open_and_units(tmp_path):
    cfg = config()
    bars = download(cfg, stock_api())
    assert bars[0].volume == 1000  # AKShare hands -> shares
    version = update(cfg, tmp_path, stock_api())
    engine, scenario = run(cfg, tmp_path, version)
    assert scenario['data_version'] == version
    assert len(engine.ledger.fills) == 1
    fill = engine.ledger.fills[0]
    assert fill.time == bars[1].start
    assert fill.price == D(10)
    assert engine.ledger.snapshot(engine.margin_rules).pnl == D(100)
    # A second update does not modify the old immutable snapshot.
    update(cfg, tmp_path, stock_api())
    assert run(cfg, tmp_path, version)[0].ledger.fills == engine.ledger.fills


def test_future_settlement_cash_not_close(tmp_path):
    cfg = config()
    cfg['instruments'] = [dict(id='IF2401', asset='future', account='futures', exchange='CFFEX', product='IF', multiplier='10', tick='1', expiry='2024-01-19', delivery='2024-01-19')]
    cfg['symbols'] = {'IF2401': 'IF2401'}
    cfg['margin_rules'] = {'IF2401': dict(model='future', rate='0.1')}
    cfg['instructions'] = [dict(at='2024-01-01T15:00:00+08:00', instrument='IF2401', quantity=1)]
    for s in cfg['sessions']:
        s['instrument'] = 'IF2401'
        s['upper_limit'], s['lower_limit'] = '200', '1'
    rows = [dict(symbol='IF2401', date=f'2024010{day}', open=100, high=110, low=90, close=105, volume=10, settle=102+day) for day in (2, 3)]
    api = SimpleNamespace(get_futures_daily=lambda **kwargs: Table(rows))
    assert download(cfg, api)[0].volume == 10  # futures hands unchanged
    version = update(cfg, tmp_path, api)
    engine, _ = run(cfg, tmp_path, version)
    assert len(engine.ledger.fills) == 1
    assert engine.ledger.cash == D(10050)
    assert engine.ledger.lots[0].basis == D(105)
    rows[0]['settle'] = None
    with pytest.raises((ValueError, TypeError)):
        update(cfg, tmp_path, api)
    with Store(tmp_path) as store:
        assert store.latest() == version


@pytest.mark.parametrize('change', ['minute', 'option', 'continuous', 't0', 'limits', 'sessions'])
def test_reject_unsupported_or_incomplete_config(change):
    cfg = deepcopy(config())
    if change == 'minute':
        cfg['frequency'] = '1m'
    elif change == 'option':
        cfg['instruments'][0]['asset'] = 'fund'
    elif change == 'continuous':
        cfg['symbols']['600000.SH'] = 'IF0'
    elif change == 't0':
        cfg['instruments'][0]['sell_delay_days'] = 0
    elif change == 'limits':
        del cfg['sessions'][0]['upper_limit']
    else:
        cfg['sessions'].append(cfg['sessions'][0])
    with pytest.raises((ValueError, KeyError)):
        download(cfg, stock_api())


def test_missing_or_duplicate_rows_never_publish(tmp_path):
    cfg = config()
    row = dict(日期='2024-01-02', 开盘=10, 最高=12, 最低=9, 收盘=11, 成交量=10)
    for rows in ([row], [row, row]):
        with pytest.raises(ValueError):
            update(cfg, tmp_path, stock_api(rows))
    assert not (tmp_path / 'metadata.duckdb').exists()


def test_non_akshare_snapshot_rejected(tmp_path):
    cfg = config()
    with Store(tmp_path) as store:
        version = store.publish(download(cfg, stock_api()), 'ifind', '1d')
    with pytest.raises(ValueError, match='AKShare snapshot'):
        run(cfg, tmp_path, version)


def test_standard_report_preserves_snapshot_execution(tmp_path):
    import json
    from backtest.runner import result
    cfg = config()
    version = update(cfg, tmp_path / 'store', stock_api())
    original, original_cfg = run(cfg, tmp_path / 'store', version)
    reported, reported_cfg = run(cfg, tmp_path / 'store', version,
                                  report_output=tmp_path / 'reported')
    assert reported_cfg == original_cfg
    assert result(reported, reported_cfg) == result(original, original_cfg)
    report = tmp_path / 'reported' / 'standard_report'
    assert D(json.loads((report / 'metrics.json').read_text(encoding='utf-8'))['pnl']) == D(100)
    assert (report / 'performance.png').read_bytes().startswith(b'\x89PNG')
