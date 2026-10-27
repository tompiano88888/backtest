"""Causal expiry normalization, same-pair linking and registered entry studies."""
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
import hashlib
import math

import numpy as np

from .expiry_spread_research import HoldingPolicy, build_profile, observations
from .factor_data import load_research_data
from .factor_research import csv_if, dump, ranks
from .factor_simulation import simulate
from .futures_spread import PRODUCTS
from .models import D


def normalized(ratio, dte, profile):
    band = profile.get(dte)
    if band is None or not np.isfinite(ratio) or band['std'] <= 0:
        return math.nan
    return (ratio - band['mean']) / band['std']


def ratio_at(prices, near, far, day):
    a, b = prices.get((near, day)), prices.get((far, day))
    if not a or not b or a.get('close') is None or b.get('close') is None or a['close'] <= 0:
        return math.nan
    return float((b['close'] - a['close']) / a['close'])


def transform(rows, prices, calendars, instruments, radius=5, min_samples=30, min_contracts=12):
    """Each delta uses one actual pair and one currently known reference curve."""
    output, references = [], []
    for product in PRODUCTS:
        group = sorted((r for r in rows if r['product'] == product), key=lambda r: r['day'])
        available = [r for r in group if r['near'] in instruments]
        cache, path, residual_path, segment, last_day, previous_pair = {}, [], [], 0, None, None
        for row in group:
            r = dict(row)
            day, near, far = r['day'], r['near'], r['far']
            boundary = day - timedelta(days=1)
            latest = max((instruments[x['near']].expiry for x in available
                          if x['day'] <= boundary and instruments[x['near']].expiry <= boundary), default=None)
            if latest not in cache:
                cache[latest] = build_profile(available, instruments, boundary, radius, .2, 0,
                                             min_samples, min_contracts)
                references.extend(dict(product=product, available_from=day, last_expiry=latest, **b)
                                  for b in cache[latest].values())
            profile = cache[latest]
            z = normalized(r['relative_spread'], r['expiry_days'], profile)
            previous_z = math.nan
            if last_day is not None and near in instruments:
                previous_z = normalized(ratio_at(prices, near, far, last_day),
                                        (instruments[near].expiry - last_day).days, profile)
            delta = z - previous_z
            linked = math.nan
            if np.isfinite(z):
                if path and np.isfinite(delta):
                    linked = path[-1] + delta
                else:
                    segment += 1
                    path = []
                    linked = 0.0  # arbitrary segment origin; never interpret as equilibrium
                path.append(linked)
            else:
                path = []
            if np.isfinite(z):
                residual_path.append(z)
            else:
                residual_path = []
            r.update(expiry_z=z, linked=linked, linked_delta=delta, segment=segment,
                     roll=previous_pair is not None and previous_pair != (near, far),
                     reference_mean=profile.get(r['expiry_days'], {}).get('mean'),
                     reference_std=profile.get(r['expiry_days'], {}).get('std'),
                     reference_count=profile.get(r['expiry_days'], {}).get('count', 0))
            for n in (5, 20, 60):
                r[f'momentum_{n}'] = r[f'reversion_{n}'] = math.nan
                if len(residual_path) >= n + 1:
                    history = np.array(residual_path[-n-1:])
                    change_std = np.diff(history).std(ddof=1)
                    level_std = history.std(ddof=1)
                    if change_std > 0:
                        r[f'momentum_{n}'] = (history[-1] - history[0]) / (change_std * math.sqrt(n))
                    if level_std > 0:
                        r[f'reversion_{n}'] = -(history[-1] - history.mean()) / level_std
            index = calendars[product].index(day)
            for h in (1, 5, 10):
                r[f'forward_{h}'], r[f'forward_end_{h}'] = math.nan, None
                end_index = index + 1 + h
                if near in instruments and end_index < len(calendars[product]):
                    begin, end = calendars[product][index+1], calendars[product][end_index]
                    quotes = [prices.get((c, d)) for c in (near, far) for d in (begin, end)]
                    if end <= instruments[near].expiry and all(q and q.get('open') is not None for q in quotes):
                        a, b, c, e = quotes
                        r[f'forward_{h}'] = float(((e['open']-c['open'])-(b['open']-a['open'])) * instruments[near].multiplier)
                        r[f'forward_end_{h}'] = end
            output.append(r)
            last_day, previous_pair = day, (near, far)
    return output, references


def experiments():
    return [dict(id=f'{family}_{n}_{t}', family=family, n=n, threshold=t)
            for family in ('momentum', 'reversion') for n in (5, 20, 60) for t in (.5, 1., 2.)] + [
                dict(id=f'expiry_reversion_{t}', family='expiry_reversion', n=0, threshold=t) for t in (.5, 1., 2.)]


def score(row, spec):
    return -row['expiry_z'] if spec['family'] == 'expiry_reversion' else row[f"{spec['family']}_{spec['n']}"]


class EntryPolicy(HoldingPolicy):
    """Uniform five-session hold isolates entry quality; no roll or resizing."""
    def __init__(self, specs, instruments, calendars):
        super().__init__({p: {} for p in specs}, instruments)
        self.specs, self.calendars, self.exit_day = specs, calendars, {}

    def __call__(self, product, day, row, ledger, equity, sizing, target_vol):
        held = {(p.instrument, p.side): ledger.quantity(p.instrument, p.side, product)
                for p in ledger.lots if p.group == product}
        decision = dict(day=day, product=product, signal_day=row['day'] if row else None,
                        reason='no_signal', signal=0, score=None, requested_quantity=0)
        if held:
            if product in self.exit_due or len(held) != 2 or len(set(held.values())) != 1:
                decision['reason'] = 'retry_exit'
                return {}, decision
            if day >= self.exit_day[product]:
                decision['reason'] = 'five_day_exit'
                self.exit_due.add(product)
                return {}, decision
            if min(self.instruments[c].expiry for c, _ in held) <= day:
                decision['reason'] = 'expiry_eod'
                return held, decision
            decision['reason'] = 'hold'
            return held, decision
        self.exit_due.discard(product)
        if row is None or self.specs[product] is None:
            return {}, decision
        value = score(row, self.specs[product])
        if not np.isfinite(value) or abs(value) < self.specs[product]['threshold']:
            return {}, decision
        calendar = self.calendars[product]
        index = calendar.index(day)
        # A complete five-session label must be available within the same contract life.
        if index+5 >= len(calendar) or row['near'] not in self.instruments or calendar[index+5] > self.instruments[row['near']].expiry:
            decision['reason'] = 'insufficient_contract_life'
            return {}, decision
        direction = 1 if value > 0 else -1
        self.exit_day[product] = calendar[index+5]
        decision.update(reason='entry', signal=direction, score=value, requested_quantity=1)
        return {(row['near'], -direction): 1, (row['far'], direction): 1}, decision


def diagnostics(rows):
    result = []
    names = ['expiry_z'] + [f'{f}_{n}' for f in ('momentum', 'reversion') for n in (5, 20, 60)]
    for product in PRODUCTS:
        for period, begin, finish in [('pre2024', date.min, date(2023,12,31)),
                                     ('validation2024', date(2024,1,1), date(2024,12,31)),
                                     ('exploratory2025plus', date(2025,1,1), date.max)]:
            for name in names:
                for horizon in (1,5,10):
                    group = [r for r in rows if r['product']==product and begin<=r['day']<=finish
                             and r[f'forward_end_{horizon}'] is not None and r[f'forward_end_{horizon}']<=finish
                             and np.isfinite([r[name],r[f'forward_{horizon}']]).all()]
                    if len(group)<20:
                        continue
                    x=np.array([r[name] for r in group]); y=np.array([r[f'forward_{horizon}'] for r in group])
                    rx,ry=ranks(x),ranks(y)
                    ic=float(np.corrcoef(rx,ry)[0,1]) if rx.std()>0 and ry.std()>0 else None
                    order=np.argsort(x,kind='stable'); bins=np.array_split(order,5)
                    for quintile, indices in enumerate(bins,1):
                        result.append(dict(product=product,period=period,factor=name,horizon=horizon,
                                           quintile=quintile,count=len(indices),rank_ic=ic,gross_mean_pnl=float(y[indices].mean())))
    return result


def run(output='outputs/index_futures_continuous_research', daily_version=None, metadata_version=None):
    out=Path(output);out.mkdir(parents=True,exist_ok=True)
    dv,mv,raw,facts,instruments=load_research_data('data/index_futures',daily_version,metadata_version)
    base,prices,calendars=observations(raw,facts,instruments)
    print('Building causal expiry residuals and same-pair continuous chain',flush=True)
    rows,references=transform(base,prices,calendars,instruments)
    fields=['product','day','near','far','expiry_days','relative_spread','expiry_z','linked','linked_delta',
            'segment','roll','reference_mean','reference_std','reference_count'] + [f'{f}_{n}' for f in ('momentum','reversion') for n in (5,20,60)] + [f'forward_{h}' for h in (1,5,10)]
    csv_if(out/'series.csv',[{k:r[k] for k in fields} for r in rows]);csv_if(out/'references.csv',references)
    diag=diagnostics(rows);csv_if(out/'factor_diagnostics.csv',diag)
    specs=experiments();dump(out/'registry.json',specs)
    config=dict(daily_version=dv,metadata_version=mv,radius=5,min_samples=30,min_contracts=12,
                holding_sessions=5,fee_rate='0.00005',ticks=1,sizing='one',selection_year=2024,
                evaluation='2025+ exploratory reused history; not untouched holdout')
    dump(out/'config.json',config)
    trials,selected,family_selected=[],{},{}
    # Last ten observed validation sessions are embargoed; all liquidation stays in validation.
    for product in PRODUCTS:
        validation=[d for d in calendars[product] if d.year==2024]
        end=validation[-11]
        winner=None
        family_winners={}
        for spec in specs:
            result=simulate(rows,prices,calendars,instruments,{product:{}},date(2024,1,1),end,
                            sizing='one',nominal_cap=1e9,target_policy=EntryPolicy({product:spec},instruments,calendars))
            item=dict(product=product,**spec,**result['metrics']);trials.append(item)
            eligible=item['fills']>=20 and item['sharpe'] is not None
            if eligible and (winner is None or item['sharpe']>winner[0]):
                winner=(item['sharpe'],spec)
            if eligible and (spec['family'] not in family_winners or item['sharpe']>family_winners[spec['family']][0]):
                family_winners[spec['family']]=(item['sharpe'],spec)
        # Abstain if no candidate has positive cost-adjusted validation Sharpe.
        selected[product]=winner[1] if winner and winner[0]>0 else None
        family_selected[product]={f: v[1] for f,v in family_winners.items()}
        print(f'{product}: selected {selected[product]}',flush=True)
    csv_if(out/'trials.csv',trials);dump(out/'selected.json',selected)
    dump(out/'family_selected.json',family_selected)
    family_results=[]
    for product in PRODUCTS:
        for family,spec in family_selected[product].items():
            result=simulate(rows,prices,calendars,instruments,{product:{}},date(2025,1,1),max(r['day'] for r in rows),
                            sizing='one',nominal_cap=1e9,target_policy=EntryPolicy({product:spec},instruments,calendars))
            family_results.append(dict(product=product,family=family,id=spec['id'],
                                       net_pnl=result['daily'][-1]['equity']-D('10000000'),**result['metrics']))
    csv_if(out/'family_comparison.csv',family_results)
    totals,yearly=[],[]
    for scenario,fee,ticks in [('base','0.00005',1),('stress','0.0001',2)]:
        result=simulate(rows,prices,calendars,instruments,{p:{} for p in PRODUCTS},date(2025,1,1),max(r['day'] for r in rows),
                        sizing='one',nominal_cap=1e9,fee_rate=fee,slip=ticks,details=True,
                        target_policy=EntryPolicy(selected,instruments,calendars))
        for kind in ('daily','fills','entries','decisions','warnings'):
            csv_if(out/f'{scenario}_{kind}.csv',result[kind])
        dump(out/f'{scenario}_terminal_positions.json',result['terminal_positions'])
        totals.append(dict(scenario=scenario,product='portfolio',net_pnl=result['daily'][-1]['equity']-D('10000000'),**result['metrics']))
        for product in PRODUCTS:
            net=sum((r[f'pnl_{product}'] for r in result['daily']),D(0))
            totals.append(dict(scenario=scenario,product=product,net_pnl=net,
                               fees=sum((f['fee'] for f in result['fills'] if f['group']==product),D(0))))
            for year in sorted({r['day'].year for r in result['daily']}):
                yearly.append(dict(scenario=scenario,product=product,year=year,
                                   net_pnl=sum((r[f'pnl_{product}'] for r in result['daily'] if r['day'].year==year),D(0))))
        if scenario=='base':
            base_result=result
    # Heterogeneous portfolio/product metrics are exported with the union of columns.
    keys=list(dict.fromkeys(k for r in totals for k in r));csv_if(out/'evaluations.csv',[{k:r.get(k) for k in keys} for r in totals])
    csv_if(out/'yearly.csv',yearly)
    expiry_check=[]
    for product in PRODUCTS:
        for dte in sorted({r['expiry_days'] for r in rows if r['product']==product and r['expiry_days'] is not None}):
            group=[r for r in rows if r['product']==product and r['expiry_days']==dte and np.isfinite(r['expiry_z'])]
            if group:
                expiry_check.append(dict(product=product,dte=dte,count=len(group),mean_raw=float(np.mean([r['relative_spread'] for r in group])),
                                         mean_z=float(np.mean([r['expiry_z'] for r in group])),std_z=float(np.std([r['expiry_z'] for r in group],ddof=1)) if len(group)>1 else None))
    csv_if(out/'remaining_expiry_effect.csv',expiry_check)
    plot(out,rows,base_result['daily'],diag)
    text=['# 到期去季节化连续价差研究','',
          '相对价差 r=(远月收盘−近月收盘)/近月收盘。对同品种到期自然日数±5天、此前已到期周期，累计估计均值μ(d)和样本标准差σ(d)，z=(r−μ)/σ。至少30观察和12合约。',
          '链指标 C 从零起始；每日增加同一实际合约对在当日与前一观察日的标准化偏离差，两者用当日可知的同一曲线。这既避免直接拼接不同合约，也避免参考更新引入机械跳变。缺重叠或缺参考时断段，窗口不能跨段。',
          '去季节化不能保证与到期日统计独立，也不能保证链指标平稳；remaining_expiry_effect.csv检验残余到期规律。链指标是信号，不是可交易价格、净值或盈亏。',
          '主序列为每日可观察的z，换月时保留新组合的真实重定价并标记roll；不凭空接平。比较同一z序列5/20/60日动量、滚动均值回归，阈值0.5/1/2；另比较直接回归条件均值的−z信号，共21个候选/品种。缺z时重启窗口。链指标仅是有重叠时的辅助对照，不作为本轮入场因子。参数登记后按2024净夏普选择，至少20成交腿，净夏普非正则空仓。2024末10交易日隔离。',
          '每腿一手，收盘信号最早下一交易日开盘成交；统一持有5个观察交易日，入场须能在近月到期前完成。实际合约不替换，退出缺行情持续重试，到期遵守现金结算规则，期末不强平。标签以实际两腿次日开盘至1/5/10日后开盘计算，跨到期留空。',
          '2025起仅冻结入场参数，参考曲线按当时已完成历史更新。该区间在旧研究已使用，因此本次是新假设的事后探索，不冒充全新独立验证。',
          '基准每腿每次0.5bp＋1tick，压力1bp＋2tick；资金1000万，不按资金缩量，费用为假设。日线开盘可成交性、独立日历和历史公告PIT未核实。原数据只读。','',
          '## 2024选择','']
    text += [f'- {p}：{selected[p]}' for p in PRODUCTS]
    text += ['','## 2025至数据末日净PnL','']+[f"- {r['scenario']} / {r['product']}：{r['net_pnl']:.2f}元" for r in totals]
    text += ['',f'行情快照 {dv}；元数据快照 {mv}。',
             '全部试验在trials.csv；每个方法按2024选出的候选在family_comparison.csv作2025后并列比较（即使2024夏普为负也保留用于诊断，不构成推荐）；不会按后续表现重选。因子分组毛收益及秩相关在factor_diagnostics.csv；成交、账务、参考和序列均导出。',
             f"本轮选定组合基准净夏普：{base_result['metrics']['sharpe']}；不能仅依据本次事后比较确认策略可用。"]
    (out/'report.md').write_text('\n'.join(text),encoding='utf-8')
    dump(out/'provenance.json',dict(**config,source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()))
    print(totals,flush=True)
    return totals


def plot(out,rows,daily,diag):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig,axes=plt.subplots(4,2,figsize=(15,12),constrained_layout=True)
    for product,(left,right) in zip(PRODUCTS,axes):
        group=[r for r in rows if r['product']==product]
        left.plot([r['day'] for r in group],[r['expiry_z'] for r in group],linewidth=.5)
        left.axhline(0,color='gray');left.set_title(f'{product}: expiry-normalized residual z')
        for segment in sorted({r['segment'] for r in group}):
            part=[r for r in group if r['segment']==segment and np.isfinite(r['linked'])]
            right.plot([r['day'] for r in part],[r['linked'] for r in part],linewidth=.7)
        right.set_title(f'{product}: same-pair linked signal (arbitrary origin)')
    fig.savefig(out/'continuous_series.png',dpi=150);plt.close(fig)
    fig,ax=plt.subplots(figsize=(12,5),constrained_layout=True)
    for product in PRODUCTS:
        value=D(0);curve=[]
        for r in daily:
            value+=r[f'pnl_{product}'];curve.append(float(value))
        ax.plot([r['day'] for r in daily],curve,label=product)
    ax.axhline(0,color='gray');ax.set_ylabel('Net PnL (CNY)');ax.legend()
    fig.savefig(out/'selected_pnl.png',dpi=150);plt.close(fig)
    fig,axes=plt.subplots(2,2,figsize=(12,8),constrained_layout=True)
    for product,ax in zip(PRODUCTS,axes.flat):
        for factor in ('momentum_5','reversion_5','expiry_z'):
            group=[r for r in diag if r['product']==product and r['factor']==factor
                   and r['horizon']==5 and r['period']=='validation2024']
            if group:
                ax.plot([r['quintile'] for r in group],[r['gross_mean_pnl'] for r in group],marker='o',label=factor)
        ax.axhline(0,color='gray');ax.set_title(f'{product}: 2024, 5-day gross label PnL')
        ax.set_xlabel('Factor quintile');ax.set_ylabel('CNY per spread lot');ax.legend()
    fig.savefig(out/'grouped_returns.png',dpi=150);plt.close(fig)
