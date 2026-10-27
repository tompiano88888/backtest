from datetime import date
from backtest.arbitrage_repo_returns import repo_rate,apply_repo
from backtest.models import D


def row(day,pnl):
    return dict(day=day,cumulative_future_tick_pnl=str(pnl))


def test_repo_year_boundaries_2020_remains_two_percent():
    assert repo_rate(date(2014,12,31))==D('.03')
    assert repo_rate(date(2015,1,1))==D('.02')
    assert repo_rate(date(2020,12,31))==D('.02')
    assert repo_rate(date(2021,1,1))==D('.015')


def test_flat_weekends_accrue_current_cash_and_switch_rate_at_year_boundary():
    summary,yearly,daily=apply_repo(1000,[row('2020-12-31',0),row('2021-01-04',10)],
                                  [dict(entry_day='2021-01-04',exit_day='2021-01-04')])
    assert summary['repo_days']==4
    assert daily[0]['repo_rate']==D('.02') and daily[1]['repo_rate']==D('.015')
    assert daily[2]['repo_eligible'] and not daily[2]['is_observed_trading_day']
    assert daily[-1]['repo_interest']==0
    assert daily[1]['interest_base']==D(1000)+daily[0]['repo_interest']
    assert summary['final_equity']==D(1010)+summary['repo_interest']
    assert sum(r['repo_interest'] for r in yearly)==summary['repo_interest']
    assert yearly[1]['arbitrage_pnl']==D(10)


def test_holding_weekend_and_entry_exit_dates_do_not_accrue_unused_cash():
    summary,_,daily=apply_repo(1000,[row('2025-01-03',0),row('2025-01-06',10),row('2025-01-07',10)],
                             [dict(entry_day='2025-01-03',exit_day='2025-01-06')])
    assert [r['repo_interest'] for r in daily[:4]]==[D(0)]*4
    assert summary['repo_days']==1
    assert daily[-1]['interest_base']==D(1010)


def test_nonpositive_cash_does_not_generate_negative_repo_interest():
    summary,_,daily=apply_repo(100,[row('2025-01-03',-150)],[])
    assert summary['repo_interest']==0
    assert not daily[0]['repo_eligible']
    assert summary['combined_cagr'] is None
