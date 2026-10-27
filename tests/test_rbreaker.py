from datetime import date, datetime, timedelta
from types import SimpleNamespace

from backtest.accounting import Ledger
from backtest.engine import Engine, ExecutionConfig, Frame
from backtest.models import Account, Asset, Bar, D, Instrument, MarginRule
from backtest.rbreaker import RBreaker, TZ, levels, normalize_minute, reconcile_fills, select_main


def fixture():
    inst = Instrument('IF2601.CFE', Asset.FUTURE, Account.FUTURES, 'CFFEX', 'IF', D(300), D('.2'), 1, date(2026,1,16), date(2026,1,16))
    engine = Engine(Ledger({inst.id:inst}, 1000000), {inst.id:MarginRule('future',D('.15'))}, ExecutionConfig(participation=D(1)))
    return inst, engine


def bar(code, day, hm, price):
    end = datetime.fromisoformat(f'{day}T{hm}:00').replace(tzinfo=TZ)
    return Bar(code, end-timedelta(minutes=1),end,day,*([D(price)]*4),100)


def test_settlement_and_overnight_close_do_not_double_count():
    inst, engine = fixture()
    first, second = date(2026,1,5), date(2026,1,6)
    callback = RBreaker()
    callback.begin_day(inst.id,dict(high='100',low='90',close='95'))
    for hm, price in [('09:31','120'),('09:32','121'),('14:59','122'),('15:00','123')]:
        b = bar(inst.id,first,hm,price)
        final = hm == '15:00'
        engine.step(Frame(b.end,{inst.id:b},frozenset({inst.id}),frozenset({inst.id}) if final else frozenset(),((inst.id,first,D(124)),) if final else ()), on_bar=callback)
    # Signal 120 executes next open at 121. Settlement 124 realizes 900.
    assert len(engine.ledger.fills) == 1
    assert engine.ledger.fills[0].price == D(121)
    assert engine.ledger.cash == D(1000900)
    assert engine.ledger.lots[0].basis == D(124)
    callback.begin_day(inst.id,dict(high='125',low='115',close='123'))
    for hm,price in [('09:31','126'),('14:59','126'),('15:00','127')]:
        b = bar(inst.id,second,hm,price)
        final = hm == '15:00'
        engine.step(Frame(b.end,{inst.id:b},frozenset({inst.id}),frozenset({inst.id}) if final else frozenset(),((inst.id,second,D(128)),) if final else ()),on_bar=callback)
    assert not engine.ledger.lots
    assert engine.ledger.snapshot(engine.margin_rules).pnl == D(1800)
    assert reconcile_fills(engine) == D(1800)


def test_reversal_uses_observed_setup_and_next_bar():
    inst, engine = fixture()
    callback = RBreaker()
    day = date(2026,1,5)
    callback.begin_day(inst.id,dict(high='100',low='90',close='95'))
    for hm,price in [('09:31','120'),('09:32','121'),('09:33','94')]:
        b=bar(inst.id,day,hm,price)
        engine.step(Frame(b.end,{inst.id:b},frozenset({inst.id})),on_bar=callback)
    assert engine.ledger.lots[0].side == 1
    b=bar(inst.id,day,'09:34','93')
    engine.step(Frame(b.end,{inst.id:b},frozenset({inst.id})),on_bar=callback)
    assert len(engine.ledger.lots) == 1 and engine.ledger.lots[0].side == -1
    assert engine.ledger.fills[-1].price == D(93)
    assert callback.mode[inst.id] == 'reversal'


def test_prior_day_selection_and_expiry():
    rows=[dict(instrument='A',open_interest='100',volume=100),dict(instrument='B',open_interest='200',volume=50)]
    instruments={'A':SimpleNamespace(expiry=date(2026,2,1)), 'B':SimpleNamespace(expiry=date(2026,1,1))}
    assert select_main(rows,instruments,date(2026,1,2)) == 'A'


def test_opening_observation_is_not_a_tradable_bar():
    row=dict(instrument='A',source_timestamp=datetime(2026,1,5,9,30),open='1',high='1',low='1',close='1',volume='100')
    assert normalize_minute(row) is None
    row['source_timestamp'] += timedelta(minutes=1)
    result=normalize_minute(row)
    assert result.start.time().isoformat() == '09:30:00'


def test_six_levels_hand_calculation():
    k=levels(dict(high='100',low='90',close='95'))
    assert k == dict(bbreak=D('106.025'),sbreak=D('83.975'),ssetup=D('102.35'),bsetup=D('87.65'),senter=D('98.025'),benter=D('91.975'))
