"""Synthetic, hand-reconciled delivery lifecycles; no strategy imports."""
from datetime import date, datetime
from zoneinfo import ZoneInfo

from backtest.accounting import Ledger
from backtest.engine import Engine, ExecutionConfig, Frame
from backtest.models import Account, Asset, Bar, D, Instruction, Instrument, MarginRule


ZONE = ZoneInfo('Asia/Shanghai')
DELIVERY = date(2026, 1, 7)


def at(day, hour=15, minute=0):
    return datetime(2026, 1, day, hour, minute, tzinfo=ZONE)


def quote(code, day, price, volume=100):
    value = D(price)
    return Bar(code, at(day, 9, 30), at(day), date(2026, 1, day),
               value, value + 5, value - 5, value, volume)


def make_engine(max_attempts=None):
    instruments = {
        'F': Instrument('F', Asset.FUTURE, Account.FUTURES, 'SYNTH', 'P',
                        multiplier=D(10), tick=D(1), expiry=DELIVERY, delivery=DELIVERY),
        'E': Instrument('E', Asset.FUND, Account.SECURITY, 'SYNTH', 'E',
                        tick=D('.01'), lot=1, sell_delay_days=1),
        'N': Instrument('N', Asset.FUTURE, Account.FUTURES, 'SYNTH', 'P',
                        multiplier=D(10), tick=D(1), expiry=date(2026, 2, 6), delivery=date(2026, 2, 6)),
    }
    rules = {code: MarginRule('future', D('.15')) for code in ('F', 'N')}
    return Engine(Ledger(instruments, 10000), rules,
                  ExecutionConfig(participation=D('.5'), slippage_ticks=0),
                  {'F': ExecutionConfig(participation=D('.5'), slippage_ticks=1)},
                  combination_max_attempts=max_attempts)


def step(engine, day, bars, *, opens=None, delivery=False, on_day_end=None):
    return engine.step(Frame(at(day), {bar.instrument: bar for bar in bars},
                             frozenset(opens if opens is not None else [bar.instrument for bar in bars]),
                             settle_instruments=frozenset({'F'}) if delivery else frozenset(),
                             settlements=(('F', DELIVERY, D(100)),) if delivery else ()),
                       on_day_end=on_day_end)


def pair(engine, quantity=10):
    return engine.submit([Instruction('F', -1, 1, group='pair'),
                          Instruction('E', 1, quantity, group='pair')], at(5), combination=True)


def test_filled_future_delivery_expires_remaining_etf_open_without_forced_trade():
    engine = make_engine()
    op = pair(engine)
    step(engine, 6, [quote('F', 6, 100), quote('E', 6, 10, 4)])
    assert [(leg.instrument, leg.quantity) for leg in op.legs] == [('E', 8)]
    original_fills = tuple(engine.ledger.fills)

    snap = step(engine, 7, [quote('F', 7, 100), quote('E', 7, 10, 0)], delivery=True)
    assert op.state == 'expired'
    assert op.combination and op.original_instruments == frozenset({'F', 'E'})
    assert tuple(engine.ledger.fills) == original_fills
    assert [(leg.instrument, leg.quantity) for leg in op.legs] == [('E', 8)]
    assert [(p.instrument, p.quantity) for p in engine.ledger.lots] == [('E', 2)]
    assert engine.groups['pair']['status'] == 'incomplete'
    # Future sells at 99 and delivers at 100: -10. ETF costs 20 and is worth 20.
    assert (snap.cash, snap.equity, snap.pnl, snap.margin) == (D(9970), D(9990), D(-10), D(0))
    assert engine.ledger.group_pnl() == {'pair': D(-10)}
    assert sum(w['code'] == 'DELIVERY_INTERRUPTED_OPERATION' for w in engine.warnings) == 1

    step(engine, 8, [quote('E', 8, 10)], opens={'E'})
    assert op.state == 'expired' and tuple(engine.ledger.fills) == original_fills
    assert engine.residual()['pending_operations'] == []
    assert engine.ledger.quantity('E', 1, 'pair') == 2


def test_expiration_visible_to_day_end_callback_allows_explicit_residual_exit():
    engine = make_engine()
    op = pair(engine)
    step(engine, 6, [quote('F', 6, 100), quote('E', 6, 10, 4)])
    cleanup = []

    def exit_residual(ctx):
        assert op.state == 'expired'
        assert any(w['code'] == 'DELIVERY_INTERRUPTED_OPERATION' for w in ctx.warnings)
        assert [(p['instrument'], p['quantity']) for p in ctx.positions] == [('E', 2)]
        cleanup.append(ctx.submit([Instruction('E', 1, 2, 'close', 'pair')]))

    # Without AccountingEvents, settle_instruments supplies the day-end callback.
    step(engine, 7, [quote('F', 7, 100), quote('E', 7, 10, 0)], delivery=True, on_day_end=exit_residual)
    assert len(engine.ledger.fills) == 2  # No backfill into the just completed Bar.
    snap = step(engine, 8, [quote('E', 8, 10)], opens={'E'})
    assert cleanup[0].state == 'completed'
    assert engine.ledger.fills[-1].time == at(8, 9, 30)
    assert not engine.ledger.lots and snap.cash == snap.equity == D(9990)
    assert engine.groups['pair']['status'] == 'closed'


def test_close_operation_expires_when_already_closed_original_future_delivers():
    engine = make_engine()
    opened = pair(engine)
    step(engine, 6, [quote('F', 6, 100), quote('E', 6, 10)])
    assert opened.state == 'completed'
    close = engine.submit([Instruction('F', -1, 1, 'close', 'pair'),
                           Instruction('E', 1, 10, 'close', 'pair')], at(6), combination=True)
    snap = step(engine, 7, [quote('F', 7, 102), quote('E', 7, 10, 4)], delivery=True)
    assert close.state == 'expired'
    assert [(leg.instrument, leg.quantity) for leg in close.legs] == [('E', 8)]
    assert engine.ledger.quantity('F', -1, 'pair') == 0
    assert engine.ledger.quantity('E', 1, 'pair') == 8
    # Future 99 -> 103 costs 40; ETF exits 2 at unchanged 10, leaving 8 worth 80.
    assert (snap.cash, snap.equity, snap.pnl) == (D(9880), D(9960), D(-40))
    assert len(engine.ledger.fills) == 4 and engine.groups['pair']['status'] == 'incomplete'
    step(engine, 8, [quote('E', 8, 10)], opens={'E'})
    assert engine.ledger.quantity('E', 1, 'pair') == 8


def test_roll_remainder_is_interrupted_by_old_contract_delivery():
    engine = make_engine()
    engine.submit([Instruction('F', -1, 2, group='pair')], at(5))
    step(engine, 6, [quote('F', 6, 100)])
    roll = engine.roll('pair', {'P': 'N'}, at(6), enabled=True)
    assert roll.mode == 'roll'
    step(engine, 7, [quote('F', 7, 100), quote('N', 7, 110, 2)], delivery=True)
    assert roll.state == 'expired'
    assert [(leg.instrument, leg.quantity, leg.effect) for leg in roll.legs] == [('N', 1, 'open')]
    assert engine.ledger.quantity('F', -1, 'pair') == 0
    assert engine.ledger.quantity('N', -1, 'pair') == 1
    assert engine.groups['pair']['status'] == 'incomplete'
    count = len(engine.ledger.fills)
    step(engine, 8, [quote('N', 8, 110)], opens={'N'})
    assert len(engine.ledger.fills) == count


def test_timeout_cleanup_surviving_leg_keeps_own_clock_after_delivery():
    engine = make_engine(max_attempts=1)
    op = pair(engine)
    step(engine, 6, [quote('F', 6, 100), quote('E', 6, 10, 4)])
    assert op.state == 'unwinding' and not op.combination
    # Both close legs initially remain. Future closes on delivery day; ETF cannot.
    step(engine, 7, [quote('F', 7, 100), quote('E', 7, 10, 0)], delivery=True)
    assert op.state == 'unwinding'
    assert [(leg.instrument, leg.quantity, leg.effect) for leg in op.legs] == [('E', 2, 'close')]
    assert not any(w['code'] == 'DELIVERY_INTERRUPTED_OPERATION' for w in engine.warnings)
    snap = step(engine, 8, [quote('E', 8, 10)], opens={'E'})
    assert op.state == 'unwound' and not engine.ledger.lots
    assert snap.pnl == D(-20)  # Short opens 99, closes 101, multiplier 10.


def test_cleanup_still_expires_if_its_remaining_leg_itself_delivers():
    engine = make_engine(max_attempts=1)
    op = pair(engine)
    step(engine, 6, [quote('F', 6, 100), quote('E', 6, 10, 0)])
    assert op.state == 'unwinding' and not op.combination
    step(engine, 7, [quote('F', 7, 100, 0), quote('E', 7, 10, 0)], delivery=True)
    assert op.state == 'expired' and not engine.ledger.lots
    assert engine.ledger.snapshot(engine.margin_rules).pnl == D(-10)


def test_non_delivery_settlement_does_not_interrupt_original_dependency():
    engine = make_engine()
    op = pair(engine)
    step(engine, 6, [quote('F', 6, 100), quote('E', 6, 10, 4)])
    # Settle F on a day before its final delivery, while ETF market is closed.
    engine.step(Frame(at(6, 16), {}, frozenset(), settle_instruments=frozenset({'F'}),
                      settlements=(('F', date(2026, 1, 6), D(100)),)))
    assert op.state == 'pending' and op.attempts == 1
    engine.step(Frame(at(7, 10), {}, frozenset({'F'})))
    assert op.state == 'pending' and op.attempts == 1
    # No delivery event has been supplied, so common-market matching still works.
    snap = step(engine, 7, [quote('F', 7, 100), quote('E', 7, 10)])
    assert op.state == 'completed' and engine.ledger.quantity('E', 1, 'pair') == 10
    assert snap.pnl == D(-10)


def test_unrelated_delivery_does_not_expire_other_pending_combination():
    engine = make_engine()
    op = engine.submit([Instruction('N', -1, 1, group='other'),
                        Instruction('E', 1, 10, group='other')], at(5), combination=True)
    step(engine, 6, [quote('N', 6, 110), quote('E', 6, 10, 4)])
    step(engine, 7, [quote('F', 7, 100), quote('N', 7, 110), quote('E', 7, 10, 0)], delivery=True)
    assert op.state == 'pending'
    assert not any(w['code'] == 'DELIVERY_INTERRUPTED_OPERATION' for w in engine.warnings)
