from datetime import date
from decimal import Decimal
import json

import pytest

from backtest.cli import demo_config
from backtest.performance import export_performance, scenario_observations, statistics
from backtest.runner import run_scenario


def test_boolean_market_flags_and_weekend_income():
    rows=[dict(day='2024-01-05',equity=100,is_market_day=True),
          dict(day='2024-01-06',equity=105,is_market_day=False),
          dict(day='2024-01-08',equity=110,is_market_day=True)]
    m=statistics(rows,100,date(2024,1,5))
    assert m['volatility']==pytest.approx(.1/(2**.5)*252**.5)
    assert m['annual'][0]['partial_year']


def test_zero_variance_single_day_and_insolvency_are_not_faked():
    m=statistics([dict(day='2024-01-01',equity='100')],100,date(2024,1,1))
    assert m['cagr'] is m['sharpe'] is m['volatility'] is m['calmar'] is None
    m=statistics([dict(day='2024-01-01',equity='110'),dict(day='2024-01-02',equity='-10')],100,date(2024,1,1))
    assert Decimal(m['pnl'])==-110
    assert Decimal(m['max_drawdown'])>1
    assert m['cagr'] is m['sharpe'] is m['volatility'] is None


def test_night_session_uses_trading_date_not_calendar_date():
    cfg={'frames':[dict(time='2024-01-04T21:01:00+08:00',bars=[{'trading_date':'2024-01-05'}])]}
    assert scenario_observations(cfg,[{'equity':'100'}])[0]['day']=='2024-01-05'
    with pytest.raises(ValueError,match='Explicit report trading date'):
        scenario_observations({'frames':[{'time':'2024-01-01T00:00:00+08:00'}]},[{'equity':'100'}])


def test_complete_scenario_emits_standard_artifacts_without_changing_pnl(tmp_path):
    config=demo_config()
    engine=run_scenario(config,output=tmp_path/'demo.json')
    report=tmp_path/'demo'/'standard_report'
    assert (report/'performance.png').read_bytes().startswith(b'\x89PNG')
    m=json.loads((report/'metrics.json').read_text(encoding='utf-8'))
    assert Decimal(m['pnl'])==52==engine.ledger.snapshot(engine.margin_rules).pnl
    assert m['cagr'] is None
    assert (report/'annual.csv').is_file() and (report/'daily.csv').is_file()


def test_report_supports_nonpositive_initial_cash(tmp_path):
    m=export_performance([dict(day='2024-01-01',equity='-1')],0,date(2024,1,1),tmp_path)
    assert m['total_return'] is None and m['max_drawdown'] is None
    assert (tmp_path/'standard_report'/'performance.png').is_file()


def test_duplicate_dates_rejected():
    with pytest.raises(ValueError,match='strictly increasing'):
        statistics([dict(day='2024-01-01',equity=100)]*2,100,date(2024,1,1))
