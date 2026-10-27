"""Reverse actual executed legs at fixed dates/quantities, recomputing all costs."""
from __future__ import annotations

from collections import defaultdict
from datetime import date
import csv
import hashlib
import json
from pathlib import Path

from .accounting import Ledger
from .factor_data import load_research_data
from .factor_research import csv_if, dump, sharpe_interval
from .factor_simulation import summary
from .models import D, Fill, Instruction


def replay_reverse(original_daily, original_fills, prices, instruments, initial_cash,
                   fee_rate, slippage_ticks):
    ledger = Ledger(instruments, initial_cash)
    scheduled = defaultdict(list)
    for row in original_fills:
        scheduled[row['day']].append(row)
    daily, fills, cost_checks = [], [], []
    previous = D(initial_cash)
    original_fee = reverse_fee = slip_cost = D(0)
    for original_day in original_daily:
        day = date.fromisoformat(str(original_day['day']))
        day_fees = day_notional = D(0)
        for original in scheduled[str(day)]:
            code, effect, group = original['instrument'], original['effect'], original['group']
            side, qty = int(original['side']), int(original['quantity'])
            old_order = Instruction(code, side, qty, effect, group)
            new_order = Instruction(code, -side, qty, effect, group)
            tick_cost = instruments[code].tick * slippage_ticks
            raw_price = D(original['price']) - (1 if old_order.buy else -1) * tick_cost
            new_price = raw_price + (1 if new_order.buy else -1) * tick_cost
            fee = new_price * qty * instruments[code].multiplier * D(fee_rate)
            ledger.apply(Fill(day, day, new_order, new_price, fee))
            day_fees += fee
            day_notional += new_price * qty * instruments[code].multiplier
            original_fee += D(original['fee'])
            reverse_fee += fee
            slip_cost += tick_cost * qty * instruments[code].multiplier
            fills.append(dict(day=day, instrument=code, side=-side, quantity=qty, effect=effect,
                              group=group, price=new_price, fee=fee, raw_price=raw_price))
        for code in sorted({p.instrument for p in ledger.lots}):
            row = prices.get((code, day))
            if row is None or row.get('settlement_price') is None:
                raise ValueError(f'Missing held-contract settlement: {code}/{day}')
            ledger.settle(code, day, row['settlement_price'])
        equity = D(initial_cash) + sum(ledger.group_pnl().values(), D(0))
        assert equity == ledger.cash
        forward_pnl = D(original_day['equity']) - D(initial_cash)
        identity = equity - D(initial_cash) + forward_pnl + original_fee + reverse_fee + 2 * slip_cost
        if abs(identity) > D('0.000000001'):
            raise AssertionError(f'Gross-PnL/cost reversal identity mismatch: {day}/{identity}')
        cost_checks.append(dict(day=day, forward_pnl=forward_pnl, reverse_pnl=equity - D(initial_cash),
                                total_fees=original_fee + reverse_fee, total_slippage=2 * slip_cost,
                                identity_error=identity))
        nominal = sum((abs(ledger.marks[p.instrument]) * p.quantity * instruments[p.instrument].multiplier for p in ledger.lots), D(0))
        daily.append(dict(day=day, equity=equity, cash=ledger.cash, return_=(equity - previous) / previous,
                          fees=day_fees, traded_notional=day_notional, nominal=nominal, fills=len(scheduled[str(day)]),
                          cash_deficit=max(-ledger.cash, D(0))))
        daily[-1]['return'] = daily[-1].pop('return_')
        previous = equity
    return dict(metrics=summary(daily, D(initial_cash)), daily=daily, fills=fills,
                entries=ledger.entries, cost_checks=cost_checks)


def run(source='outputs/index_futures_mean_std_research', output='outputs/index_futures_reverse_research'):
    source, output = Path(source), Path(output)
    config = json.loads((source / 'config.json').read_text())
    dv, mv, raw, facts, instruments = load_research_data(config['root'], config['daily_version'], config['metadata_version'])
    prices = {(r['instrument'], r['trading_date']): r for r in raw}
    output.mkdir(parents=True, exist_ok=True)
    results, hashes = [], {}
    for name in ('risk_base', 'risk_stress', 'one_base', 'one_stress', 'same_reference'):
        daily_path, fills_path = source / f'{name}_daily.csv', source / f'{name}_fills.csv'
        if not daily_path.exists() or not fills_path.exists():
            continue
        with daily_path.open(encoding='utf-8-sig', newline='') as handle:
            original_daily = list(csv.DictReader(handle))
        with fills_path.open(encoding='utf-8-sig', newline='') as handle:
            original_fills = list(csv.DictReader(handle))
        for path in (daily_path, fills_path):
            hashes[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        stress = name.endswith('stress')
        result = replay_reverse(original_daily, original_fills, prices, instruments, config['initial_cash'],
                                D(config['fee_rate']) * (2 if stress else 1), config['slippage_ticks'] * (2 if stress else 1))
        forward = summary([{k: (D(r[k]) if k in ('equity', 'return', 'fees', 'traded_notional', 'nominal', 'cash_deficit') else
                                    int(r[k]) if k == 'fills' else r[k]) for k in r} for r in original_daily], D(config['initial_cash']))
        interval = sharpe_interval(result['daily'], config['bootstrap_samples'], config['seed'])
        row = dict(scenario=name, forward_sharpe=forward['sharpe'], **result['metrics'],
                   sharpe_ci_low=interval[0], sharpe_ci_high=interval[1],
                   entries=len({(f['day'], f['group']) for f in original_fills if f['effect'] == 'open'}))
        results.append(row)
        for kind in ('daily', 'fills', 'entries', 'cost_checks'):
            csv_if(output / f'{name}_{kind}.csv', result[kind])
        print(name, row, flush=True)
    csv_if(output / 'evaluations.csv', results)
    dump(output / 'provenance.json', dict(daily_version=dv, metadata_version=mv, source=str(source),
                                        original_execution_sha256=hashes,
                                        policy='exact original dates and integer quantities; swap sides; recompute fees and adverse slippage',
                                        source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                                        status='post-hoc reversal motivated by observed losses; not independent validation'))
    lines = ['# 固定成交的反向对照', '\n完全保留原始成交日期、合约、手数、均值退出与到期处理，仅交换多空方向。费用与不利滑点重新计算。'
             '风险手数也固定原始值，用于隔离方向影响；不是根据反向策略权益重新计算仓位。',
             '\n|情景|原夏普|反向夏普|反向年化|反向最大回撤|开仓次数|', '|---|---:|---:|---:|---:|---:|']
    for r in results:
        lines.append(f'|{r["scenario"]}|{r["forward_sharpe"]:.3f}|{r["sharpe"]:.3f}|{r["annual_return"]*100:.3f}%|{-r["max_drawdown"]*100:.3f}%|{r["entries"]}|')
    lines.extend(['\n每个观察交易日校验：原净盈亏＋反向净盈亏＋两边费用＋两边不利滑点＝0。近月最终现金结算继续使用结算价，远月到期退出保持原成交时间和原始价格字段。',
                  '\n反向思路是在看到亏损之后提出的，同一历史区间的盈利不能视作新的独立验证。原结果也不能证明“稳定亏损”。'
                  '日线成交、情景费率、历史PIT及交易日历限制沿用前次报告。'])
    (output / 'REPORT.md').write_text('\n'.join(lines), encoding='utf-8')
    plot(output, results)
    return results


def plot(output, results):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(12, 5), constrained_layout=True)
    for name in ('risk_base', 'risk_stress', 'same_reference'):
        path = output / f'{name}_daily.csv'
        if not path.exists():
            continue
        with path.open(encoding='utf-8-sig') as handle:
            rows = list(csv.DictReader(handle))
        initial = float(D(rows[0]['equity']) / (1 + D(rows[0]['return'])))
        ax.plot([date.fromisoformat(r['day']) for r in rows], [float(r['equity']) / initial for r in rows], label=name)
    ax.set_title('Post-hoc reversed trades: net NAV at fixed original quantities')
    ax.legend()
    fig.savefig(output / 'net_value.png', dpi=150)
    plt.close(fig)
