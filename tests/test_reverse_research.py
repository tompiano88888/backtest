from datetime import date

from backtest.accounting import Ledger
from backtest.factor_simulation import execute
from backtest.models import Account, Asset, D, Instrument
from backtest.reverse_research import replay_reverse


def test_reverse_keeps_dates_and_sizes_but_charges_adverse_costs():
    near, far = 'IF2501.CFE', 'IF2502.CFE'
    instruments = {c: Instrument(c, Asset.FUTURE, Account.FUTURES, 'CFFEX', 'IF', D(10), D('.1'),
                                 expiry=date(2025, m, 20), delivery=date(2025, m, 20)) for c, m in [(near, 1), (far, 2)]}
    day1, day2 = date(2025, 1, 2), date(2025, 1, 3)
    ledger = Ledger(instruments, 10000)
    original_daily = []
    for day, effect, a, b, settlement_a, settlement_b in [
        (day1, 'open', 100, 110, 101, 115), (day2, 'close', 102, 118, 102, 118)]:
        execute(ledger, near, -1, 1, effect, 'IF', day, D(a), D('.00005'), 1)
        execute(ledger, far, 1, 1, effect, 'IF', day, D(b), D('.00005'), 1)
        for code, settlement in [(near, settlement_a), (far, settlement_b)]:
            if any(p.instrument == code for p in ledger.lots):
                ledger.settle(code, day, D(settlement))
        original_daily.append(dict(day=str(day), equity=str(ledger.cash)))
    fills = [dict(day=str(f.trading_date), instrument=f.instruction.instrument, side=f.instruction.side,
                  quantity=f.instruction.quantity, effect=f.instruction.effect, group='IF', price=f.price, fee=f.fee)
             for f in ledger.fills]
    prices = {(near, day1): dict(settlement_price=D(101)), (far, day1): dict(settlement_price=D(115))}
    reverse = replay_reverse(original_daily, fills, prices, instruments, 10000, '.00005', 1)
    assert ledger.cash == D('10055.785')
    assert reverse['daily'][-1]['equity'] == D('9935.785')
    assert [(f['day'], f['quantity'], f['effect']) for f in reverse['fills']] == [
        (date.fromisoformat(f['day']), f['quantity'], f['effect']) for f in fills]
    assert reverse['fills'][-1]['price'] == D('118.1')
    assert all(c['identity_error'] == 0 for c in reverse['cost_checks'])
    assert reverse == replay_reverse(original_daily, fills, prices, instruments, 10000, '.00005', 1)
