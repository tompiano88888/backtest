"""Registered experiments and held-out evaluation of index-futures spreads."""
from __future__ import annotations

from datetime import date, timedelta
import hashlib
import importlib.metadata
import json
from pathlib import Path

import numpy as np

from .factor_data import WINDOWS, load_research_data, make_features, feature_names, fit_model, predict
from .factor_simulation import simulate, summary
from .futures_spread import PRODUCTS, write_csv
from .models import D
from .storage import json_default

DEFAULTS = dict(root='data/index_futures', output='outputs/index_futures_factor_research',
                daily_version=None, metadata_version=None, windows=list(WINDOWS),
                train_end='2023-12-31', validation_end='2024-12-31', test_start='2025-01-01',
                initial_cash='10000000', fee_rate='0.00005', slippage_ticks=1,
                target_vol=.10, nominal_cap=2, bootstrap_samples=500, seed=20261009)


def registry(windows):
    trials = [dict(id=f'mean_reversion_t{t}', family='mean_reversion', n=20, alpha=1., threshold=t) for t in (1, 2)]
    for family in ('return', 'amplitude', 'expiry', 'volatility', 'all'):
        for n in windows:
            for alpha in (.1, 1., 10.):
                for threshold in (1, 2):
                    trials.append(dict(id=f'{family}_n{n}_a{alpha}_t{threshold}', family=family,
                                       n=n, alpha=alpha, threshold=threshold))
    return trials


def dump(path, value):
    Path(path).write_text(json.dumps(value, default=json_default, ensure_ascii=False, sort_keys=True, indent=2,
                                    allow_nan=False), encoding='utf-8')


def csv_if(path, rows):
    if rows:
        write_csv(path, rows)
    else:
        Path(path).write_text('', encoding='utf-8-sig')


def training_rows(group, boundary, embargo=10):
    before = [r for r in group if r['day'] <= boundary]
    if len(before) <= embargo:
        return []
    cutoff = before[-embargo]['day']
    return [r for r in before if r['day'] < cutoff and r['label_end_1'] is not None and r['label_end_1'] < cutoff]


def ranks(values):
    values = np.asarray(values)
    order = np.argsort(values, kind='stable')
    result = np.empty(len(values), dtype=float)
    i = 0
    while i < len(values):
        j = i + 1
        while j < len(values) and values[order[j]] == values[order[i]]:
            j += 1
        result[order[i:j]] = (i + j - 1) / 2
        i = j
    return result


def factor_diagnostics(features, config):
    correlations, grouped = [], []
    train_end, val_end = date.fromisoformat(config['train_end']), date.fromisoformat(config['validation_end'])
    fields = ['z', 'expiry_days'] + [f'{name}_{n}' for n in config['windows'] for name in ('return', 'amplitude', 'volatility')]
    for product in PRODUCTS:
        group = [r for r in features if r['product'] == product]
        splits = [('train', date.min, train_end), ('validation', train_end + timedelta(days=1), val_end),
                  ('test', date.fromisoformat(config['test_start']), date.max)]
        for field in fields:
            train_values = [r[field] for r in training_rows(group, train_end) if np.isfinite(r[field])]
            if not train_values:
                continue
            cuts = np.quantile(train_values, [.2, .4, .6, .8])
            for horizon in (1, 5, 10):
                for split, start, end in splits:
                    valid = [r for r in group if start <= r['day'] <= end and r[f'label_end_{horizon}'] is not None
                             and r[f'label_end_{horizon}'] <= end and r['main_complete']
                             and np.isfinite([r[field], r[f'label_{horizon}']]).all()]
                    x = np.array([r[field] for r in valid])
                    y = np.array([r[f'label_{horizon}'] for r in valid])
                    ic = None
                    if len(x) > 2 and np.std(x) > 0 and np.std(y) > 0:
                        ic = float(np.corrcoef(ranks(x), ranks(y))[0, 1])
                    correlations.append(dict(product=product, feature=field, horizon=horizon, split=split,
                                             year='all', count=len(valid), spearman=ic))
                    for year in sorted({r['day'].year for r in valid}):
                        subset = [r for r in valid if r['day'].year == year]
                        xx, yy = [r[field] for r in subset], [r[f'label_{horizon}'] for r in subset]
                        yearly = float(np.corrcoef(ranks(xx), ranks(yy))[0, 1]) if len(xx) > 2 and np.std(xx) > 0 and np.std(yy) > 0 else None
                        correlations.append(dict(product=product, feature=field, horizon=horizon, split=split,
                                                 year=year, count=len(subset), spearman=yearly))
                    bins = np.searchsorted(cuts, x, side='right')
                    for bin_id in range(5):
                        sample = y[bins == bin_id]
                        grouped.append(dict(product=product, feature=field, horizon=horizon, split=split,
                                            quintile=bin_id + 1, count=len(sample),
                                            mean_gross_pnl=float(sample.mean()) if len(sample) else None))
    return correlations, grouped


def sharpe_interval(daily, samples, seed):
    returns = np.array([float(r['return']) for r in daily])
    if len(returns) < 40 or returns.std(ddof=1) == 0:
        return [None, None]
    rng = np.random.default_rng(seed)
    result = []
    for _ in range(samples):
        starts = rng.integers(0, len(returns), size=(len(returns) + 19) // 20)
        sample = np.concatenate([returns[(start + np.arange(20)) % len(returns)] for start in starts])[:len(returns)]
        if sample.std(ddof=1) > 0:
            result.append(sample.mean() / sample.std(ddof=1) * np.sqrt(252))
    return [float(v) for v in np.quantile(result, [.025, .975])] if result else [None, None]


def run_research(overrides=None, progress=print):
    config = DEFAULTS | (overrides or {})
    if set(config) - set(DEFAULTS):
        raise ValueError('Unknown research configuration keys')
    if any(type(n) is not int or n < 2 for n in config['windows']) or 20 not in config['windows']:
        raise ValueError('Windows must include 20 and contain integers >=2')
    if config['bootstrap_samples'] < 1 or D(config['initial_cash']) <= 0 or D(config['fee_rate']) < 0 or config['slippage_ticks'] < 0:
        raise ValueError('Invalid cash, costs or bootstrap samples')
    train_end, val_end, test_start = map(date.fromisoformat, [config['train_end'], config['validation_end'], config['test_start']])
    if not train_end < val_end < test_start:
        raise ValueError('Training, validation and test dates must be ordered')
    output = Path(config['output'])
    output.mkdir(parents=True, exist_ok=True)
    daily_version, metadata_version, raw, facts, instruments = load_research_data(config['root'], config['daily_version'], config['metadata_version'])
    config.update(daily_version=daily_version, metadata_version=metadata_version)
    trials = registry(config['windows'])
    dump(output / 'config.json', config)
    dump(output / 'registry.json', trials)
    provenance = dict(daily_version=daily_version, metadata_version=metadata_version, raw_rows=len(raw),
                      source_hashes={p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(__file__).parent.glob('factor_*.py')},
                      dependencies={p: importlib.metadata.version(p) for p in ('numpy', 'matplotlib', 'duckdb', 'pyarrow')},
                      limitations=['Daily open executions assumed; no open auction volume, historical limits or intraday leg sequencing.',
                                   'Lifecycle facts retrieved now; historical announcement known_at not reconstructed.',
                                   'Observed calendar union cannot detect market-wide missing dates.',
                                   'Fees and slippage are scenarios, not verified historical tariffs.',
                                   'Historical margin unavailable: no fabricated margin/risk ratio; cash deficits reported.',
                                   'No guaranteed profit or risk-free arbitrage; terminal holdings are marked, not forcibly closed.'])
    dump(output / 'provenance.json', provenance)
    features, prices, calendars = make_features(raw, facts, instruments, config['windows'])
    write_csv(output / 'features.csv', features)
    ic, groups = factor_diagnostics(features, config)
    write_csv(output / 'factor_ic.csv', ic)
    write_csv(output / 'factor_groups.csv', groups)
    latest = max(r['day'] for r in features)
    val_start = train_end + timedelta(days=1)
    args = dict(initial_cash=config['initial_cash'], fee_rate=config['fee_rate'], slip=config['slippage_ticks'],
                target_vol=config['target_vol'], nominal_cap=config['nominal_cap'])
    selections, trial_results, family_models = {}, [], {}
    for product in PRODUCTS:
        group = [r for r in features if r['product'] == product]
        train = training_rows(group, train_end)
        winners = {}
        # End validation before the test boundary, with a 10-observed-day isolation gap.
        validation_days = [d for d in calendars[product] if val_start <= d <= val_end]
        validation_last = validation_days[-11] if len(validation_days) > 10 else val_start
        for number, trial in enumerate(trials):
            model = fit_model(train, feature_names(trial['family'], trial['n']), trial['alpha'], trial['family'] == 'mean_reversion')
            selected = dict(model=model, threshold=trial['threshold'])
            result = simulate(features, prices, calendars, instruments, {product: selected}, val_start, validation_last, **args)
            record = dict(product=product, **trial, training_samples=model['training_samples'] if model else 0, **result['metrics'])
            trial_results.append(record)
            score = record['sharpe'] if record['sharpe'] is not None and model is not None else -float('inf')
            # No trade count reward or test-set data participates in selection.
            if record['fills'] < 20:
                score = -float('inf')
            family = trial['family']
            if family not in winners or score > winners[family][0]:
                winners[family] = (score, trial, selected)
            if (number + 1) % 30 == 0:
                progress(f'{product}: {number + 1}/{len(trials)} validation experiments', flush=True)
        if all(not np.isfinite(w[0]) for w in winners.values()):
            selections[product] = dict(model=None, threshold=1, trial_id='no_eligible_model')
        else:
            best = max(winners.values(), key=lambda w: w[0])
            selections[product] = best[2] | dict(trial_id=best[1]['id'], validation_sharpe=best[0])
        for family, (score, trial, selected) in winners.items():
            family_models.setdefault(family, {})[product] = (selected if np.isfinite(score) else dict(model=None, threshold=1))
        progress(f'{product}: frozen {selections[product]["trial_id"]}', flush=True)
    write_csv(output / 'trials.csv', trial_results)
    dump(output / 'selected_models.json', selections)
    evaluations, yearly = [], []
    scenarios = [('selected', selections), ('carry', {p: dict(carry=True) for p in PRODUCTS})] + sorted(family_models.items())
    selected_daily = None
    predictions = []
    for name, models in scenarios:
        for sizing in ('risk', 'one'):
            for cost in ('base', 'stress'):
                kwargs = args | (dict(fee_rate=str(D(config['fee_rate']) * 2), slip=config['slippage_ticks'] * 2,
                                     decision_fee_rate=config['fee_rate'], decision_slip=config['slippage_ticks']) if cost == 'stress' else {})
                result = simulate(features, prices, calendars, instruments, models, test_start, latest,
                                  sizing=sizing, details=True, **kwargs)
                label = f'{name}_{sizing}_{cost}'
                interval = sharpe_interval(result['daily'], config['bootstrap_samples'], config['seed'])
                evaluations.append(dict(strategy=name, sizing=sizing, cost=cost, **result['metrics'],
                                        sharpe_ci_low=interval[0], sharpe_ci_high=interval[1]))
                for kind in ('daily', 'fills', 'entries', 'decisions', 'warnings'):
                    csv_if(output / f'{label}_{kind}.csv', result[kind])
                dump(output / f'{label}_terminal_positions.json', result['terminal_positions'])
                for year in sorted({r['day'].year for r in result['daily']}):
                    subset = [r for r in result['daily'] if r['day'].year == year]
                    year_initial = subset[0]['equity'] / (1 + subset[0]['return'])
                    yearly.append(dict(strategy=name, sizing=sizing, cost=cost, product='portfolio', year=year,
                                       **summary(subset, year_initial)))
                # Standalone products use identical frozen models, own unified research pool.
                for product in PRODUCTS:
                    standalone = simulate(features, prices, calendars, instruments, {product: models[product]},
                                          test_start, latest, sizing=sizing, **kwargs)
                    yearly.append(dict(strategy=name, sizing=sizing, cost=cost, product=product, year='all',
                                       **standalone['metrics']))
                if name == 'selected' and sizing == 'risk' and cost == 'base':
                    selected_daily = result['daily']
                if name == 'selected' and sizing == 'one' and cost == 'base':
                    predictions = result['decisions']
    write_csv(output / 'evaluations.csv', evaluations)
    write_csv(output / 'yearly.csv', yearly)
    csv_if(output / 'predictions.csv', predictions)
    # Prespecified annual checks: never apply a family selected using 2024 to earlier years.
    rolling = []
    for year in range(2017, train_end.year + 1):
        models = {}
        for product in PRODUCTS:
            train = training_rows([r for r in features if r['product'] == product], date(year - 1, 12, 31))
            models[product] = dict(model=fit_model(train, feature_names('all', 20), 1.), threshold=1)
        result = simulate(features, prices, calendars, instruments, models, date(year, 1, 1), date(year, 12, 31), **args)
        rolling.append(dict(year=year, specification='all_n20_alpha1_threshold1_prespecified', **result['metrics']))
    write_csv(output / 'rolling.csv', rolling)
    plot_results(output, evaluations, selected_daily, groups)
    selected = next(r for r in evaluations if r['strategy'] == 'selected' and r['sizing'] == 'risk' and r['cost'] == 'base')
    stress = next(r for r in evaluations if r['strategy'] == 'selected' and r['sizing'] == 'risk' and r['cost'] == 'stress')
    report(output, selected, stress, evaluations, selections, provenance, ic)
    progress(f'Completed: selected OOS Sharpe {selected["sharpe"]}', flush=True)
    return dict(selected=selected, stress=stress, config=config)


def plot_results(output, evaluations, daily, groups):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(2, 1, figsize=(13, 8), constrained_layout=True)
    dates = [r['day'] for r in daily]
    initial = float(daily[0]['equity'] / (1 + daily[0]['return']))
    equity = np.array([initial] + [float(r['equity']) for r in daily])
    axes[0].plot(dates, equity[1:] / initial)
    axes[0].set_title('Frozen selected strategy: out-of-sample net NAV')
    axes[1].fill_between(dates, equity[1:] / np.maximum.accumulate(equity)[1:] - 1, 0)
    axes[1].set_title('Drawdown; daily open research execution assumptions')
    fig.savefig(output / 'net_value.png', dpi=150)
    plt.close(fig)
    fig, ax = plt.subplots(figsize=(13, 5), constrained_layout=True)
    rows = [r for r in evaluations if r['sizing'] == 'risk' and r['cost'] == 'base']
    ax.bar([r['strategy'] for r in rows], [r['sharpe'] or 0 for r in rows])
    ax.axhline(1.5, color='red', linestyle='--', label='Research target 1.5')
    ax.set_title('Held-out Sharpe; family comparisons are diagnostics, not reselection')
    ax.legend()
    fig.savefig(output / 'strategy_comparison.png', dpi=150)
    plt.close(fig)
    fig, axes = plt.subplots(2, 2, figsize=(12, 8), constrained_layout=True)
    for product, ax in zip(PRODUCTS, axes.flat):
        for feature in ('return_20', 'amplitude_20', 'expiry_days', 'volatility_20'):
            values = [r for r in groups if r['product'] == product and r['feature'] == feature and r['horizon'] == 1 and r['split'] == 'test']
            ax.plot([r['quintile'] for r in values], [r['mean_gross_pnl'] if r['mean_gross_pnl'] is not None else np.nan for r in values], label=feature)
        ax.set_title(f'{product}: OOS 1-day gross PnL by train-defined quintile')
        ax.axhline(0, color='grey', linewidth=.5)
        ax.legend(fontsize=8)
    fig.savefig(output / 'factor_groups.png', dpi=150)
    plt.close(fig)


def report(output, selected, stress, evaluations, selections, provenance, correlations):
    def number(value):
        return '未定义' if value is None else f'{value:.3f}'
    passed = selected['sharpe'] is not None and selected['sharpe'] >= 1.5
    text = ['# 股指期货四因子跨期研究',
            f'\n冻结选择策略样本外夏普：**{number(selected["sharpe"])}**；高夏普目标：**{"达到" if passed else "未达到"}**。',
            f'\n压力成本夏普：{number(stress["sharpe"])}；基准夏普 95% 区间：'
            f'[{number(selected["sharpe_ci_low"])}, {number(selected["sharpe_ci_high"])}]（20 日循环区块 bootstrap，非独立正态假设）。',
            '\n训练截至 2023 年末、2024 年验证选型、2025 年起最终检验；边界隔离 10 个观察交易日。'
            '最终模型系数来自训练期，未用 2024 年标签重新拟合，也未根据检验期重选。',
            '\n|策略|仓位|成本|夏普|年化收益|最大回撤|成交腿次|', '|---|---|---|---:|---:|---:|---:|']
    for r in evaluations:
        text.append(f'|{r["strategy"]}|{r["sizing"]}|{r["cost"]}|{number(r["sharpe"])}|{number(r["annual_return"])}|{number(r["max_drawdown"])}|{r["fills"]}|')
    text.extend(['\n年化收益和回撤以比例显示（0.01＝1%）。保留空仓交易日；期末只估值，不强平。'
                 '压力情景冻结模型及基准信号门槛，仅提高实际成交费用和滑点；权益变化会影响风险仓位。',
                 '\n## 冻结选择', *[f'- {p}：{s["trial_id"]}' for p, s in selections.items()],
                 '\n## 因子与复现材料',
                 'factor_ic.csv 包含各窗口、各预测周期、训练／验证／检验及年度秩相关；factor_groups.csv 使用训练期分位边界。'
                 '这两项是固定两腿的毛收益诊断，不能替代扣成本的账户收益。',
                 '\ntrials.csv 保存全部参数试验，registry.json 为预注册范围；evaluations.csv 为全部冻结家族的检验。'
                 '检验期家族排行仅作诊断，不能据此替换 selected。rolling.csv 为此前年份预设 N20/alpha1/threshold1 四因子年度滚动检查，未使用 2024 年选出的参数。',
                 '\n各策略输出 daily、fills、entries、decisions、warnings 和 terminal_positions，支持账务核对；yearly.csv 包含年度及单品种表现。',
                 '\n## 成本和执行假设',
                 '基准每腿每次成交 0.5bp＋1 tick，压力 1bp＋2 tick；主力持仓量最大，同量依次比较成交量和较近月份。'
                 '两腿等手数。名义敞口上限为策略限制，不以保证金拒单；历史保证金缺失，未虚构保证金或风险度。'
                 '风险预算按四品种各四分之一方差预算分配，组合相关性可能使实际波动偏离 10%。',
                 '\n日线开盘按全额成交假设，缺开盘价不成交；已有残余腿持续退出。未模拟开盘队列、涨跌停排队、参与率与日内部分成交状态机。'
                 '这些是研究候选结果，不是实盘或完整 Engine 撮合验收。',
                 f'\n行情版本：{provenance["daily_version"]}；元数据版本：{provenance["metadata_version"]}。',
                 '\n历史公告可知时间未装配；独立交易日历未审计，不能发现全市场同时缺失的日期。',
                 '\n![净值与回撤](net_value.png)\n\n![策略比较](strategy_comparison.png)\n\n![因子分组](factor_groups.png)'])
    text.extend(['\n## 四因子诊断（预设 N20，一日毛收益）',
                 '|品种|因子|训练秩相关|验证秩相关|检验秩相关|', '|---|---|---:|---:|---:|'])
    for product in PRODUCTS:
        for field, label in [('return_20', '涨幅'), ('amplitude_20', '振幅'), ('expiry_days', '到期天数'), ('volatility_20', '波动率')]:
            values = {}
            for split in ('train', 'validation', 'test'):
                found = next((r for r in correlations if r['product'] == product and r['feature'] == field
                              and r['horizon'] == 1 and r['split'] == split and r['year'] == 'all'), None)
                values[split] = found['spearman'] if found else None
            text.append(f'|{product}|{label}|{number(values["train"])}|{number(values["validation"])}|{number(values["test"])}|')
    text.append('\n相关性方向是否跨阶段一致与扣成本后增量表现需一起判断，不能把单一高相关值等同于有效策略。'
                '验证期少于 20 次成交腿的模型不参与选择，避免由极少交易主导夏普；无合格模型的品种保持空仓。')
    (output / 'REPORT.md').write_text('\n'.join(text), encoding='utf-8')
