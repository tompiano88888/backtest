from datetime import date
from backtest.models import D,Instrument,Asset,Account
from backtest.etf_candidate_scoring import ETFCandidateMarket


def market():
    days=[date(2025,1,i) for i in (2,3,6,7,8)]
    code='IF2501.CFE'
    instruments={code:Instrument(code,Asset.FUTURE,Account.FUTURES,'CFFEX','IF',D(10),D('.1'),1,
                                expiry=days[-1],delivery=days[-1])}
    prices={(code,d):dict(open=D(100),close=D(100),settlement_price=D(100)) for d in days}
    spot={('000300.SH',d):dict(open=D(100),close=D(100)) for d in days}
    for d in days:spot['510300.SH',d]=dict(open=D(3),close=D('3.1'),ohlcv_complete=True)
    spot['510300.SH',days[3]]['open']=D('3.4')
    rates=dict(zip(days,[D('.03'),D('.01'),D('-.03'),D('-.03'),None]))
    event=dict(instrument='510300.SH',announcement_date=date(2025,1,1),record_date=days[1],
               exchange_ex_date=days[2],payment_date=days[4],cash_per_unit=D('.1'))
    result=ETFCandidateMarket('IF',days,{('IF',d):code for d in days},prices,spot,instruments,
                             lambda p,c,d:rates[d],dividend_events=[event])
    return result,days


def test_actual_etf_score_matches_ledger_trace_and_manual_dividend_profit():
    m,days=market()
    fast=m.evaluate(days[0],4,D('.02'),D(0),D(1500))
    slow,fund=m.evaluate(days[0],4,D('.02'),D(0),D(1500),trace=True)
    assert fast==slow
    assert fast['arbitrage_pnl']==D(148)  # ETF120, future-2, dividend30
    assert fast['cycles']==1 and not fast['open_at_end']
    assert fast['estimated_repo_interest']==sum((r['repo_interest'] for r in fund.daily),D(0))
    assert fund.dividends.total==D(30)


def test_window_end_marks_open_position_and_receivable_without_future_payment():
    m,days=market()
    fast=m.evaluate(days[0],2,D('.02'),D(0),D(1500))
    slow,fund=m.evaluate(days[0],2,D('.02'),D(0),D(1500),trace=True)
    assert fast==slow and fast['open_at_end']
    assert fast['arbitrage_pnl']==D(59)  # ETF30 + accrued dividend30 - entry tick1
    assert fund.daily[-1]['dividend_receivable']==D(30)
    assert not any(e['kind']=='etf_dividend' for e in fund.ledger.entries)
    m.spot['510300.SH',days[-1]]['close']=D(1000)
    assert m.evaluate(days[0],2,D('.02'),D(0),D(1500),trace=True)[0]==fast


def test_single_candidate_does_not_repeat_lots_and_fast_matches_funding_cancel():
    m,days=market()
    assert m.evaluate(days[0],4,D('.02'),D(0),D(10000),trace=True)[1].completed[0]['etf_quantity']==300
    m.prices['IF2501.CFE',days[1]].update(close=D(180),settlement_price=D(180))
    fast=m.evaluate(days[0],4,D('.02'),D(0),D(1300))
    slow,fund=m.evaluate(days[0],4,D('.02'),D(0),D(1300),trace=True)
    assert fast==slow and fund.cancelled_openings


def test_fast_matches_reference_for_every_grid_pair():
    from backtest.rolling_arbitrage_optimization import ENTRIES,EXITS
    m,days=market()
    for enter in ENTRIES:
        for exit_ in EXITS:
            assert m.evaluate(days[0],4,enter,exit_,D(1500))==m.evaluate(days[0],4,enter,exit_,D(1500),trace=True)[0]


def test_original_signal_is_unchanged_when_using_actual_etf_score_without_nav():
    from backtest.rolling_arbitrage_optimization import ProductMarket
    m,days=market();m.prices['IF2501.CFE',days[0]]['close']=D(101)
    args=('IF',days,m.mapping,m.prices,m.spot,m.instruments)
    actual=ETFCandidateMarket(*args,dividend_events=m.dividend_events)
    original=ProductMarket(*args)
    assert actual.rates==original.rates
    assert actual.rate_provider('IF','IF2501.CFE',days[0])==original.rates[0]
    fast=actual.evaluate(days[0],4,D('.02'),D(0),D(1500))
    slow,fund=actual.evaluate(days[0],4,D('.02'),D(0),D(1500),trace=True)
    assert fast==slow and fast['arbitrage_pnl']==D(59)
    assert fund.completed[0]['exit_reason']=='delivery'
