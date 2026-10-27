"""Causal expanding-history, fixed one-lot reversed mean-band research."""
from datetime import timedelta
from pathlib import Path
import hashlib

from .expiry_spread_research import HoldingPolicy, build_profile, observations
from .factor_data import load_research_data
from .factor_research import csv_if, dump
from .factor_simulation import simulate
from .futures_spread import PRODUCTS
from .models import D


class ExpandingPolicy(HoldingPolicy):
    def __init__(self, rows, instruments, radius=5, min_samples=30, min_contracts=12):
        super().__init__({p: {} for p in PRODUCTS}, instruments, 'mean_std', trade_sign=-1)
        self.rows = {p: [r for r in rows if r['product'] == p and r['near'] in instruments]
                     for p in PRODUCTS}
        self.radius, self.min_samples, self.min_contracts = radius, min_samples, min_contracts
        self.cache = {}
        self.references = []

    def __call__(self, product, day, row, *args):
        if row:
            boundary = row['day'] - timedelta(days=1)
            completed = max((self.instruments[r['near']].expiry for r in self.rows[product]
                             if r['day'] <= boundary and self.instruments[r['near']].expiry <= boundary), default=None)
            key = product, completed
            if key not in self.cache:
                profile = build_profile(self.rows[product], self.instruments, boundary, self.radius, .2, 0,
                                        self.min_samples, self.min_contracts)
                for band in profile.values():
                    band['low'], band['high'] = band['mean'] - band['std'], band['mean'] + band['std']
                    self.references.append(dict(product=product, available_from=row['day'],
                                                last_completed_expiry=completed, **band))
                self.cache[key] = profile
            self.profiles[product] = self.cache[key]
        return super().__call__(product, day, row, *args)


def run(output='outputs/index_futures_full_history_reverse'):
    out = Path(output)
    out.mkdir(parents=True, exist_ok=True)
    dv, mv, raw, facts, instruments = load_research_data('data/index_futures')
    rows, prices, calendars = observations(raw, facts, instruments)
    config = dict(daily_version=dv, metadata_version=mv, radius=5, min_samples=30, min_contracts=12,
                  history='expanding completed cycles; expiry strictly before signal day',
                  trade_sign=-1, sizing='one', initial_cash='10000000', fee_rate='0.00005', slippage_ticks=1)
    dump(out / 'config.json', config)
    policy = ExpandingPolicy(rows, instruments)
    print('Running all products, full history, one lot per leg', flush=True)
    result = simulate(rows, prices, calendars, instruments, {p: {} for p in PRODUCTS},
                      min(r['day'] for r in rows), max(r['day'] for r in rows), sizing='one', details=True,
                      initial_cash=config['initial_cash'], fee_rate=config['fee_rate'], slip=1,
                      nominal_cap=1e9, target_policy=policy)
    for name in ('daily', 'fills', 'entries', 'decisions', 'warnings'):
        csv_if(out / f'{name}.csv', result[name])
    csv_if(out / 'references.csv', policy.references)
    dump(out / 'terminal_positions.json', result['terminal_positions'])
    totals, yearly = [], []
    for product in PRODUCTS:
        fills = [f for f in result['fills'] if f['group'] == product]
        net = sum((r[f'pnl_{product}'] for r in result['daily']), D(0))
        fees = sum((f['fee'] for f in fills), D(0))
        slip = sum((instruments[f['instrument']].tick * instruments[f['instrument']].multiplier * f['quantity']
                    for f in fills), D(0))
        totals.append(dict(product=product, data_start=min(calendars[product]), data_end=max(calendars[product]),
                           first_entry=min((f['day'] for f in fills if f['effect'] == 'open'), default=None),
                           net_pnl=net, fees=fees, slippage=slip, gross_pnl=net+fees+slip,
                           pair_entries=sum(f['effect'] == 'open' for f in fills)//2, leg_fills=len(fills)))
        for year in sorted({r['day'].year for r in result['daily']}):
            yearly.append(dict(product=product, year=year,
                               net_pnl=sum((r[f'pnl_{product}'] for r in result['daily'] if r['day'].year == year), D(0))))
    assert sum((r['net_pnl'] for r in totals), D(0)) == result['daily'][-1]['equity'] - D(config['initial_cash'])
    assert all(f['quantity'] == 1 for f in result['fills'])
    csv_if(out / 'pnl_by_product.csv', totals)
    csv_if(out / 'yearly.csv', yearly)
    dump(out / 'provenance.json', dict(raw_rows=len(raw), **config,
         source_hashes={name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                        for name in ('full_history_spread.py', 'expiry_spread_research.py', 'factor_simulation.py')}))
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(13, 6), constrained_layout=True)
    for product in PRODUCTS:
        cumulative, values = D(0), []
        for r in result['daily']:
            cumulative += r[f'pnl_{product}']
            values.append(float(cumulative))
        ax.plot([r['day'] for r in result['daily']], values, label=product)
    ax.axhline(0, color='gray', linewidth=.7)
    ax.set_ylabel('Cumulative net PnL (CNY)')
    ax.set_title('Full-history reversed mean-band strategy; one lot per leg')
    ax.legend()
    fig.savefig(out / 'pnl.png', dpi=150)
    plt.close(fig)
    lines = ['# 全历史反向价差策略：固定一手', '',
             '高于条件均值＋1σ买远卖近，低于均值−1σ卖远买近；回到均值后次日开盘平仓，不同时反向。',
             '参考按同品种到期自然日数±5天统计，只用信号日前已到期的完整周期，累计历史；至少30观察、12合约。每腿一手，不调仓、不展期。',
             '均值随此前完成周期更新；预热期间空仓。四品种全参与，不再使用验证期选型或品种筛选。全历史是事后探索结果，不是独立样本外验证。',
             '每腿每次成交费用0.5bp＋1tick不利滑点；统一1000万资金池，按结算价每日记账。到期近腿现金结算，远腿同日收盘平仓；期末仅估值，未平仓盈亏计入净PnL。', '',
             '|品种|数据起止|首次开仓|组合开仓次数|净PnL（元）|费用（元）|滑点（元）|',
             '|---|---|---|---:|---:|---:|---:|']
    for r in totals:
        lines.append(f"|{r['product']}|{r['data_start']}～{r['data_end']}|{r['first_entry']}|{r['pair_entries']}|{r['net_pnl']:.2f}|{r['fees']:.2f}|{r['slippage']:.2f}|")
    lines += ['', f"组合净PnL：{sum((r['net_pnl'] for r in totals), D(0)):.2f}元。", '',
              f'行情快照：{dv}；元数据快照：{mv}。',
              '历史公告可知时间、独立日历未完整核实；日线开盘全额成交是假设，不代表真实可成交性。费用是研究假设。原始数据只读。',
              'pnl_by_product.csv和yearly.csv提供归因；daily/fills/entries/decisions/references支持逐笔核对。']
    (out / 'report.md').write_text('\n'.join(lines), encoding='utf-8')
    print(totals, flush=True)
    return totals
