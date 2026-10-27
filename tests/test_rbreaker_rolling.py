from datetime import date,timedelta
from types import SimpleNamespace

from backtest.models import D
from backtest.rbreaker_rolling import RollingAllocation,WeightedTradeStops,three_months_before
from backtest.rbreaker_portfolio import PortfolioRBreaker,allocate
from tests.test_rbreaker import bar
from tests.test_rbreaker_stops import setup,step


def allocator():
    result=RollingAllocation.__new__(RollingAllocation)
    result.audit=[]
    start=date(2026,1,1)
    result.returns={p:[(start+timedelta(days=n),sign*(.01 if n%2 else .005)) for n in range(60)]
                    for p,sign in [('IF',1),('IH',-1)]}
    return result


def test_month_window_clamps_day_and_excludes_current_future_and_old_results():
    assert three_months_before(date(2024,5,31))==date(2024,2,29)
    result=allocator()
    day=date(2026,3,2)
    expected=result.weights(day,('IF','IH'))
    result.returns['IH'] += [(day,1000),(day+timedelta(days=1),1000),(date(2025,1,1),1000)]
    assert result.weights(day,('IF','IH'))==expected
    assert expected=={'IF':D('.9'),'IH':D('.1')}
    assert all(str(r['information_day'])<str(day) for r in result.audit)


def test_cold_start_and_nonpositive_scores_fall_back_to_equal():
    result=allocator()
    assert result.weights(date(2026,1,10),('IF','IH'))=={'IF':D('.5'),'IH':D('.5')}
    result.returns['IF']=result.returns['IH']
    assert result.weights(date(2026,3,2),('IF','IH'))=={'IF':D('.5'),'IH':D('.5')}


def test_weighted_atr_budget_frozen_after_partial_fill_and_new_weights():
    inst,engine,day,old,strategy=setup()
    stop=WeightedTradeStops(old.instruments,old.atr)
    stop.multipliers={'IF':D('.5')}
    strategy.stop_policy=stop
    step(engine,strategy,bar(inst.id,day,'09:31',120))
    assert stop.seeds[inst.id]['budget']==D(5000)
    assert stop.seeds[inst.id]['distance']==D(10)
    step(engine,strategy,bar(inst.id,day,'09:32',121))
    stop.multipliers={'IF':D(2)}
    step(engine,strategy,bar(inst.id,day,'09:33',122))
    assert stop.active[inst.id]['budget']==D(5000)
    assert stop.active[inst.id]['risk_multiplier']==D('.5')


def test_lower_weight_margin_cap_does_not_allow_new_open_above_cap():
    inst,engine,day,old,strategy=setup()
    sizes=allocate(D(1000000),{'IF':D(110000)},{inst.id:(1,None)},
                   {inst.id:inst},{inst.id:D(120)},product_caps={'IF':D('.1')})
    assert sizes=={inst.id:0}
