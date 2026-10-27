from datetime import date
from types import SimpleNamespace
from backtest.all_contract_cash_arbitrage import analyze_contract
from backtest.models import D


def fixture():
    days=[date(2025,1,i) for i in range(1,7)]
    inst=SimpleNamespace(product='IF',multiplier=D(10),tick=D('.1'),expiry=days[4],delivery=days[4])
    prices={('IF2501.CFE',d):dict(open=D(v),close=D(c),settlement_price=D(c))
            for d,v,c in zip(days,[104,103,100,98,97,97],[101,100,99,98,97,97])}
    spot={('000300.SH',d):dict(open=D(101 if d==days[3] else 100),close=D(100)) for d in days}
    return days,inst,prices,spot


def test_actual_pair_next_open_pnl_and_all_entry_events():
    days,inst,prices,spot=fixture()
    summary,cycles,daily,signals,_=analyze_contract('IF2501.CFE',inst,prices,spot,days,days[0])
    assert len(cycles)==1
    c=cycles[0]
    assert c['signal_day']==days[0] and c['entry_day']==days[1]
    assert c['exit_signal_day']==days[2] and c['exit_day']==days[3]
    assert c['gross_pnl']==D(60) and c['future_tick_pnl']==D(58)
    assert summary['future_tick_pnl']==D(58)
    assert sum(r['event']=='entry' for r in daily)==len(cycles)


def test_delivery_uses_actual_settlement_not_forced_zero_basis():
    days,inst,prices,spot=fixture()
    for d in days[1:]:prices['IF2501.CFE',d]['close']=D(101)
    prices['IF2501.CFE',days[4]]['settlement_price']=D(99)
    _,cycles,_,_,_=analyze_contract('IF2501.CFE',inst,prices,spot,days,days[0])
    assert cycles[0]['exit_reason']=='delivery'
    assert cycles[0]['exit_future']==D(99) and cycles[0]['exit_index']==D(100)
    assert cycles[0]['gross_pnl']==D(40) and cycles[0]['future_tick_pnl']==D(39)


def test_missing_open_cancels_entry_without_fill_and_future_changes_preserve_past():
    days,inst,prices,spot=fixture()
    original=analyze_contract('IF2501.CFE',inst,prices,spot,days,days[0])
    prices['IF2501.CFE',days[4]]['settlement_price']=D(1000)
    changed=analyze_contract('IF2501.CFE',inst,prices,spot,days,days[0])
    assert original[2][:4]==changed[2][:4]
    prices['IF2501.CFE',days[1]]['open']=None
    _,cycles,_,_,issues=analyze_contract('IF2501.CFE',inst,prices,spot,days,days[0])
    assert cycles==[] and any(r['reason']=='entry_missing_open' for r in issues)


def test_data_end_marks_without_forced_liquidation():
    days,inst,prices,spot=fixture()
    _,cycles,daily,_,_=analyze_contract('IF2501.CFE',inst,prices,spot,days[:2],days[0])
    assert cycles[0]['exit_day'] is None and cycles[0]['exit_reason']=='open_at_data_end'
    assert cycles[0]['future_tick_pnl']==daily[-1]['future_tick_pnl']
