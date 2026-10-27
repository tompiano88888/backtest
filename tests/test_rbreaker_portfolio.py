from datetime import date
from types import SimpleNamespace

from backtest.accounting import Ledger
from backtest.engine import Engine, ExecutionConfig, Frame
from backtest.models import D, MarginRule
from backtest.rbreaker_portfolio import FEE, PortfolioRBreaker, allocate, margin_usage
from tests.test_rbreaker import bar, fixture


def test_simultaneous_entries_share_total_budget_and_obey_product_cap():
    a=SimpleNamespace(product='IF',multiplier=D(300),tick=D('.2'),lot=1)
    b=SimpleNamespace(product='IH',multiplier=D(300),tick=D('.2'),lot=1)
    instruments={'A':a,'B':b}
    marks={'A':D(4000),'B':D(3000)}
    sizes=allocate(D(10000000),{},dict(A=(1,None),B=(-1,None)),instruments,marks)
    cost={k:sizes[k]*(marks[k]+instruments[k].tick)*instruments[k].multiplier for k in sizes}
    net_equity=D(10000000)-sum(cost.values())*FEE
    assert sum(cost.values())*D('.15')<=net_equity*D('.9')
    assert all(v*D('.15')<=net_equity*D('.5') for v in cost.values())
    assert sizes['A']>1 and sizes['B']>1
    assert sizes==allocate(D(10000000),{},dict(B=(-1,None),A=(1,None)),instruments,marks)


def test_usage_threshold_blocks_open_and_existing_overcap_is_not_sold():
    inst,_=fixture()
    result=allocate(D(1000),{'IF':D(900)},{inst.id:(1,None)},{inst.id:inst},{inst.id:D(100)})
    assert result[inst.id]==0
    result=allocate(D(1000),{'IF':D(600)},{inst.id:(1,None)},{inst.id:inst},{inst.id:D(100)})
    assert result[inst.id]==0


def test_margin_aggregates_months_on_larger_side():
    instruments={k:SimpleNamespace(id=k,product='IF',multiplier=D(300)) for k in ('A','B')}
    positions=[dict(instrument='A',quantity=2,side=1),dict(instrument='B',quantity=1,side=-1)]
    assert margin_usage(positions,{'A':D(100),'B':D(200)},instruments,D('.15'))=={'IF':D(9000)}


def test_fees_and_slippage_apply_to_open_and_close():
    inst,_=fixture()
    engine=Engine(Ledger({inst.id:inst},1000000),{inst.id:MarginRule('future',D('.15'))},
                  ExecutionConfig(participation=D(1),fee_rate=FEE,slippage_ticks=1))
    day=date(2026,1,5)
    first=bar(inst.id,day,'09:31',100)
    engine.step(Frame(first.end,{inst.id:first},frozenset({inst.id})))
    engine.target(inst.id,2,first.end)
    from dataclasses import replace
    second=replace(bar(inst.id,day,'09:32',101),low=D(100),high=D(102))
    engine.step(Frame(second.end,{inst.id:second},frozenset({inst.id})))
    engine.target(inst.id,0,second.end)
    third=replace(bar(inst.id,day,'09:33',103),low=D(102),high=D(104))
    engine.step(Frame(third.end,{inst.id:third},frozenset({inst.id})))
    opened,closed=engine.ledger.fills
    assert opened.price==D('101.2') and closed.price==D('102.8')
    expected_fee=(D('101.2')+D('102.8'))*300*2*FEE
    assert opened.fee+closed.fee==expected_fee
    assert engine.ledger.snapshot(engine.margin_rules).pnl==D(960)-expected_fee


def test_portfolio_reversal_waits_until_old_side_is_fully_closed():
    from dataclasses import replace
    inst,_=fixture()
    engine=Engine(Ledger({inst.id:inst},100000),{inst.id:MarginRule('future',D('.15'))},ExecutionConfig(participation=D(1)))
    strategy=PortfolioRBreaker({inst.id:inst})
    strategy.begin_day({'IF':inst.id},{inst.id:dict(high='100',low='90',close='95')})
    day=date(2026,1,5)
    for hm,price in [('09:31',120),('09:32',121),('09:33',94)]:
        b=bar(inst.id,day,hm,price)
        engine.step(Frame(b.end,{inst.id:b},frozenset({inst.id})),on_bar=strategy)
    assert engine.ledger.lots and engine.ledger.lots[0].side==1
    # No volume: closing must wait; never open a short alongside the long.
    b=replace(bar(inst.id,day,'09:34',93),volume=0)
    engine.step(Frame(b.end,{inst.id:b},frozenset({inst.id})),on_bar=strategy)
    assert all(p.side==1 for p in engine.ledger.lots)
    b=bar(inst.id,day,'09:35',93)
    engine.step(Frame(b.end,{inst.id:b},frozenset({inst.id})),on_bar=strategy)
    assert not engine.ledger.lots
    b=bar(inst.id,day,'09:36',92)
    engine.step(Frame(b.end,{inst.id:b},frozenset({inst.id})),on_bar=strategy)
    assert engine.ledger.lots and all(p.side==-1 for p in engine.ledger.lots)
