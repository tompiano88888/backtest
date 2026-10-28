from copy import deepcopy
from datetime import date, datetime, timedelta, timezone

import pytest

from backtest.account_events import AccountingEvents, CashDividend, IdleCashIncome, from_config
from backtest.accounting import Ledger
from backtest.engine import Engine, Frame
from backtest.models import Account, Asset, D, Instrument
from backtest.runner import result, run_scenario


def security_config(asset="fund"):
    days = [date(2026, 9, 18)+timedelta(days=n) for n in range(5)]
    frames = []
    for day in days:
        bars = [] if day.weekday() >= 5 else [dict(instrument="S", start=f"{day}T09:30:00+08:00", end=f"{day}T15:00:00+08:00", trading_date=str(day), open="10" if day == days[0] else "9", close="10" if day == days[0] else "9", high="10", low="9", volume=10000)]
        frames.append(dict(time=f"{day}T15:00:00+08:00", bars=bars, open_instruments=["S"] if bars else [], accounting_day_start=str(day), accounting_day_end=str(day)))
    dividend = dict(event_id="S-div-1", instrument="S", record_date=str(days[0]), ex_date=str(days[3]), payment_date=str(days[4]), cash_per_unit="1", withholding_rate=".1")
    return dict(initial_cash="10000", instruments=[dict(id="S", asset=asset, account="security", exchange="TEST", product="S", lot=100, sell_delay_days=1)], execution=dict(participation="1"), accounting=dict(dividends=[dividend]), frames=frames,
                instructions=[dict(at=f"{days[0]}T09:00:00+08:00", legs=[dict(instrument="S", side=1, quantity=100, group="a")]),
                              dict(at=f"{days[3]}T09:00:00+08:00", legs=[dict(instrument="S", side=1, quantity=100, effect="close", group="a"), dict(instrument="S", side=1, quantity=100, group="b")])])


@pytest.mark.parametrize("asset", ["fund", "equity"])
def test_record_close_ex_receivable_payment_after_sale_and_late_buyer(asset):
    config = security_config(asset)
    observations = []
    engine = run_scenario(config, on_bar=lambda ctx: observations.append(ctx.account))
    output = result(engine, config)
    assert [o.dividend_receivable for o in observations] == [0, 0, 0, 90, 0]
    assert observations[3].cash == 9000  # receivable is not available cash
    assert observations[3].equity == observations[4].equity == 9990
    assert output["final"]["pnl"] == -10  # price drop100, net dividend90
    assert output["group_pnl"] == {"a": D(-10), "b": D(0)}
    assert sum(output["group_pnl"].values()) == output["final"]["pnl"]
    assert output["income"] == dict(dividend_gross=D(100), dividend_tax=D(10), dividend_cash_paid=D(90), idle_cash_income=D(0))
    assert len(output["accounting"]["claims"]) == 1
    assert output["accounting"]["claims"][0]["holding"]["group"] == "a"
    assert engine.ledger.cash == 9090


def test_unpaid_dividend_in_equity_and_group_pnl_at_end():
    config = security_config()
    config["frames"] = config["frames"][:-1]
    engine = run_scenario(config)
    assert engine.ledger.snapshot({}).dividend_receivable == 90
    assert engine.ledger.snapshot({}).pnl == -10
    assert sum(engine.ledger.group_pnl().values()) == -10
    assert not any(r["kind"] == "cash_dividend" for r in engine.ledger.entries)


def test_ex_date_missing_raw_price_fails_or_uses_explicit_nontradeable_reference():
    config = security_config()
    config["frames"] = config["frames"][:-1]
    config["frames"][-1]["bars"] = []
    config["frames"][-1]["open_instruments"] = []
    with pytest.raises(ValueError, match="Missing ex-date raw valuation"):
        run_scenario(config)
    config["frames"][-1]["valuation_marks"] = {"S": "9"}
    engine = run_scenario(config)
    assert len(engine.ledger.fills) == 1  # valuation references cannot match orders
    assert engine.ledger.snapshot({}).equity == 9990
    assert sum(engine.ledger.group_pnl().values()) == -10


def test_sold_on_record_date_has_no_entitlement():
    config = security_config()
    config["instruments"][0]["sell_delay_days"] = 0
    row = deepcopy(config["frames"][0])
    row["time"] = "2026-09-18T10:00:00+08:00"
    row.pop("accounting_day_end")
    row["bars"][0]["end"] = row["time"]
    config["frames"][0].pop("accounting_day_start")
    config["frames"][0]["bars"][0]["start"] = row["time"]
    config["frames"].insert(0, row)
    config["instructions"][1]["at"] = "2026-09-18T10:00:00+08:00"
    config["instructions"][1]["legs"] = config["instructions"][1]["legs"][:1]
    engine = run_scenario(config)
    assert engine.accounting_events.claims == []
    assert engine.ledger.snapshot({}).pnl == 0


def empty_frame(day):
    return Frame(datetime.combine(day, datetime.min.time(), timezone.utc)+timedelta(hours=15), {}, frozenset(),
                 accounting_day_start=day, accounting_day_end=day)


def test_calendar_interest_holiday_rate_change_compounding_and_reconciliation():
    ledger = Ledger({}, 1000)
    income = IdleCashIncome([dict(valid_from="2026-01-01", annual_rate=".365"), dict(valid_from="2027-01-01", annual_rate=".73")], base="cash")
    engine = Engine(ledger, {}, accounting_events=AccountingEvents(idle_cash=income))
    engine.step(empty_frame(date(2026, 12, 31)))
    engine.step(empty_frame(date(2027, 1, 1)))
    assert ledger.cash == D("1003.002")
    assert ledger.group_pnl() == {"cash_management": D("3.002")}
    assert income.audit[0]["principal"] == 1000
    assert income.audit[1]["principal"] == 1001
    assert ledger.last_equity == ledger.cash
    with pytest.raises(ValueError, match="consecutive"):
        engine.step(empty_frame(date(2027, 1, 3)))
    assert ledger.cash == D("1003.002")


@pytest.mark.parametrize("base,expected", [("cash", "1"), ("cash_less_margin", ".9")])
def test_margin_only_changes_interest_base_not_cash_or_equity_deduction(base, expected):
    from test_accounting import future, trade, DAY
    from backtest.models import MarginRule
    ledger = Ledger({"F": future()}, 1000)
    trade(ledger, "F", 1, 1, 100)
    ledger.settle("F", DAY, D(100))
    income = IdleCashIncome([dict(valid_from="2026-01-01", annual_rate=".365")], base=base)
    engine = Engine(ledger, {"F": MarginRule("future", D(".1"))}, accounting_events=AccountingEvents(idle_cash=income))
    engine.step(empty_frame(DAY))
    snap = ledger.snapshot(engine.margin_rules)
    assert snap.cash == snap.equity == 1000+D(expected)
    assert snap.margin == 100


def test_interest_excludes_receivable_and_reserve_and_negative_cash():
    ledger = Ledger({"S": Instrument("S", Asset.FUND, Account.SECURITY, "TEST", "S")}, 100)
    ledger.recognize_dividend("claim", "S", "old", D(10000), D(0), date(2026, 9, 18))
    income = IdleCashIncome([dict(valid_from="2026-01-01", annual_rate=".365")], reserve="25")
    income.accrue(date(2026, 9, 19), ledger, {})
    assert ledger.cash == D("100.075")
    assert income.audit[0]["principal"] == 75
    assert sum(ledger.group_pnl().values()) == ledger.snapshot({}).pnl
    with pytest.raises(ValueError, match="Duplicate"):
        income.accrue(date(2026, 9, 19), ledger, {})
    assert ledger.cash == D("100.075")
    ledger.post_cash_event("cost", D(-1000), "test_cost", None, "old", date(2026, 9, 20))
    income.accrue(date(2026, 9, 20), ledger, {})
    assert income.audit[-1]["principal"] == income.audit[-1]["income"] == 0


def test_explicit_global_end_does_not_update_equity_at_partial_market_end():
    ledger = Ledger({}, 1000)
    engine = Engine(ledger, {}, accounting_events=AccountingEvents())
    day = date(2026, 9, 18)
    begin = Frame(datetime(2026, 9, 18, 9, tzinfo=timezone.utc), {}, frozenset(), frozenset({"MARKET"}), accounting_day_start=day)
    ledger.post_cash_event("cash", D(10), "test", None, "test", day)
    calls = []
    engine.step(begin, on_day_end=lambda ctx: calls.append(ctx.account.equity))
    assert ledger.last_equity == 1000
    assert calls == []
    engine.step(Frame(begin.time+timedelta(hours=1), {}, frozenset(), accounting_day_end=day), on_day_end=lambda ctx: calls.append(ctx.account.equity))
    assert ledger.last_equity == 1010
    assert calls == [1010]


def test_stock_tax_and_future_share_event_extension():
    from test_accounting import trade
    class Tax:
        def tax_amount(self, event, holding):
            assert holding.quantity == 100
            return D(5)

    class Shares:
        def day_start(self, day, ledger):
            if day == date(2026, 9, 22):
                ledger.corporate_action("split", "S", day, D(0), D(2))
        def day_end(self, day, ledger):
            pass

    ledger = Ledger({"S": Instrument("S", Asset.EQUITY, Account.SECURITY, "TEST", "S")}, 1000)
    trade(ledger, "S", 1, 100, 10, day=date(2026, 9, 21))
    event = CashDividend("cash", "S", date(2026, 9, 21), date(2026, 9, 22), date(2026, 9, 23), D(1))
    engine = Engine(ledger, {}, accounting_events=AccountingEvents([event], tax_policy=Tax(), handlers=[Shares()]))
    engine.step(empty_frame(date(2026, 9, 21)))
    from dataclasses import replace
    engine.step(replace(empty_frame(date(2026, 9, 22)), valuation_marks={"S": D("4.5")}))
    assert ledger.quantity("S", 1) == 200
    assert ledger.snapshot({}).equity == 995  # 900 stock + 95 net dividend
    assert engine.accounting_events.claims[0]["holding"].quantity == 100
    assert sum(ledger.group_pnl().values()) == -5


def test_invalid_dates_duplicate_ids_missing_rate_and_clock_fail():
    with pytest.raises(ValueError, match="record_date"):
        CashDividend("x", "S", date(2026, 1, 1), date(2026, 1, 1), date(2026, 1, 2), D(1))
    config = security_config()
    config["accounting"]["dividends"] *= 2
    with pytest.raises(ValueError, match="Duplicate"):
        run_scenario(config)
    engine = Engine(Ledger({}, 100), {}, accounting_events=AccountingEvents())
    with pytest.raises(ValueError, match="open accounting"):
        engine.step(Frame(datetime(2026, 1, 1, tzinfo=timezone.utc), {}, frozenset()))
    income = IdleCashIncome([dict(valid_from="2027-01-01", annual_rate=".01")])
    with pytest.raises(ValueError, match="Missing historical"):
        income.accrue(date(2026, 1, 1), Ledger({}, 100), {})


def test_disabled_income_retains_existing_results_and_no_clock_needed():
    from backtest.cli import demo_config
    config = demo_config()
    engine = run_scenario(config)
    assert engine.ledger.snapshot(engine.margin_rules).pnl == 52
    assert from_config(dict(idle_cash=dict(enabled=False))).idle_cash is None


def test_vendor_dividend_snapshot_rejected_without_importing_adapter():
    with pytest.raises(ValueError, match="Vendor dividend snapshots"):
        from_config(dict(dividend_snapshot={"version": "external"}))
