"""Historical conditional spread distributions and hold-until-reversal trades."""
from __future__ import annotations

from datetime import date, timedelta
import hashlib
import json
import math
from pathlib import Path

import numpy as np

from .factor_data import load_research_data, make_features
from .factor_research import dump, csv_if, sharpe_interval
from .factor_simulation import simulate, execute, summary
from .futures_spread import PRODUCTS, write_csv
from .models import D

DEFAULTS = dict(root='data/index_futures', output='outputs/index_futures_expiry_research',
                daily_version=None, metadata_version=None, train_end='2023-12-31',
                validation_end='2024-12-31', test_start='2025-01-01', initial_cash='10000000',
                fee_rate='0.00005', slippage_ticks=1, target_vol=.10, nominal_cap=2,
                min_samples=30, min_contracts=12, seed=20261009, bootstrap_samples=500, rule='quantile')


def observations(raw, facts, instruments):
    features, prices, calendars = make_features(raw, facts, instruments)
    for r in features:
        r['near'], r['far'] = r['observed_near'], r['observed_far']
        inst = instruments.get(r['near'])
        r['expiry_days'] = (inst.expiry - r['day']).days if inst else None
        a, b = prices.get((r['near'], r['day'])), prices.get((r['far'], r['day']))
        r['known_notional'] = (a['close'] + b['close']) * inst.multiplier if a and b and inst else None
    return features, prices, calendars


def build_profile(rows, instruments, boundary, radius, quantile, history_years,
                  min_samples=30, min_contracts=12):
    try:
        earliest = boundary.replace(year=boundary.year - history_years) if history_years else date.min
    except ValueError:
        earliest = date(boundary.year - history_years, 2, 28)
    # Completed historical cycles only; current/test observations never enter the reference.
    history = [r for r in rows if earliest <= r['day'] <= boundary and
               instruments[r['near']].expiry <= boundary and np.isfinite(r['relative_spread'])]
    profile = {}
    for dte in range(0, max([r['expiry_days'] for r in history if r['expiry_days'] is not None] + [0]) + radius + 1):
        sample = [r for r in history if abs(r['expiry_days'] - dte) <= radius]
        contracts = len({r['near'] for r in sample})
        if len(sample) < min_samples or contracts < min_contracts:
            continue
        values = np.array([r['relative_spread'] for r in sample])
        profile[dte] = dict(dte=dte, count=len(sample), contracts=contracts, mean=float(values.mean()),
                            median=float(np.median(values)), std=float(values.std(ddof=1)),
                            low=float(np.quantile(values, quantile)), high=float(np.quantile(values, 1 - quantile)))
    return profile


def direction(row, profile):
    if row is None or row['expiry_days'] not in profile or not np.isfinite(row['relative_spread']):
        return 0
    band = profile[row['expiry_days']]
    if band['low'] >= band['high']:
        return 0
    if row['relative_spread'] < band['low']:
        return 1
    if row['relative_spread'] > band['high']:
        return -1
    return 0


class HoldingPolicy:
    def __init__(self, profiles, instruments, rule='quantile', trade_sign=1):
        if trade_sign not in (-1, 1):
            raise ValueError('trade_sign must be +1 or -1')
        self.trade_sign = trade_sign
        self.profiles, self.instruments = profiles, instruments
        self.rule = rule
        self.exit_due = set()

    def __call__(self, product, day, row, ledger, equity, sizing, target_vol):
        held = {(p.instrument, p.side): ledger.quantity(p.instrument, p.side, product)
                for p in ledger.lots if p.group == product}
        signal = direction(row, self.profiles.get(product, {}))
        band = self.profiles.get(product, {}).get(row['expiry_days'], {}) if row else {}
        decision = dict(day=day, signal_day=row['day'] if row else None, product=product,
                        near=row['near'] if row else '', far=row['far'] if row else '',
                        dte=row['expiry_days'] if row else None, ratio=row['relative_spread'] if row else None,
                        low=band.get('low'), high=band.get('high'), mean=band.get('mean'), std=band.get('std'), reference_count=band.get('count', 0),
                        signal=signal, trade_direction=signal * self.trade_sign, reason='', requested_quantity=0)
        if not held:
            self.exit_due.discard(product)
        if product in self.exit_due or (held and (len(held) != 2 or len(set(held.values())) != 1)):
            decision['reason'] = 'retry_residual_exit'
            return {}, decision
        if held and min(self.instruments[c].expiry for c, _ in held) <= day:
            decision['reason'] = 'expiry_close_at_eod'
            return held, decision
        if held:
            old_far = max(held, key=lambda key: self.instruments[key[0]].expiry)
            old_direction = old_far[1] * self.trade_sign
            if self.rule == 'mean_std':
                reached = row is not None and band and np.isfinite(row['relative_spread']) and (
                    row['relative_spread'] >= band['mean'] if old_direction > 0 else row['relative_spread'] <= band['mean'])
                if reached:
                    decision['reason'] = 'exit_at_mean'
                    return {}, decision
                decision['reason'] = 'hold_until_mean'
                decision['requested_quantity'] = next(iter(held.values()))
                return held, decision
            if not signal or signal == old_direction:
                decision['reason'] = 'hold_fixed_quantity'
                decision['requested_quantity'] = next(iter(held.values()))
                return held, decision
        if row is None or not signal or row['known_notional'] is None or equity <= 0:
            decision['reason'] = 'no_entry'
            return {}, decision
        if day >= self.instruments[row['near']].expiry:
            decision['reason'] = 'no_new_entry_on_expiry'
            return {}, decision
        if held:
            quantity = next(iter(held.values()))  # reversal preserves the entry size
        elif sizing == 'one':
            quantity = 1
        elif np.isfinite(row['risk']) and row['risk'] > 0:
            quantity = int(float(equity) * target_vol * .5 / math.sqrt(252) / row['risk'])
        else:
            quantity = 0
        decision.update(reason='reverse' if held else 'entry', requested_quantity=quantity)
        return ({(row['near'], -signal * self.trade_sign): quantity,
                 (row['far'], signal * self.trade_sign): quantity} if quantity > 0 else {}), decision

    def expiry_exits(self, ledger, day, prices, fee_rate, slip):
        events = []
        for product in self.profiles:
            held = [p for p in ledger.lots if p.group == product]
            if not any(self.instruments[p.instrument].expiry == day for p in held):
                continue
            self.exit_due.add(product)
            for lot in list(held):
                if self.instruments[lot.instrument].expiry == day:
                    continue  # original Ledger will cash settle the expired leg
                raw = prices.get((lot.instrument, day))
                if raw is None or raw.get('close') is None:
                    events.append(dict(day=day, product=product, instrument=lot.instrument, reason='expiry_far_missing_close'))
                    continue
                execute(ledger, lot.instrument, lot.side, lot.quantity, 'close', product, day, raw['close'], fee_rate, slip)
                events.append(dict(day=day, product=product, instrument=lot.instrument, reason='expiry_far_close'))
        return events


def run(overrides=None, progress=print):
    config = DEFAULTS | (overrides or {})
    if set(config) - set(DEFAULTS):
        raise ValueError('Unknown configuration fields')
    if config['rule'] not in ('quantile', 'mean_std'):
        raise ValueError('Unknown signal rule')
    train_end, validation_end, test_start = map(date.fromisoformat,
                                               [config['train_end'], config['validation_end'], config['test_start']])
    if not train_end < validation_end < test_start:
        raise ValueError('Invalid temporal split')
    out = Path(config['output'])
    out.mkdir(parents=True, exist_ok=True)
    dv, mv, raw, facts, instruments = load_research_data(config['root'], config['daily_version'], config['metadata_version'])
    config.update(daily_version=dv, metadata_version=mv)
    dump(out / 'config.json', config)
    specs = [dict(id=f'r{radius}_q{q}_h{history}', radius=radius, quantile=q, history_years=history)
             for radius in (0, 2, 5) for q in (.1, .2, .3) for history in (0, 3)] if config['rule'] == 'quantile' else [
                 dict(id=f'r{radius}_sigma1_h{history}', radius=radius, history_years=history)
                 for radius in (0, 2, 5) for history in (0, 3)]
    dump(out / 'registry.json', specs)
    rows, prices, calendars = observations(raw, facts, instruments)
    write_csv(out / 'observations.csv', [{k: r[k] for k in ('product', 'day', 'near', 'far', 'expiry_days', 'relative_spread', 'risk', 'known_notional', 'execution_day')} for r in rows])
    args = dict(initial_cash=config['initial_cash'], fee_rate=config['fee_rate'], slip=config['slippage_ticks'],
                target_vol=config['target_vol'], nominal_cap=config['nominal_cap'])
    trials, selected, profiles, descriptions = [], {}, {}, []
    for product in PRODUCTS:
        group = [r for r in rows if r['product'] == product]
        winner = None
        val_days = [d for d in calendars[product] if train_end < d <= validation_end]
        last = val_days[-11] if len(val_days) > 10 else validation_end
        for spec in specs:
            profile = build_profile(group, instruments, train_end, spec['radius'], spec.get('quantile', .2), spec['history_years'],
                                    config['min_samples'], config['min_contracts'])
            if config['rule'] == 'mean_std':
                for band in profile.values():
                    band['low'], band['high'] = band['mean'] - band['std'], band['mean'] + band['std']
            policy = HoldingPolicy({product: profile}, instruments, config['rule'])
            result = simulate(rows, prices, calendars, instruments, {product: {}}, train_end + timedelta(days=1), last,
                              target_policy=policy, **args)
            metrics = result['metrics']
            trials.append(dict(product=product, **spec, reference_dte_buckets=len(profile), **metrics))
            score = metrics['sharpe'] if metrics['sharpe'] is not None and metrics['fills'] >= 20 else -math.inf
            if winner is None or score > winner[0]:
                winner = score, spec, profile
        if winner is not None and np.isfinite(winner[0]):
            selected[product] = winner[1] | dict(validation_sharpe=winner[0])
            profiles[product] = winner[2]
        else:
            selected[product] = dict(id='no_eligible_profile')
            profiles[product] = {}
        descriptions.extend(dict(product=product, **band) for band in profiles[product].values())
        progress(f'{product}: {selected[product]["id"]}', flush=True)
    write_csv(out / 'trials.csv', trials)
    dump(out / 'selected.json', selected)
    csv_if(out / 'profiles.csv', descriptions)
    evaluations, yearly = [], []
    base_result = None
    end = max(r['day'] for r in rows)
    for sizing in ('risk', 'one'):
        for cost in ('base', 'stress'):
            kwargs = args | (dict(fee_rate=str(D(config['fee_rate']) * 2), slip=config['slippage_ticks'] * 2) if cost == 'stress' else {})
            result = simulate(rows, prices, calendars, instruments, {p: {} for p in PRODUCTS}, test_start, end,
                              sizing=sizing, details=True, target_policy=HoldingPolicy(profiles, instruments, config['rule']), **kwargs)
            ci = sharpe_interval(result['daily'], config['bootstrap_samples'], config['seed'])
            evaluations.append(dict(sizing=sizing, cost=cost, **result['metrics'], sharpe_ci_low=ci[0], sharpe_ci_high=ci[1]))
            label = f'{sizing}_{cost}'
            for kind in ('daily', 'fills', 'entries', 'decisions', 'warnings'):
                csv_if(out / f'{label}_{kind}.csv', result[kind])
            dump(out / f'{label}_terminal_positions.json', result['terminal_positions'])
            for year in sorted({r['day'].year for r in result['daily']}):
                subset = [r for r in result['daily'] if r['day'].year == year]
                initial = subset[0]['equity'] / (1 + subset[0]['return'])
                yearly.append(dict(sizing=sizing, cost=cost, product='portfolio', year=year, **summary(subset, initial)))
            for product in PRODUCTS:
                standalone = simulate(rows, prices, calendars, instruments, {product: {}}, test_start, end,
                                      sizing=sizing, target_policy=HoldingPolicy({product: profiles[product]}, instruments, config['rule']), **kwargs)
                yearly.append(dict(sizing=sizing, cost=cost, product=product, year='all', **standalone['metrics']))
            if sizing == 'risk' and cost == 'base':
                base_result = result
    write_csv(out / 'evaluations.csv', evaluations)
    write_csv(out / 'yearly.csv', yearly)
    dump(out / 'provenance.json', dict(daily_version=dv, metadata_version=mv, raw_rows=len(raw),
                                     source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                                     simulation_sha256=hashlib.sha256(Path(__file__).with_name('factor_simulation.py').read_bytes()).hexdigest(),
                                     test_status='2025+ reused from prior research; retrospective OOS, not untouched independent confirmation'))
    plot(out, descriptions, base_result['daily'], D(config['initial_cash']), config['rule'])
    report(out, selected, evaluations, config)
    progress(f'Completed: {evaluations[0]}', flush=True)
    return evaluations


def plot(out, descriptions, daily, initial, rule='quantile'):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(2, 2, figsize=(13, 8), constrained_layout=True)
    for product, ax in zip(PRODUCTS, axes.flat):
        group = sorted([r for r in descriptions if r['product'] == product], key=lambda r: r['dte'])
        ax.plot([r['dte'] for r in group], [100 * r['mean' if rule == 'mean_std' else 'median'] for r in group], label='Historical mean' if rule == 'mean_std' else 'Historical median')
        ax.fill_between([r['dte'] for r in group], [100 * r['low'] for r in group], [100 * r['high'] for r in group], alpha=.25, label='Mean +/- 1 std' if rule == 'mean_std' else 'Selected quantile band')
        ax.set_title(product)
        ax.set_xlabel('Calendar days to near expiry')
        ax.set_ylabel('(Next - near) / near, %')
        ax.legend()
    fig.savefig(out / 'expiry_profiles.png', dpi=150)
    plt.close(fig)
    fig, axes = plt.subplots(2, 1, figsize=(13, 8), constrained_layout=True)
    eq = np.array([float(initial)] + [float(r['equity']) for r in daily])
    dates = [r['day'] for r in daily]
    axes[0].plot(dates, eq[1:] / float(initial))
    axes[0].set_title('Expiry-conditioned mean reversion: frozen-model research NAV')
    axes[1].fill_between(dates, eq[1:] / np.maximum.accumulate(eq)[1:] - 1, 0)
    axes[1].set_title('Drawdown; hold until mean or expiry' if rule == 'mean_std' else 'Drawdown; hold until opposite signal or expiry')
    fig.savefig(out / 'net_value.png', dpi=150)
    plt.close(fig)


def report(out, selected, evaluations, config):
    def fmt(value):
        return '未定义' if value is None else f'{value:.3f}'
    lines = ['# 到期阶段条件价差均值回归',
             '\n比值＝（下月收盘价−当月收盘价）／当月收盘价，按到期自然日数建立历史分布。',
             '\n低于历史下分位数时买下月卖当月，高于上分位数时反向。中性信号保持组合和手数；'
             '反向信号次日开盘先平后反开。到期日不再开仓或反向，近月收盘按最终结算价现金结清，远月按当日收盘价加费用和不利滑点平仓。',
             '\n历史参考仅使用训练截止前已到期的完整合约周期。2024 年选型，2025 起冻结检验；'
             '该检验段已用于上一轮研究，属于重复使用的事后样本外检验，不是新的未见验证集。',
             '\n|仓位|成本|夏普|年化收益|最大回撤|成交腿次|', '|---|---|---:|---:|---:|---:|']
    for r in evaluations:
        lines.append(f'|{r["sizing"]}|{r["cost"]}|{fmt(r["sharpe"])}|{fmt(r["annual_return"])}|{fmt(r["max_drawdown"])}|{r["fills"]}|')
    lines.extend(['\n年化收益、回撤以比例保存。基准费用 0.5bp/腿/次＋1tick，压力加倍；没有新增收益模型或每日风险再平衡。',
                  '\n## 冻结参数', *[f'- {p}：{s}' for p, s in selected.items()],
                  '\n半径 r 表示用到期天数±r日的历史样本，q 为下分位数，上分位数为1−q；h0 为全部历史，h3 为最近三年。'
                  f'至少 {config["min_samples"]} 条观察和 {config["min_contracts"]} 个不同到期合约；验证期少于20次成交腿不参与选择。',
                  '\n每品种 18 个预登记试验，全部结果见 trials.csv；profiles.csv 为冻结历史分布，observations.csv 为原始比值。'
                  'yearly.csv 为年度及单品种表现，决策、成交、账务和期末持仓分别输出。',
                  '\n风险配置只在入场决定手数，正常持有期间不调整；反向沿用原手数，必要时受新增敞口上限缩量。'
                  '名义上限只约束新增交易，不为保持上限主动平掉原有组合。',
                  '\n日线开盘全额成交假设尚未验证涨跌停、容量或日内部分成交。历史真实费用、保证金、公告可知时间和独立日历未装配。'
                  '缺行情不假定成交；残余腿持续退出，到期现金结算遵守统一账本。区间结束不强平。',
                  f'\n行情版本 {config["daily_version"]}；元数据版本 {config["metadata_version"]}。',
                  '\n![到期阶段分布](expiry_profiles.png)\n\n![净值回撤](net_value.png)'])
    (out / 'REPORT.md').write_text('\n'.join(lines), encoding='utf-8')
    if config['rule'] == 'mean_std':
        text = '\n'.join(lines)
        text = text.replace('低于历史下分位数时买下月卖当月，高于上分位数时反向。中性信号保持组合和手数；反向信号次日开盘先平后反开。',
                            '低于历史均值减一个样本标准差时买下月卖当月，高于均值加一个标准差时做空价差。持仓跨回均值时次日开盘只平仓，不同次反开；其他时候保持手数。')
        text = text.replace('每品种 18 个预登记试验', '每品种 6 个预登记试验（标准差倍数固定为1）')
        text = text.replace('反向沿用原手数', '均值退出后新入场重新计算手数')
        text = text.replace('到期日不再开仓或反向', '到期日不再开仓')
        text = text.replace('q 为下分位数，上分位数为1−q；', '上下界固定为均值±一个样本标准差；')
        (out / 'REPORT.md').write_text(text, encoding='utf-8')
