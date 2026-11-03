"""Paired release: next-minute clock, one-tick total cost and real accounting."""
from dataclasses import replace
from datetime import timedelta

import pytest

from backtest.engine import ExecutionConfig, Frame
from backtest.models import Auction, D, Fill, Instruction
from backtest.runner import result
from backtest.self_lock import LockScope, OpeningUnlockPolicy
from test_market_price_clamp import T, bar, fixture


SCOPE = LockScope('F', 'long-owner', 'short-owner')


def locked(long=3, short=3):
    e = fixture()
    e.config = ExecutionConfig(participation=None, price_model='limit_open_then_market', fee_rate=D('.0001'))
    yesterday = T-timedelta(days=1)
    for side, q, group in ((1, long, SCOPE.long_group), (-1, short, SCOPE.short_group)):
        e.ledger.apply(Fill(yesterday, yesterday.date(), Instruction('F', side, q, group=group), D(100), D(0)))
    return e


def minute(n=0, **changes):
    b = bar(h='104', l='96', volume=10)
    return replace(b, start=b.start+timedelta(minutes=n), end=b.end+timedelta(minutes=n),
                   turnover=D(10000), **changes)


def frame(b, settle=False):
    return Frame(b.end, {'F': b}, frozenset({'F'}),
                 frozenset({'F'}) if settle else frozenset(),
                 (('F', b.trading_date, D(100)),) if settle else ())


def policy_callback(policy, signals=()):
    return lambda ctx: policy.after_bar(ctx, scopes=[SCOPE], opening_signals=signals,
                                       first_bar_ends={'F': T+timedelta(minutes=1)})


def test_first_minute_decision_second_minute_vwap_cost_shared_volume_and_pnl():
    e = locked(); policy = OpeningUnlockPolicy()
    e.step(frame(minute()), on_bar=policy_callback(policy))
    assert len(e.ledger.fills) == 2  # No fills in the decision minute.
    op = e.operations[-1]
    snapshot = e.step(frame(minute(1)), on_bar=policy_callback(policy))
    fills = e.ledger.fills[2:]
    # volume10 * 50% = 5 total units -> two matched pairs, not five per side.
    assert [f.instruction.quantity for f in fills] == [2, 2]
    assert [f.price for f in fills] == [D('99.5'), D('100.5')]
    assert all(f.time == T+timedelta(minutes=2) for f in fills)
    assert sum(f.fee for f in fills) == D('.4')
    # 2 pairs * 1 tick * multiplier10 + actual-price fees = 20.4 cash loss.
    assert snapshot.cash == D('9979.6') and snapshot.pnl == D('-20.4')
    assert snapshot.margin == D(100)
    assert op.state == 'pending' and [l.quantity for l in op.legs] == [1, 1]
    e.step(frame(minute(2)), on_bar=policy_callback(policy))
    assert op.state == 'completed' and not e.ledger.lots
    assert e.ledger.cash == D('9969.4')
    assert sum(e.ledger.group_pnl().values(), D(0)) == D('-30.6')
    assert len(policy.decisions) == 1 and len(e.lock_releases) == 1
    payload = result(e, {'initial_cash': '10000'})
    assert payload['lock_releases'][0]['total_adverse_ticks'] == '1'
    assert all(r['method'] == 'bar_vwap_half_tick' for r in payload['execution_details'])


def test_opening_signal_keeps_locks_and_auction_does_not_count_as_first_minute():
    e = locked(); policy = OpeningUnlockPolicy()
    auction = Auction('F', T, T.date(), D(100), 1000)
    e.step(Frame(T, {}, frozenset({'F'}), auctions={'F': auction}), on_bar=policy_callback(policy))
    assert not policy.decisions and not e.operations
    e.step(frame(minute()), on_bar=policy_callback(policy, {'F'}))
    assert policy.decisions[0]['reason'] == 'opening_signal' and not e.operations
    e.step(frame(minute(1)), on_bar=policy_callback(policy))
    assert not e.operations and len(e.ledger.fills) == 2


def test_missing_first_minute_is_not_replaced_by_a_later_bar():
    e = locked(); policy = OpeningUnlockPolicy()
    e.step(Frame(T+timedelta(minutes=1), {}, frozenset({'F'})), on_bar=policy_callback(policy))
    e.step(frame(minute(1)), on_bar=policy_callback(policy))
    assert policy.decisions[0]['reason'] == 'missing_first_minute'
    assert not e.operations


def test_only_overlap_and_selected_owners_close_and_net_position_is_preserved():
    e = locked(long=5, short=2)
    yesterday = T-timedelta(days=1)
    e.ledger.apply(Fill(yesterday, yesterday.date(), Instruction('F', -1, 7, group='unrelated'), D(100), D(0)))
    e.step(frame(minute()))
    op = e.close_locked('F', SCOPE.long_group, SCOPE.short_group, e.now)
    e.step(frame(minute(1)))
    assert op.state == 'completed'
    assert e.ledger.quantity('F', 1, SCOPE.long_group) == 3
    assert e.ledger.quantity('F', -1, SCOPE.short_group) == 0
    assert e.ledger.quantity('F', -1, 'unrelated') == 7


def test_pending_targets_cancelled_without_reopening_and_duplicate_request_is_idempotent():
    e = locked(); e.step(frame(minute()))
    e.config = replace(e.config, price_model='next_open')
    e.target_group('F', 1, e.now, SCOPE.long_group)
    previous = e.operations[-1]
    op = e.close_locked('F', SCOPE.long_group, SCOPE.short_group, e.now)
    assert previous.state == 'cancelled' and not e.targets
    assert e.close_locked('F', SCOPE.long_group, SCOPE.short_group, e.now) is op
    e.step(frame(minute(1)), on_bar=lambda ctx: ctx.close_locked('F', SCOPE.long_group, SCOPE.short_group))
    assert len(e.lock_releases) == 1
    e.step(frame(minute(2), settle=True))
    assert op.state == 'completed' and not e.targets and not e.ledger.lots


@pytest.mark.parametrize('turnover,method', [(None, 'bar_ohlc4_half_tick'),
                                           (D(999999), 'bar_ohlc4_invalid_vwap_half_tick')])
def test_average_fallback_is_disclosed(turnover, method):
    e = locked(long=1, short=1); e.step(frame(minute()))
    e.close_locked('F', SCOPE.long_group, SCOPE.short_group, e.now)
    b = replace(minute(1), turnover=turnover, close=D(102))
    e.step(frame(b))
    assert [f.price for f in e.ledger.fills[2:]] == [D(100), D(101)]
    assert all(d['method'] == method for d in e.execution_details)


def test_no_volume_or_flat_price_cannot_fake_a_fill_and_release_survives_day_end():
    e = locked(long=1, short=1); e.step(frame(minute()))
    op = e.close_locked('F', SCOPE.long_group, SCOPE.short_group, e.now)
    e.step(frame(replace(minute(1), volume=0, turnover=D(0))))
    e.step(frame(replace(minute(2), high=D(100), low=D(100)), settle=True))
    assert len(e.ledger.fills) == 2 and op.state == 'pending'
    assert any(w.get('reason') == 'PRICE_OUTSIDE_BAR' for w in e.warnings)
    tomorrow = replace(minute(3), start=T+timedelta(days=1), end=T+timedelta(days=1, minutes=1),
                       trading_date=(T+timedelta(days=1)).date())
    e.step(frame(tomorrow))
    assert op.state == 'completed' and not e.ledger.lots


def test_one_side_can_remain_pending_without_reopening_the_filled_side():
    e = locked(long=1, short=1); e.step(frame(minute()))
    op = e.close_locked('F', SCOPE.long_group, SCOPE.short_group, e.now)
    b = replace(minute(1), high=D('100.2'), low=D(99), close=D(100))
    e.step(frame(b))
    assert len(e.ledger.fills) == 3 and len(op.legs) == 1
    assert op.legs[0].side == -1
    e.step(frame(minute(2)))
    assert op.state == 'completed' and len(e.ledger.fills) == 4


def test_auction_and_funding_do_not_override_minute_policy():
    e = locked(long=1, short=1); e.step(frame(minute()))
    e.ledger.cash = D('-100')
    op = e.close_locked('F', SCOPE.long_group, SCOPE.short_group, e.now)
    at = T+timedelta(minutes=2)
    e.auction_volume_exemption = lambda leg: True
    e.step(Frame(at, {}, frozenset({'F'}), auctions={'F': Auction('F', at, T.date(), D(100), 1000)}))
    assert len(e.ledger.fills) == 2 and op.state == 'pending'
    e.step(frame(minute(2)))
    assert op.state == 'completed' and e.ledger.cash < 0


def test_released_settlement_basis_is_not_double_counted():
    e = locked(long=1, short=1)
    e.ledger.settle('F', (T-timedelta(days=1)).date(), D(103))
    e.step(frame(minute()))
    e.close_locked('F', SCOPE.long_group, SCOPE.short_group, e.now)
    e.step(frame(minute(1), settle=True))
    assert e.ledger.cash == D('9989.8') and not e.ledger.lots


def test_invalid_quantity_and_instruction_policy_do_not_mutate_inventory():
    e = locked(); e.step(frame(minute()))
    with pytest.raises(ValueError):
        e.close_locked('F', SCOPE.long_group, SCOPE.short_group, e.now, 4)
    with pytest.raises(ValueError):
        Instruction('F', 1, 1, price_model='locked_close_average')
    with pytest.raises(ValueError):
        e.submit([Instruction('F', 1, 1, 'close', SCOPE.long_group, price_model='locked_close_average')], e.now)
    assert not e.operations and len(e.ledger.fills) == 2
