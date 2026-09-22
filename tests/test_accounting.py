from datetime import date, datetime, timezone

import pytest

from backtest.accounting import Ledger
from backtest.models import Account, Asset, D, Fill, Instrument, Instruction, MarginRule

DAY = date(2026, 9, 21)
EXPIRY = date(2026, 9, 23)
NOW = datetime(2026, 9, 21, 1, tzinfo=timezone.utc)


def future(name="F", expiry=EXPIRY):
    return Instrument(name, Asset.FUTURE, Account.FUTURES, "TEST", "P", D(10), D(1), expiry=expiry, delivery=expiry)


def option(name="O"):
    return Instrument(name, Asset.OPTION, Account.ETF_OPTION, "TEST", "ETF", D(100), D("0.01"), expiry=EXPIRY, delivery=EXPIRY, underlying="S", strike=D(100))


def trade(ledger, instrument, side, qty, price, effect="open", group="standalone", day=DAY, fee=0):
    ledger.apply(Fill(NOW, day, Instruction(instrument, side, qty, effect, group), D(price), D(fee)))


def test_futures_daily_settlement_and_delivery_no_double_count():
    ledger = Ledger({"F": future()}, 1000)
    trade(ledger, "F", 1, 2, 100)
    ledger.settle("F", DAY, D(110))
    assert ledger.cash == 1200
    assert ledger.snapshot({"F": MarginRule("future", D("0.1"))}).equity == 1200
    ledger.settle("F", EXPIRY, D(105))
    assert ledger.cash == 1100
    assert not ledger.lots
    with pytest.raises(ValueError, match="Duplicate"):
        ledger.settle("F", EXPIRY, D(105))


def test_option_long_short_premium_mark_delivery():
    ledger = Ledger({"O": option()}, 1000)
    trade(ledger, "O", 1, 2, 3, group="long")
    trade(ledger, "O", -1, 1, 3, group="short")
    assert ledger.cash == 700
    ledger.marks["S"] = D(100)
    ledger.settle("O", DAY, D(5))
    snap = ledger.snapshot({"O": MarginRule("etf_option", D("0.12"), D("0.07"))})
    assert snap.cash == 700
    assert snap.equity == 1200
    assert len(ledger.lots) == 2
    ledger.settle("O", EXPIRY, D(8))
    assert ledger.cash == 1500
    assert not ledger.lots
    assert sum(ledger.group_pnl().values()) == 500


def test_margin_larger_amount_not_larger_notional():
    ledger = Ledger({"F": future(), "G": future("G")}, 1)
    trade(ledger, "F", 1, 2, 100)
    trade(ledger, "G", -1, 1, 100)
    snapshot = ledger.snapshot({"F": MarginRule("future", D("0.1")), "G": MarginRule("future", D("0.3"))})
    assert snapshot.margin == 300
    assert snapshot.equity == 1  # no margin debit, no admission check


def test_t1_and_isolated_group_close():
    inst = Instrument("S", Asset.EQUITY, Account.SECURITY, "TEST", "S", lot=100, sell_delay_days=1)
    ledger = Ledger({"S": inst}, 0)
    trade(ledger, "S", 1, 100, 10, group="a")
    trade(ledger, "S", 1, 100, 10, group="b")
    with pytest.raises(ValueError):
        trade(ledger, "S", 1, 100, 10, "close", "a")
    trade(ledger, "S", 1, 100, 11, "close", "a", date(2026, 9, 22))
    assert ledger.quantity("S", 1, "b") == 100
    assert ledger.quantity("S", 1, "a") == 0
    assert ledger.cash == -900  # negative cash is permitted, recorded honestly


def test_fee_and_group_reconciliation():
    ledger = Ledger({"F": future()}, 1000)
    trade(ledger, "F", 1, 2, 100, group="a", fee=3)
    trade(ledger, "F", -1, 1, 100, group="b", fee=2)
    ledger.settle("F", DAY, D(110))
    snap = ledger.snapshot({"F": MarginRule("future", D("0.1"))})
    assert snap.pnl == 95
    assert sum(ledger.group_pnl().values()) == snap.pnl


def test_corporate_action_quantity_cash_and_duplicate():
    inst = Instrument("S", Asset.EQUITY, Account.SECURITY, "TEST", "S")
    ledger = Ledger({"S": inst}, 10000)
    trade(ledger, "S", 1, 100, 10)
    ledger.corporate_action("a", "S", date(2026, 9, 22), D(1), D(2))
    ledger.marks["S"] = D("4.5")
    assert ledger.quantity("S", 1) == 200
    assert ledger.snapshot({}).equity == 10000
    with pytest.raises(ValueError):
        ledger.corporate_action("a", "S", date(2026, 9, 22), D(1), D(2))
