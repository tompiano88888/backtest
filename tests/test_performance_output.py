import json
from decimal import Decimal
from datetime import date

from backtest import cli
from backtest.performance import export_performance, statistics
from backtest.runner import run_scenario, result


def test_cli_adds_report_without_changing_result_or_support_scope(tmp_path, monkeypatch):
    cfg=cli.demo_config()
    engine=run_scenario(cfg)
    before=result(engine,cfg)
    config=tmp_path/'config.json'
    config.write_text('{}')
    output=tmp_path/'result.json'
    monkeypatch.setattr(cli,'run',lambda *args:(engine,cfg))
    monkeypatch.setattr('sys.argv',['backtest','run',str(config),'--version','fixed-test','--output',str(output)])
    cli.main()
    after=result(engine,cfg)
    assert before==after
    folder=tmp_path/'result'/'standard_report'
    assert (folder/'performance.png').read_bytes().startswith(b'\x89PNG')
    metrics=json.loads((folder/'metrics.json').read_text(encoding='utf-8'))
    assert Decimal(metrics['pnl'])==52
    assert metrics['cagr'] is None
    assert (folder/'annual.csv').is_file() and (folder/'daily.csv').is_file()


def test_nonpositive_equity_is_reported_without_fabricating_ratios(tmp_path):
    metrics=export_performance([dict(day='2024-01-01',equity=-1)],0,date(2024,1,1),tmp_path)
    assert metrics['pnl']=='-1'
    assert metrics['cagr'] is metrics['sharpe'] is metrics['max_drawdown'] is None


def test_boolean_calendar_flags_keep_nonmarket_income():
    rows=[dict(day='2024-01-05',equity=100,is_market_day=True),
          dict(day='2024-01-06',equity=110,is_market_day=False),
          dict(day='2024-01-08',equity=121,is_market_day=True)]
    m=statistics(rows,100,date(2024,1,5))
    assert m['sharpe'] is not None
    assert Decimal(m['pnl'])==21
