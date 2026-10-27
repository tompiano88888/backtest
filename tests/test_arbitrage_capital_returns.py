from datetime import date
from backtest.arbitrage_capital_returns import capacity_rows,calculate,annualized
from backtest.models import D


def test_capital_overlap_close_before_open_and_delivery_release():
    cycles=[dict(product='IF',entry_day='2024-12-31',exit_day='2025-01-02',exit_reason='rate_below_minus2',entry_index='100',multiplier='2'),
            dict(product='IF',entry_day='2025-01-02',exit_day='2025-01-03',exit_reason='delivery',entry_index='200',multiplier='2')]
    daily=[dict(product='IF',day=d,gross_pnl=str(g),future_tick_pnl=str(n)) for d,g,n in
           [('2024-12-31',0,0),('2025-01-02',12,10),('2025-01-03',33,30)]]
    rows=capacity_rows(cycles,daily)
    assert [r['capital_daily_peak'] for r in rows]==[D(300),D(600),D(600)]
    assert rows[-1]['capital_end_day']==0
    summaries,yearly=calculate(rows)
    assert summaries[0]['fixed_initial_capital']==D(600)
    assert summaries[0]['future_tick_pnl']==D(30)
    assert yearly[-1]['actual_return_future_tick_pnl']==D('.05')
    assert sum(r['pnl_future_tick_pnl'] for r in yearly)==D(30)


def test_annual_return_uses_opening_equity_and_zero_occupancy_efficiency_is_empty():
    rows=[dict(product='IF',day=date(y,1,2),capital_daily_peak=D(c),entries=0,
               daily_gross_pnl=D(p),daily_future_tick_pnl=D(p),cumulative_gross_pnl=D(total),cumulative_future_tick_pnl=D(total))
          for y,c,p,total in [(2023,100,10,10),(2024,100,22,32),(2025,0,0,32)]]
    summaries,yearly=calculate(rows)
    assert yearly[0]['actual_return_future_tick_pnl']==D('.1')
    assert yearly[1]['actual_return_future_tick_pnl']==D('.2')
    assert yearly[2]['actual_return_future_tick_pnl']==0
    assert yearly[2]['capital_efficiency_annualized_future_tick_pnl'] is None
    assert summaries[0]['total_return_future_tick_pnl']==D('.32')


def test_calendar_cagr_and_nonpositive_equity():
    assert abs(annualized(D(100),D(110),365)-D('.1'))<D('1e-24')
    assert annualized(D(100),D(0),365) is None
