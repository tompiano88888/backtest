from datetime import date, datetime, timedelta
from dataclasses import replace

from backtest.accounting import Ledger
from backtest.engine import Engine, ExecutionConfig, Frame
from backtest.models import Auction, D, MarginRule
from backtest.rbreaker import RBreaker
from backtest.rbreaker_portfolio import PortfolioRBreaker
from backtest.rbreaker_stops import TradeStops
from tests.test_rbreaker import TZ, bar, fixture


def test_auction_signal_enters_continuous_bar_with_slippage_not_auction():
    inst,_=fixture()
    day=date(2026,1,5)
    stamp=datetime(2026,1,5,9,30,tzinfo=TZ)
    engine=Engine(Ledger({inst.id:inst},1000000),{inst.id:MarginRule('future',D('.15'))},
                  ExecutionConfig(participation=D(1),slippage_ticks=1))
    strategy=PortfolioRBreaker({inst.id:inst})
    strategy.begin_day({'IF':inst.id},{inst.id:dict(high=100,low=90,close=95)})
    engine.step(Frame(stamp,{},frozenset({inst.id}),auctions={inst.id:Auction(inst.id,stamp,day,D(120),100)}),on_bar=strategy)
    assert not engine.ledger.fills
    assert strategy.strategies['IF'].high==120
    b=replace(bar(inst.id,day,'09:31',121),low=D(120),high=D(122))
    engine.step(Frame(b.end,{inst.id:b},frozenset({inst.id})),on_bar=strategy)
    assert engine.ledger.fills and engine.ledger.fills[0].price==D('121.2')


def test_gap_stop_seen_in_auction_waits_for_continuous_open():
    inst,_=fixture()
    day=date(2026,1,5)
    engine=Engine(Ledger({inst.id:inst},1000000),{inst.id:MarginRule('future',D('.15'))},ExecutionConfig(participation=D(1)))
    stop=TradeStops({inst.id:inst},{(inst.id,day):D(10)})
    strategy=PortfolioRBreaker({inst.id:inst},stop_policy=stop)
    strategy.begin_day({'IF':inst.id},{inst.id:dict(high=100,low=90,close=95)})
    for hm,price in [('09:31',120),('09:32',121),('14:59',125),('15:00',125)]:
        b=bar(inst.id,day,hm,price)
        final=hm=='15:00'
        engine.step(Frame(b.end,{inst.id:b},frozenset({inst.id}),frozenset({inst.id}) if final else frozenset(),
                          ((inst.id,day,D(125)),) if final else ()),on_bar=strategy)
    next_day=day+timedelta(days=1)
    strategy.begin_day({'IF':inst.id},{inst.id:dict(high=125,low=115,close=125)})
    stamp=datetime.combine(next_day,datetime.min.time(),TZ).replace(hour=9,minute=30)
    auction=Auction(inst.id,stamp,next_day,D(100),100)
    engine.step(Frame(stamp,{},frozenset({inst.id}),auctions={inst.id:auction}),on_bar=strategy)
    assert engine.ledger.lots  # stop signal could not backfill this auction
    assert stop.events[-1]['trigger_time']==str(stamp)
    assert stop.active[inst.id]['budget']==D(10000)
    b=bar(inst.id,next_day,'09:31',99)
    engine.step(Frame(b.end,{inst.id:b},frozenset({inst.id})),on_bar=strategy)
    assert not engine.ledger.lots
    assert stop.completed[-1]['realized']==D(-19800)
