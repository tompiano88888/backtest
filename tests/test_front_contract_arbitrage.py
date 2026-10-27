from datetime import date
from types import SimpleNamespace
from backtest.front_contract_arbitrage import front_map,THRESHOLDS
from backtest.all_contract_cash_arbitrage import analyze_contract
from backtest.models import D
from test_all_contract_arbitrage import fixture


def test_front_preserves_expiry_day_and_rolls_next_day_without_future_price_input():
    inst={'IF2501.CFE':SimpleNamespace(product='IF',expiry=date(2025,1,5)),
          'IF2502.CFE':SimpleNamespace(product='IF',expiry=date(2025,2,5))}
    first={c:date(2024,12,1) for c in inst}
    result=front_map(inst,first,{'IF':[date(2025,1,5),date(2025,1,6)]})
    assert result['IF',date(2025,1,5)]=='IF2501.CFE'
    assert result['IF',date(2025,1,6)]=='IF2502.CFE'


def test_non_front_signal_cannot_enter_and_execution_day_is_also_checked():
    days,inst,prices,spot=fixture()
    result=analyze_contract('IF2501.CFE',inst,prices,spot,days,days[0],entry_allowed=lambda d:False)
    assert result[1]==[] and result[3]==[]
    result=analyze_contract('IF2501.CFE',inst,prices,spot,days,days[0],entry_allowed=lambda d:d==days[0])
    assert result[1]==[] and result[2][1]['event']=='entry_cancelled_not_front'


def test_new_thresholds_open_at_two_and_hold_until_below_minus_four():
    days,inst,prices,spot=fixture()
    inst=SimpleNamespace(product='IC',multiplier=D(200),tick=D('.2'),expiry=date(2026,1,1),delivery=date(2026,1,1))
    spot={('000905.SH',d):dict(open=D(100),close=D(100)) for d in days}
    prices={('IC2501.CFE',d):dict(open=D(103),close=D(v),settlement_price=D(v))
            for d,v in zip(days,[103,103,'97.5',95,95,95])}
    result=analyze_contract('IC2501.CFE',inst,prices,spot,days,days[0],*THRESHOLDS['IC'],entry_allowed=lambda d:True)
    assert result[1][0]['entry_day']==days[1]
    assert not any(r['kind']=='exit' and r['day']==days[2] for r in result[3])
    assert result[1][0]['exit_signal_day']==days[3]
    assert result[1][0]['exit_day']==days[4]
    assert result[0]['rate_exits']==1
