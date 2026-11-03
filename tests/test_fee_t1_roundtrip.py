"""Hand-reconciled synthetic multi-asset fees, T+1 and flat close-today rate."""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from backtest.accounting import Ledger
from backtest.engine import Engine, ExecutionConfig, Frame
from backtest.models import Account, Asset, Bar, D, Instrument, Instruction, MarginRule


def test_proportional_fees_no_minimum_and_etf_t1_with_future_today_audit():
    day = date(2020,1,2)
    t = datetime(2020,1,2,9,30,tzinfo=ZoneInfo('Asia/Shanghai'))
    instruments = {
        'E': Instrument('E',Asset.FUND,Account.SECURITY,'TEST','E',lot=100,sell_delay_days=1),
        'F': Instrument('F',Asset.FUTURE,Account.FUTURES,'TEST','F',multiplier=D(10),
                        expiry=date(2020,1,31),delivery=date(2020,1,31))}
    engine = Engine(Ledger(instruments,10000),{'F':MarginRule('future',D('.1'))},
                    ExecutionConfig(participation=1,commission_rate=D('.0001')),
                    {'F':ExecutionConfig(participation=1,fee_rate=D('.000023'),
                                          close_today_fee_rate=D('.000023'),fee_matching='today_first')})
    def step(at, e, f):
        quotes={c:Bar(c,at,at+timedelta(minutes=1),at.date(),*(D(price) for _ in range(4)),1000)
                for c,price in [('E',e),('F',f)]}
        return engine.step(Frame(at+timedelta(minutes=1),quotes,frozenset(quotes)))
    engine.submit([Instruction('E',1,100,group='etf')],t)
    engine.submit([Instruction('F',-1,2,group='future')],t)
    step(t,10,100)
    assert [f.fee for f in engine.ledger.fills]==[D('.1'),D('.046')]
    engine.submit([Instruction('E',1,100,'close','etf')],t+timedelta(minutes=1))
    engine.submit([Instruction('F',-1,1,'close','future')],t+timedelta(minutes=1))
    step(t+timedelta(minutes=1),11,90)
    assert engine.ledger.quantity('E',1,'etf')==100  # same-day sale blocked
    assert engine.fees.details[-1]['close_today_quantity']==1
    assert engine.ledger.fills[-1].fee==D('.0207')  # same rate, no surcharge
    engine.submit([Instruction('F',-1,1,'close','future')],t+timedelta(minutes=2))
    snapshot=step(t+timedelta(days=1),11,90)
    assert engine.fees.details[-1]['close_today_quantity']==0
    assert not engine.ledger.lots
    assert sum(f.fee for f in engine.ledger.fills)==D('.2974')
    # ETF gross 100; future gross 200; fees .1+.11+.046+.0207+.0207.
    assert snapshot.pnl==D('299.7026') and snapshot.cash==D('10299.7026')
    assert -sum(D(r['amount']) for r in engine.ledger.entries if r['kind']=='fee')==D('.2974')
