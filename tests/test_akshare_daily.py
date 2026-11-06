from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from backtest.akshare_daily import download, run, update, validate
from backtest.models import D, Instruction
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
                symbols={'600000.SH': '600000'}, execution=dict(participation='1', slippage_ticks=0),
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


def test_complete_output_preserves_execution_and_exports_audit(tmp_path):
    from backtest.runner import result
    cfg = config()
    version = update(cfg, tmp_path/'store', stock_api())
    original, original_cfg = run(cfg, tmp_path/'store', version)
    output = tmp_path/'complete.json'
    exported, exported_cfg = run(cfg, tmp_path/'store', version, output=output)
    assert result(exported, exported_cfg) == result(original, original_cfg)
    payload = json.loads(output.read_text(encoding='utf-8'))
    assert D(payload['final']['pnl']) == D(100)
    assert 'no_automatic_corporate_actions' in payload['assumptions']
    assert 'residual' in payload
    report = tmp_path/'complete/standard_report'
    modes = json.loads((report/'execution_modes.json').read_text(encoding='utf-8'))
    assert modes == payload['execution_modes']
    assert modes == json.loads((report/'metrics.json').read_text(encoding='utf-8'))['execution_modes']
    assert modes['modes'][0]['methods'] == {'next_open': 1}
    assert {p.name for p in (tmp_path/'complete/standard_report').iterdir()} == {
        'performance.png', 'metrics.json', 'annual.csv', 'daily.csv', 'execution_modes.json'}
    with pytest.raises(ValueError, match='Choose output'):
        run(cfg, tmp_path/'store', version, output=output, report_output=tmp_path/'other')


@pytest.mark.parametrize(('model', 'quote', 'first_price', 'first_method'), [
    ('limit_open_then_market', '10.50', '10.50', 'limit_open'),
    ('previous_close_then_open', None, '10.01', 'previous_close'),
])
def test_daily_models_retry_only_the_remainder(tmp_path, model, quote, first_price, first_method):
    cfg = config()
    cfg['execution'] = {}
    cfg['initial_cash'] = '10000'
    cfg['instructions'] = []
    cfg['sessions'].append({key: value.replace('2024-01-03', '2024-01-04')
                            for key, value in cfg['sessions'][-1].items()})
    rows = [dict(日期=f'2024-01-0{day}', 开盘=11 if day == 3 else 10,
                 最高=11 if day == 4 else 12, 最低=9,
                 收盘=10 if day == 2 else 11, 成交量=10) for day in (2, 3, 4)]
    store = tmp_path/'store'
    version = update(cfg, store, stock_api(rows))
    seen = []

    def on_bar(ctx):
        bar = ctx.bars['600000.SH']
        if bar.trading_date.day == 2:
            ctx.target(bar.instrument, 200, price_model=model, limit_price=quote)
        seen.extend(ctx.execution_details_since(len(seen)))

    engine, _ = run(cfg, store, version, on_bar=on_bar)
    assert [(f.trading_date.day, f.time.hour, f.instruction.quantity, f.price)
            for f in engine.ledger.fills] == [(3, 15, 100, D(first_price)), (4, 9, 100, D('10.01'))]
    assert [(r['attempt'], r['method'], r['price_model_source']) for r in seen] == [
        (1, first_method, 'order'), (2, 'market_open', 'order')]
    assert [r['reason'] for r in engine.execution_attempts] == ['PARTIAL_FILL', None]
    assert engine.ledger.cash == D(10000) - 100 * (D(first_price) + D('10.01'))


def test_daily_models_share_capacity_and_cli_reports_effective_rules(tmp_path, monkeypatch):
    from backtest import cli

    cfg = config()
    cfg['execution'] = {}
    instrument = '600000.SH'
    cfg['execution_by_instrument'] = {instrument: {'price_model': 'bar_average', 'participation': '0.2'}}
    cfg['instructions'] = [dict(at='2024-01-02T15:00:00+08:00', legs=[
        dict(instrument=instrument, side=1, quantity=200),
        dict(instrument=instrument, side=1, quantity=100, price_model='next_open'),
    ])]
    rows = [dict(日期=f'2024-01-0{day}', 开盘=10, 最高='10.005', 最低=9,
                 收盘=10, 成交量=10) for day in (2, 3)]
    store = tmp_path/'store'
    version = update(cfg, store, stock_api(rows))
    config_path = tmp_path/'config.json'
    config_path.write_text(json.dumps(cfg), encoding='utf-8')
    output = tmp_path/'run.json'
    monkeypatch.setattr('sys.argv', ['backtest', 'run', str(config_path), '--root', str(store),
                                    '--version', version, '--output', str(output)])
    cli.main()
    result = json.loads(output.read_text(encoding='utf-8'))
    # Open fills before the average despite being submitted second. Only 100 shares remain.
    assert [(D(f['price']), f['instruction']['quantity']) for f in result['fills']] == [
        (D('10.005'), 100), (D('9.75125'), 100)]
    assert D(result['final']['pnl']) == D('24.375')
    assert result['execution_modes']['instrument_overrides'][instrument]['price_model'] == 'bar_average'
    assert [(r['method'], r['price_model_source']) for r in result['execution_details']] == [
        ('next_open', 'order'), ('bar_ohlc4', 'config')]
    assert (tmp_path/'run/standard_report/execution_modes.json').is_file()


def test_replay_is_read_only_shareable_and_records_strategy(tmp_path):
    import subprocess
    import sys
    from backtest.runner import result
    cfg = config()
    version = update(cfg, tmp_path, stock_api())

    def on_bar(ctx):
        pass

    on_bar.strategy_params = dict(window=5)
    code = ("import sys,time;from backtest.storage import Store;"
            "s=Store(sys.argv[1],read_only=True);print('open',flush=True);time.sleep(4)")
    holder = subprocess.Popen([sys.executable, '-c', code, str(tmp_path)], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == 'open'  # another process already holds the snapshot
        engine, replay_cfg = run(cfg, tmp_path, version, on_bar)
    finally:
        holder.communicate(timeout=30)
    provenance = result(engine, replay_cfg)['provenance']
    assert provenance['strategy']['params'] == dict(window=5)
    explicit, explicit_cfg = run(cfg, tmp_path, version, strategy=dict(name='instruction-flow', params=dict(file='x')))
    assert result(explicit, explicit_cfg)['provenance']['strategy']['name'] == 'instruction-flow'
    # Replay never creates a store as a side effect.
    with pytest.raises(FileNotFoundError):
        run(cfg, tmp_path / 'absent', version)
    assert not (tmp_path / 'absent').exists()
