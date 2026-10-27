"""Causal daily features and fixed-contract forward labels for spread research."""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal
import json
from pathlib import Path

import numpy as np

from .futures_spread import contract_month, next_month, load_snapshot
from .models import Account, Asset, D, Instrument

WINDOWS = (5, 10, 20, 60)


def load_research_data(root, daily_version=None, metadata_version=None):
    import duckdb
    daily_version, raw = load_snapshot(root, daily_version)
    with duckdb.connect(str(Path(root) / 'metadata.duckdb'), read_only=True) as db:
        query = ('SELECT version,revisions FROM futures_contract_snapshots WHERE daily_version=? '
                 + ('AND version=? ' if metadata_version else '') + 'ORDER BY created_at DESC,version DESC LIMIT 1')
        found = db.execute(query, [daily_version, metadata_version] if metadata_version else [daily_version]).fetchone()
        if found is None:
            raise ValueError('No matching contract snapshot')
        facts = {}
        for code, rev in json.loads(found[1]).items():
            item = db.execute("SELECT payload FROM metadata WHERE revision=? AND key=? AND kind='instrument'",
                              [rev['instrument'], code]).fetchone()
            if item is None:
                raise ValueError(f'Missing revision {code}')
            facts[code] = json.loads(item[0])
    instruments = {}
    for code, fact in facts.items():
        if fact['expiry'] != fact['delivery']:
            raise ValueError(f'Expiry/delivery mismatch: {code}')
        instruments[code] = Instrument(code, Asset.FUTURE, Account.FUTURES, fact['exchange'], fact['product'],
                                       D(fact['multiplier']), D(fact['tick']), int(fact['lot']),
                                       date.fromisoformat(fact['expiry']), date.fromisoformat(fact['delivery']))
    return daily_version, found[0], raw, facts, instruments


def make_features(raw, facts, instruments, windows=WINDOWS):
    indexed = {(r['instrument'], r['trading_date']): r for r in raw}
    if len(indexed) != len(raw):
        raise ValueError('Duplicate price key')
    days = defaultdict(set)
    for r in raw:
        days[r['product']].add(r['trading_date'])
    output = []
    for product in sorted(days):
        calendar = sorted(days[product])
        codes = sorted(c for c in facts if c[:2] == product)
        first_month = min(contract_month(c) for c in codes)
        ratios, levels, highs, lows, spreads, daily_pnl = [], [1.0], [], [], [], []
        for i, day in enumerate(calendar):
            previous_day = calendar[i - 1] if i else None
            active = [c for c in codes if date.fromisoformat(facts[c]['first_trade_date']) <= day <= instruments[c].expiry]
            today = [indexed.get((c, day)) for c in active]
            complete = bool(today) and all(r is not None and all(r.get(k) is not None for k in
                             ('close', 'high', 'low', 'open_interest', 'volume')) for r in today)
            main = (min(today, key=lambda r: (-r['open_interest'], -r['volume'], contract_month(r['instrument'])))
                    if complete else None)
            prior = indexed.get((main['instrument'], previous_day)) if main is not None else None
            if prior is not None and prior.get('close'):
                denominator = float(prior['close'])
                ratio = float(main['close']) / denominator
                highs.append(levels[-1] * float(main['high']) / denominator)
                lows.append(levels[-1] * float(main['low']) / denominator)
                ratios.append(ratio - 1)
                levels.append(levels[-1] * ratio)
            else:
                # Reset rather than bridge a missing day with a stale index.
                ratios, levels, highs, lows = [], [1.0], [], []
            month = max(date(day.year, day.month, 1), first_month)
            near = f'{product}{month:%y%m}.CFE'
            if near not in instruments:
                raise ValueError(f'Missing natural-month metadata: {near}')
            # A next-open rollover is scheduled from this completed day's information.
            following = calendar[i + 1] if i + 1 < len(calendar) else None
            if day > instruments[near].expiry:
                month = next_month(month)
            near, far = f'{product}{month:%y%m}.CFE', f'{product}{next_month(month):%y%m}.CFE'
            a, b = indexed.get((near, day)), indexed.get((far, day))
            relative = float((b['close'] - a['close']) / a['close']) if a and b and a.get('close') and b.get('close') else np.nan
            spreads.append(relative)
            inst = instruments.get(near)
            pnl = np.nan
            if a and b and inst and all(r.get(k) is not None for r in (a, b) for k in ('settlement_price', 'previous_settlement_price')):
                pnl = float(((b['settlement_price'] - b['previous_settlement_price']) -
                             (a['settlement_price'] - a['previous_settlement_price'])) * inst.multiplier)
            daily_pnl.append(pnl)
            observed_near, observed_far = near, far
            if inst and following is not None and following >= inst.expiry:
                month = next_month(month)
                near, far = f'{product}{month:%y%m}.CFE', f'{product}{next_month(month):%y%m}.CFE'
            target_a, target_b = indexed.get((near, day)), indexed.get((far, day))
            inst = instruments.get(near)
            record = dict(product=product, day=day, main=main['instrument'] if main else '', near=near, far=far,
                          observed_near=observed_near, observed_far=observed_far,
                          main_complete=complete, relative_spread=relative, z=np.nan,
                          expiry_days=(inst.expiry - day).days if inst else np.nan, risk=np.nan,
                          known_notional=(target_a['close'] + target_b['close']) * inst.multiplier if target_a and target_b and inst else None,
                          decision_day=day, execution_day=following)
            trailing = np.asarray(spreads[-60:])
            if len(trailing) == 60 and np.isfinite(trailing).all() and trailing.std(ddof=1) > 0:
                record['z'] = (relative - trailing.mean()) / trailing.std(ddof=1)
            risk = np.asarray(daily_pnl[-60:])
            if len(risk) == 60 and np.isfinite(risk).all() and risk.std(ddof=1) > 0:
                record['risk'] = float(risk.std(ddof=1))
            for n in windows:
                usable = len(ratios) >= n
                record[f'return_{n}'] = levels[-1] / levels[-n - 1] - 1 if usable else np.nan
                record[f'amplitude_{n}'] = (max(highs[-n:]) - min(lows[-n:])) / levels[-n - 1] if usable else np.nan
                record[f'volatility_{n}'] = float(np.std(ratios[-n:], ddof=1)) if usable else np.nan
            for horizon in (1, 5, 10):
                end_index = i + 1 + horizon
                record[f'label_{horizon}'] = np.nan
                record[f'label_end_{horizon}'] = None
                if inst and following and end_index < len(calendar) and calendar[end_index] <= inst.expiry:
                    finish = calendar[end_index]
                    prices = [indexed.get((c, d)) for c in (near, far) for d in (following, finish)]
                    if all(r and r.get('open') is not None for r in prices):
                        an, ae, bn, be = prices
                        record[f'label_{horizon}'] = float(((be['open'] - bn['open']) - (ae['open'] - an['open'])) * inst.multiplier)
                        record[f'label_end_{horizon}'] = finish
            output.append(record)
    return output, indexed, {p: sorted(v) for p, v in days.items()}


def feature_names(family, n):
    if family == 'mean_reversion':
        return ['z']
    extras = {'return': [f'return_{n}'], 'amplitude': [f'amplitude_{n}'],
              'expiry': ['expiry_days'], 'volatility': [f'volatility_{n}'],
              'all': [f'return_{n}', f'amplitude_{n}', 'expiry_days', f'volatility_{n}']}
    return ['z'] + extras[family]


def fit_model(rows, names, alpha, mean_reversion=False):
    valid = [r for r in rows if r['main_complete'] and np.isfinite([r[k] for k in names] + [r['label_1']]).all()]
    if len(valid) < 252:
        return None
    x = np.array([[r[k] for k in names] for r in valid])
    y = np.array([r['label_1'] for r in valid])
    mean, scale = x.mean(axis=0), x.std(axis=0, ddof=1)
    scale[scale == 0] = 1
    z = (x - mean) / scale
    beta = np.linalg.solve(z.T @ z + alpha * np.eye(len(names)), z.T @ (y - y.mean()))
    intercept = float(y.mean())
    if mean_reversion:
        beta = np.array([-abs(beta[0])])
        intercept = 0.0
    return dict(names=names, mean=mean.tolist(), scale=scale.tolist(), beta=beta.tolist(),
                intercept=intercept, training_samples=len(valid))


def predict(model, row):
    if model is None or not row['main_complete']:
        return np.nan
    x = np.asarray([row[k] for k in model['names']])
    if not np.isfinite(x).all():
        return np.nan
    return float(((x - model['mean']) / model['scale']) @ model['beta'] + model['intercept'])
