"""Date-only research executions; no invented intraday trading timestamps."""
from __future__ import annotations

from dataclasses import asdict
from datetime import date
from decimal import Decimal
import math

import numpy as np

from .accounting import Ledger
from .models import D, Fill, Instruction
from .factor_data import predict


def summary(daily, initial_cash):
    if not daily:
        return dict(days=0, sharpe=None, annual_return=None, max_drawdown=None, total_return=None)
    returns = np.array([float(r['return']) for r in daily])
    equities = np.array([float(initial_cash)] + [float(r['equity']) for r in daily])
    std = returns.std(ddof=1) if len(returns) > 1 else 0
    ratio = equities[-1] / equities[0]
    return dict(days=len(daily), sharpe=float(returns.mean() / std * math.sqrt(252)) if std > 0 else None,
                annual_return=float(ratio ** (252 / len(daily)) - 1) if ratio > 0 else None,
                total_return=float(ratio - 1), max_drawdown=float(np.min(equities / np.maximum.accumulate(equities) - 1)),
                fees=str(sum((r['fees'] for r in daily), D(0))),
                turnover=float(sum((r['traded_notional'] for r in daily), D(0)) / D(initial_cash)),
                fills=sum(r['fills'] for r in daily),
                max_nominal_ratio=max(float(r['nominal'] / r['equity']) if r['equity'] > 0 else 0 for r in daily),
                max_cash_deficit=str(max(r['cash_deficit'] for r in daily)))


def roundtrip_cost(row, instruments, fee_rate, slip):
    a, b = instruments.get(row['near']), instruments.get(row['far'])
    if a is None or b is None or row['known_notional'] is None:
        return math.inf
    return float(2 * (row['known_notional'] * fee_rate + (a.tick * a.multiplier + b.tick * b.multiplier) * slip))


def simulate(features, prices, calendars, instruments, models, start, end,
             initial_cash='10000000', fee_rate='0.00005', slip=1, sizing='risk',
             target_vol=.10, nominal_cap=2, details=False, decision_fee_rate=None, decision_slip=None,
             target_policy=None):
    """models: product -> model/threshold, or carry=True; coefficients already frozen."""
    initial_cash, fee_rate = D(initial_cash), D(fee_rate)
    decision_fee_rate = fee_rate if decision_fee_rate is None else D(decision_fee_rate)
    decision_slip = slip if decision_slip is None else decision_slip
    ledger = Ledger(instruments, initial_cash)
    signals = {(r['product'], r['execution_day']): r for r in features if r['execution_day'] is not None}
    days = sorted({d for p in models for d in calendars[p] if start <= d <= end})
    prior_equity, daily, diagnostics, decisions = initial_cash, [], [], []
    group_last = {p: D(0) for p in models}
    for day in days:
        first_fill, first_entry = len(ledger.fills), len(ledger.entries)
        desires, estimates = {}, {}
        for product, selected in models.items():
            if day not in calendars[product]:
                continue
            row = signals.get((product, day))
            if target_policy is not None:
                wanted, diagnostic = target_policy(product, day, row, ledger, prior_equity, sizing, target_vol)
                desires[product] = wanted
                estimates[product] = sum((abs(prices.get((code, row['day']), {}).get('close') or ledger.marks.get(code, D(0)))
                                         * qty * instruments[code].multiplier for (code, _), qty in wanted.items()), D(0)) if row else sum(
                                             (abs(ledger.marks.get(code, D(0))) * qty * instruments[code].multiplier
                                              for (code, _), qty in wanted.items()), D(0))
                if details:
                    decisions.append(diagnostic)
                continue
            direction, quantity, reason, expectation = 0, 0, 'missing_signal', np.nan
            if row is not None:
                expectation = 1.0 if selected.get('carry') else predict(selected.get('model'), row)
                cost = roundtrip_cost(row, instruments, decision_fee_rate, decision_slip)
                if selected.get('carry'):
                    direction = 1
                elif np.isfinite(expectation) and abs(expectation) > selected['threshold'] * cost:
                    direction = 1 if expectation > 0 else -1
                reason = 'threshold_or_missing_features'
                if direction and prior_equity > 0:
                    if sizing == 'one':
                        quantity, reason = 1, 'fixed_one'
                    elif np.isfinite(row['risk']) and row['risk'] > 0:
                        quantity = int(float(prior_equity) * target_vol * .5 / math.sqrt(252) / row['risk'])
                        reason = 'risk_budget'
                    else:
                        reason = 'missing_or_zero_risk'
                if quantity > 0 and row['known_notional'] is not None:
                    estimates[product] = row['known_notional'] * quantity
                else:
                    direction, quantity = 0, 0
                desires[product] = ({(row['near'], -direction): quantity, (row['far'], direction): quantity}
                                    if quantity else {})
                if details:
                    decisions.append(dict(day=day, signal_day=row['day'], product=product,
                                          prediction=expectation, roundtrip_cost=cost, direction=direction,
                                          proposed_quantity=quantity, reason=reason, near=row['near'], far=row['far']))
            else:
                desires[product] = {}
        total = sum(estimates.values(), D(0))
        cap = max(prior_equity, D(0)) * D(nominal_cap)
        if total > cap and total > 0:
            if target_policy is None:
                for product in desires:
                    desires[product] = {key: int(qty * cap / total) for key, qty in desires[product].items() if int(qty * cap / total)}
            else:
                # A holding policy promises fixed quantities until reversal/expiry.
                retained = {p for p, wanted in desires.items() if wanted and wanted == {
                    (lot.instrument, lot.side): ledger.quantity(lot.instrument, lot.side, p)
                    for lot in ledger.lots if lot.group == p}}
                existing = sum((estimates[p] for p in retained), D(0))
                fresh = sum((estimates[p] for p in estimates if p not in retained), D(0))
                room = max(cap - existing, D(0))
                if fresh > room and fresh > 0:
                    for product in desires.keys() - retained:
                        desires[product] = {key: int(qty * room / fresh) for key, qty in desires[product].items() if int(qty * room / fresh)}
        # Persistently exit unwanted legs. New exposure waits if an old leg cannot close.
        blocked = set()
        for product, wanted in desires.items():
            held = {(p.instrument, p.side): ledger.quantity(p.instrument, p.side, product)
                    for p in ledger.lots if p.group == product}
            for (code, side), qty in sorted(held.items()):
                excess = qty - wanted.get((code, side), 0)
                if excess > 0:
                    raw = prices.get((code, day))
                    if raw is None or raw.get('open') is None:
                        blocked.add(product)
                        diagnostics.append(dict(day=day, product=product, instrument=code, reason='exit_missing_open'))
                        continue
                    execute(ledger, code, side, excess, 'close', product, day, raw['open'], fee_rate, slip)
        for product, wanted in desires.items():
            if product in blocked:
                continue
            # Both required opens must be present; a missing quote never implies a fill.
            needed = [(code, side, qty - ledger.quantity(code, side, product))
                      for (code, side), qty in sorted(wanted.items())]
            if any(qty > 0 and (prices.get((code, day)) is None or prices[code, day].get('open') is None)
                   for code, _, qty in needed):
                diagnostics.append(dict(day=day, product=product, instrument='', reason='entry_missing_open'))
                continue
            for code, side, qty in needed:
                if qty > 0:
                    execute(ledger, code, side, qty, 'open', product, day, prices[code, day]['open'], fee_rate, slip)
        if target_policy is not None and hasattr(target_policy, 'expiry_exits'):
            diagnostics.extend(target_policy.expiry_exits(ledger, day, prices, fee_rate, slip))
        for code in sorted({p.instrument for p in ledger.lots}):
            raw = prices.get((code, day))
            if raw is None or raw.get('settlement_price') is None:
                if instruments[code].delivery <= day or day in calendars[instruments[code].product]:
                    raise ValueError(f'Missing held-contract settlement: {code}/{day}')
                continue
            ledger.settle(code, day, raw['settlement_price'])
        group_values = ledger.group_pnl()
        equity = initial_cash + sum(group_values.values(), D(0))
        if abs(equity - ledger.cash) > D('0.00000001'):
            # Allows a market holiday mark with an unsettled basis; equity still reconciles to lots.
            assert equity == ledger.cash + sum(p.side * p.quantity * instruments[p.instrument].multiplier *
                                              (ledger.marks[p.instrument] - p.basis) for p in ledger.lots)
        fills = ledger.fills[first_fill:]
        nominal = sum((abs(ledger.marks[p.instrument]) * p.quantity * instruments[p.instrument].multiplier for p in ledger.lots), D(0))
        record = dict(day=day, cash=ledger.cash, equity=equity,
                      return_=((equity - prior_equity) / prior_equity if prior_equity > 0 else D(0)),
                      fees=sum((f.fee for f in fills), D(0)), nominal=nominal,
                      cash_deficit=max(-ledger.cash, D(0)), fills=len(fills),
                      traded_notional=sum((f.price * f.instruction.quantity * instruments[f.instruction.instrument].multiplier for f in fills), D(0)))
        record['return'] = record.pop('return_')
        for product in models:
            group_value = group_values.get(product, D(0))
            record[f'pnl_{product}'] = group_value - group_last[product]
            group_last[product] = group_value
        if details:
            record['positions'] = ';'.join(f'{p.instrument}:{p.side}:{p.quantity}' for p in ledger.lots)
        daily.append(record)
        prior_equity = equity
    result = dict(metrics=summary(daily, initial_cash), daily=daily, warnings=diagnostics)
    if details:
        result.update(decisions=decisions, entries=ledger.entries,
                      fills=[dict(day=f.trading_date, **asdict(f.instruction), price=f.price, fee=f.fee) for f in ledger.fills],
                      terminal_positions=[asdict(p) for p in ledger.lots])
    return result


def execute(ledger, code, side, quantity, effect, product, day, open_price, fee_rate, slip):
    instruction = Instruction(code, side, int(quantity), effect, product)
    instrument = ledger.instruments[code]
    price = D(open_price) + (1 if instruction.buy else -1) * instrument.tick * slip
    if price <= 0:
        raise ValueError('Nonpositive execution price')
    fee = price * instrument.multiplier * quantity * fee_rate
    # Fill.time is the trading DATE here, explicitly not a historical session timestamp.
    ledger.apply(Fill(day, day, instruction, price, fee))
