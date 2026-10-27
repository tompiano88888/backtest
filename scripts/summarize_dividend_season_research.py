"""Read-only reconciliation, seasonal diagnostics and pooled comparison."""
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
import argparse
import hashlib
import json

from backtest.models import D
from backtest.factor_research import csv_if, dump
from scripts.compare_arbitrage_floor import rows, risk

ROOT=Path('outputs/arbitrage_dividend_season_research')
FIXED=Path('outputs/arbitrage_dividend_season_fixed_floors')
OPTIMIZED=Path('outputs/arbitrage_dividend_season_optimized_floors')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def audit(root):
    summary=rows(root,'summary.csv')[0]
    cycles=rows(root,'cycles.csv')
    for c in cycles:
        # Historical CFFEX equity-index multipliers in this pinned study.
        multiplier=D(300 if c['product'] in ('IF','IH') else 200)
        expected=(D(c['entry_future'])-D(c['exit_future']))*multiplier
        expected+=(D(c['exit_etf'])-D(c['entry_etf']))*int(c['etf_quantity'])+D(c['dividend_pnl'])
        assert expected==D(c['pnl']),c['id']
    assert sum((D(c['pnl']) for c in cycles),D(0))+D(summary['repo_interest'])==D(summary['total_pnl'])
    assert summary['open_at_end']=='0'
    for r in rows(root,'daily.csv'):
        assert D(r['equity'])==D(r['cash'])+D(r['ETF_proxy_value'])+D(r['dividend_receivable'])
        assert sum((D(r['pnl_'+p]) for p in ('IF','IH','IC','IM')),D(0))+D(r['cumulative_repo'])==D(r['equity'])-D('10000000')
    return dict(closed_cycles_reconciled=len(cycles),calendar_days_reconciled=len(rows(root,'daily.csv')))


def main():
    global ROOT,FIXED,OPTIMIZED
    parser=argparse.ArgumentParser()
    parser.add_argument('--may-aug',action='store_true',help='Summarize the explicitly restricted May-August branch')
    args=parser.parse_args()
    if args.may_aug:
        ROOT=Path('outputs/arbitrage_dividend_may_aug_research')
        FIXED=Path('outputs/arbitrage_dividend_may_aug_fixed_floors')
        OPTIMIZED=Path('outputs/arbitrage_dividend_may_aug_optimized_floors')
    active_months=(5,6,7,8) if args.may_aug else (5,6,7)
    ROOT.mkdir(parents=True,exist_ok=True)
    preserved=json.loads(Path('outputs/arbitrage_nav_floor_research/baseline_preserved.json').read_text(encoding='utf-8'))
    assert all(sha(Path(p))==digest for p,digest in preserved.items())
    comparisons=[];yearly=[];products=[];verification={};cohorts=[]
    for label,root in [('original',Path('outputs/arbitrage_pooled_optimized_floors')),
                       ('season_fixed_floors',FIXED),('season_optimized_floors',OPTIMIZED)]:
        if not root.exists():
            continue
        summary=rows(root,'summary.csv')[0]
        comparisons.append(dict(branch=label,**summary,**risk(root)))
        yearly.extend(dict(branch=label,**r) for r in rows(root,'yearly.csv'))
        verification[label]=audit(root)
        grouped=defaultdict(lambda: [D(0),D(0),0])
        for c in rows(root,'cycles.csv'):
            group=grouped[c['product']];group[0]+=D(c['pnl']);group[1]+=D(c['dividend_pnl']);group[2]+=1
        products.extend(dict(branch=label,product=p,pnl=v[0],dividends=v[1],cycles=v[2]) for p,v in grouped.items())
        for season in (True,False):
            group=[c for c in rows(root,'cycles.csv') if (date.fromisoformat(c['signal_day']).month in active_months)==season]
            cohorts.append(dict(branch=label,season_months=','.join(map(str,active_months)),signal_in_season=season,cycles=len(group),
                                whole_cycle_pnl=sum((D(c['pnl']) for c in group),D(0))))
    csv_if(ROOT/'comparison.csv',comparisons);csv_if(ROOT/'yearly.csv',yearly);csv_if(ROOT/'product_pnl.csv',products)
    csv_if(ROOT/'season_entry_cohorts.csv',cohorts)
    for label,source in [('fixed_repeat',FIXED),('optimized_repeat',OPTIMIZED)]:
        repeat=Path(str(source)+'_repeat')
        if repeat.exists() and (repeat/'summary.csv').exists():
            names=[p.name for p in source.glob('*.csv')]
            assert all(sha(source/n)==sha(repeat/n) for n in names)
            verification[label]=dict(csv_files_byte_identical=len(names),rolling_selection_cache_used=True)
    monthly=defaultdict(lambda: D(0));annual=defaultdict(lambda: D(0));daily={}
    for r in rows(FIXED,'index_dividend_contributions.csv'):
        day=date.fromisoformat(r['day']);value=D(r['contribution']);p=r['product']
        daily[p,day]=value
        monthly[p,day.year,day.month]+=value;annual[p,day.year]+=value
    csv_if(ROOT/'monthly_contributions.csv',[dict(product=p,year=y,month=m,contribution=v) for (p,y,m),v in sorted(monthly.items())])
    csv_if(ROOT/'annual_contributions.csv',[dict(product=p,year=y,contribution=v) for (p,y),v in sorted(annual.items())])
    seasons=[]
    for p in ('IF','IH','IC','IM'):
        total=sum((annual[p,y] for y in range(2017,2026)),D(0))
        peak=sum((monthly[p,y,m] for y in range(2017,2026) for m in active_months),D(0))
        seasons.append(dict(product=p,years='2017-2025',mean_annual_contribution=total/9,season_share=peak/total))
    csv_if(ROOT/'seasonality.csv',seasons)
    errors=[]
    for r in rows(FIXED,'dividend_season_signals.csv'):
        if r['day']<'2020-01-01' or not r['predicted_dividend_ratio']:
            continue
        day=date.fromisoformat(r['day']);expiry=day+timedelta(days=int(r['days_to_expiry']))
        if expiry>date(2026,10,8):continue
        actual=sum((daily.get((r['product'],day+timedelta(days=i)),D(0)) for i in range(1,(expiry-day).days+1)),D(0))
        errors.append(dict(product=r['product'],contract=r['contract'],day=day,
            predicted=D(r['predicted_dividend_ratio']),realized=actual,error=D(r['predicted_dividend_ratio'])-actual))
    csv_if(ROOT/'forecast_errors.csv',errors)
    error_summary=[]
    for p in ('IF','IH','IC','IM'):
        group=[r for r in errors if r['product']==p]
        error_summary.append(dict(product=p,observations=len(group),
            mean_error=sum((r['error'] for r in group),D(0))/len(group),
            mean_absolute_error=sum((abs(r['error']) for r in group),D(0))/len(group)))
    csv_if(ROOT/'forecast_error_summary.csv',error_summary)
    verification['baseline_preservation']=dict(files=len(preserved),unchanged=True)
    if args.may_aug:
        for name in ('season_filter_audit','default_model_compatibility'):
            path=ROOT/(name+'.json')
            if path.exists():verification[name]=json.loads(path.read_text(encoding='utf-8'))
    scan=Path('outputs/arbitrage_dividend_may_aug_floor_scan' if args.may_aug else 'outputs/arbitrage_dividend_season_floor_scan')
    if (scan/'completion.json').exists():
        verification['floor_scan']=json.loads((scan/'completion.json').read_text(encoding='utf-8'))
        verified=0
        for p in scan.glob('*/*bp/audit.json'):
            item=json.loads(p.read_text(encoding='utf-8'))
            assert all(sha(p.parent/name)==digest for name,digest in item['files'].items())
            verified+=1
        assert verified==164
        verification['scan_trial_checksums_verified']=verified
    dump(ROOT/'verification.json',verification)
    lines=['# 分红季基差调整研究','',
        '区间2020-01-01至2026-10-08，四品种共享1000万元。原基准保留，新增分支不加NAV。',
        '信号=[(F-S)/S+预测分红收益比例]×365/剩余自然日。预测取此前3个完整年度月度全收益/价格指数收益差均值，按各月自然日分配至到期；每个历史训练信号按其自身日期预测。',
        '本分支预测累计月份：'+('仅5—8月，其余月份为零；跨月仅取未来至到期区间与5—8月的交集，不把全年分红重新摊进这四个月。' if args.may_aug else '全年12个月。'),
        '新信号进入原35组滚动一年开平仓寻参、实际入场/退出与排序；原代理评分目标、实际ETF账务、分红、回购、费用及资金处理保持。',
        '全收益快照3f899b4d11604eef86a318e7496f9741，21379条；与原价格指数共同覆盖内交易日完全一致。历史回溯/修订和公告时间未核实，不宣称历史版本PIT验收。','',
        '|分支|PnL（万元）|CAGR|最大回撤|夏普|组合数|最大预算缺口（元）|',
        '|---|---:|---:|---:|---:|---:|---:|']
    for r in comparisons:
        lines.append(f"|{r['branch']}|{D(r['total_pnl'])/10000:.4f}|{D(r['cagr']):.4%}|{D(r['max_drawdown']):.4%}|{float(r['sharpe']):.4f}|{r['entries']}|{D(r['max_funding_gap']):.2f}|")
    if (scan/'best_by_product.csv').exists():
        floors={r['product']:r['floor'] for r in rows(scan,'best_by_product.csv')}
        lines+=['','重新扫描下限：'+' / '.join(f'{p} {D(floors[p]):.1%}' for p in ('IF','IH','IC','IM'))+'。扫描每品种0%—4%、0.1个百分点，独立账户选优后再放回统一资金池；不是组合全局最优。']
    lines+=['',f'|品种|2017—2025平均分红贡献|5—{max(active_months)}月占比|','|---|---:|---:|']
    for r in seasons:
        lines.append(f"|{r['product']}|{r['mean_annual_contribution']:.2%}|{r['season_share']:.1%}|")
    lines+=['','|品种|到期区间预测平均偏差|到期区间预测平均绝对误差|','|---|---:|---:|']
    for r in error_summary:
        lines.append(f"|{r['product']}|{r['mean_error']:.3%}|{r['mean_absolute_error']:.3%}|")
    lines+=['','季节性表是事后描述，未用于提前选择预测窗口；逐日预测仅用往年数据。收益差含精度噪声，保持带符号月度汇总，负区间预测不出信号。早期明显异常保留在原始贡献表，不能将所有差值解读为纯分红。',
        '预测误差明细见forecast_errors.csv：真实分红贡献只用于事后核对，未进入预测或现金；未将ETF派息重复加到总收益。',
        f'误差以未年化的到期区间收益比例计，样本包含重复到期区间，不作独立统计显著性检验。season_entry_cohorts.csv按5—{max(active_months)}月信号入场与其他月份分组，统计整笔持有收益，不能当成期间收益归因。',
        '固定原下限分支的日终现金/预算缺口如表，照实保留，不改现金、不自动调整指令。年度、品种归因与各扫描资金缺口均保留。',
        '**开仓下限为全样本事后寻优，结果不是样本外收益。原同日事后撤计划回看、完整历史费用/税/份额调整缺失仍在，不是分钟统一框架验收。收益提高也不能单凭这一项替换原基准。**',
        f"原基准{len(preserved)}份保护文件SHA256不变；逐组合两腿、逐日权益及PnL归因核对见verification.json。"]
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig,axes=plt.subplots(1,2,figsize=(13,4.5),constrained_layout=True)
    for p in ('IF','IH','IC','IM'):
        axes[0].plot(range(1,13),[float(sum((monthly[p,y,m] for y in range(2017,2026)),D(0))/9*100) for m in range(1,13)],marker='o',label=p)
    axes[0].set(title='Historical dividend contribution (2017-2025 mean)',xlabel='Month',ylabel='Return contribution (%)',xticks=range(1,13))
    for label,root in [('Original',Path('outputs/arbitrage_pooled_optimized_floors')),('Season fixed',FIXED),('Season optimized',OPTIMIZED)]:
        if not (root/'summary.csv').exists():continue
        data=rows(root,'daily.csv')
        axes[1].plot([date.fromisoformat(r['day']) for r in data],[float(D(r['equity'])/D('10000000')) for r in data],label=label)
    axes[1].set(title='Pooled account equity',ylabel='NAV')
    for ax in axes:ax.grid(alpha=.25);ax.legend()
    fig.savefig(ROOT/'comparison.png',dpi=150);plt.close(fig)
    lines+=['','![季节贡献与统一资金池净值](comparison.png)']
    (ROOT/'report.md').write_text('\n'.join(lines),encoding='utf-8')
    print(json.dumps(comparisons,default=str,ensure_ascii=False))


if __name__=='__main__':main()
