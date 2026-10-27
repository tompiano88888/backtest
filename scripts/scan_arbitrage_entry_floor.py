"""Parameter orchestration of the existing historical Portfolio; no new execution rules."""
import argparse
import csv
import hashlib
import json
import math
import shutil
import time
from dataclasses import asdict
from datetime import date
from pathlib import Path

from backtest.models import D,Asset
from backtest.factor_data import load_research_data
from backtest.cash_futures_basis import load_spot,PAIRS
from backtest.front_contract_arbitrage import front_map
from backtest.rolling_arbitrage_optimization import ProductMarket,PRODUCTS
from backtest.pooled_rolling_arbitrage import Portfolio,replay_history,TRIAL_FIELDS
from backtest.research_dividends import load_events
from backtest.factor_research import csv_if,dump
from backtest.arbitrage_capital_returns import annualized


def read(path):
    with Path(path).open(encoding='utf-8-sig',newline='') as stream:
        return list(csv.DictReader(stream))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def threshold_grid(start='0',stop='.04',step='.001'):
    start,stop,step=map(D,(start,stop,step))
    if not all(v.is_finite() for v in (start,stop,step)) or step<=0 or start<0 or stop<start:
        raise ValueError('Invalid entry floor grid')
    if (stop-start)%step:raise ValueError('Grid must include its upper endpoint exactly')
    return [start+i*step for i in range(int((stop-start)/step)+1)]


class FrozenChoices:
    """Only reuse dated choices already made by the unchanged rolling selector."""
    def __init__(self,market,choices):
        self.market=market;self.choices=choices

    def __getattr__(self,key):
        return getattr(self.market,key)

    def select(self,i,writer):
        day=self.market.dates[i]
        row=dict(self.choices[self.market.product,day])
        if row['decision_day']!=day:raise ValueError('Choice date mismatch')
        return row


def load_choices(path):
    result={}
    for row in read(path):
        row=dict(row)
        for key in ('entry_threshold','exit_threshold'):
            row[key]=D(row[key])
        row['decision_day']=date.fromisoformat(row['decision_day'])
        key=row['product'],row['decision_day']
        if key in result:raise ValueError('Duplicate dated choice')
        result[key]=row
    return result


def restore_trial(root):
    summary=read(root/'summary.csv')[0]
    integer_fields={'entries','closed_cycles','open_at_end','max_simultaneous_positions',
        'budget_gap_calendar_days','cancelled_opening_count','funding_exit_count','rejected_signal_days'}
    summary={k:(v if k in ('product','start','end') else int(v) if k in integer_fields else D(v) if v else None)
             for k,v in summary.items()}
    years=[{k:(v if k=='product' else int(v) if k=='year' else D(v)) for k,v in row.items()}
           for row in read(root/'yearly.csv')]
    return summary,years


def entry_signature(market,choices,floor,begin,end):
    eligible=[]
    for i,day in enumerate(market.dates):
        if not begin<=day<=end:continue
        if day<market.dates[0].replace(year=market.dates[0].year+1):continue
        if day>=market.instruments[market.front[i]].expiry:continue
        choice=choices[market.product,day];rate=market.rates[i]
        if rate is not None and rate>choice['entry_threshold'] and rate>=floor:eligible.append(day)
    return tuple(eligible)


def summarize(fund,product,floor):
    daily=fund.daily;ending=daily[-1];initial=fund.initial
    peak=previous=initial;dd=D(0);returns=[];years=[]
    for row in daily:
        equity=row['equity'];peak=max(peak,equity);dd=max(dd,1-equity/peak)
        if row['is_market_day']:
            returns.append(float(equity/previous-1));previous=equity
    mean=sum(returns)/len(returns)
    std=math.sqrt(sum((v-mean)**2 for v in returns)/(len(returns)-1))
    previous=initial;previous_repo=previous_div=D(0)
    for year in sorted({r['day'].year for r in daily}):
        end=next(r for r in reversed(daily) if r['day'].year==year)
        pnl=end['equity']-previous;repo=end['cumulative_repo']-previous_repo
        years.append(dict(product=product,floor=floor,year=year,opening_equity=previous,
            closing_equity=end['equity'],total_pnl=pnl,arbitrage_pnl=pnl-repo,repo_interest=repo,
            dividend_pnl=end['cumulative_dividends']-previous_div,actual_return=pnl/previous))
        previous=end['equity'];previous_repo=end['cumulative_repo'];previous_div=end['cumulative_dividends']
    summary=dict(product=product,floor=floor,start=daily[0]['day'],end=ending['day'],initial_capital=initial,
        final_equity=ending['equity'],total_pnl=ending['equity']-initial,
        arbitrage_pnl=ending[f'pnl_{product}'],repo_interest=fund.repo_total,
        dividend_pnl=fund.dividends.total,total_return=ending['equity']/initial-1,
        cagr=annualized(initial,ending['equity'],len(daily)),max_drawdown=dd,
        sharpe=D(str(mean/std*math.sqrt(252))) if std else None,
        entries=len(fund.completed)+len(fund.live),closed_cycles=len(fund.completed),open_at_end=len(fund.live),
        max_simultaneous_positions=max(r['positions'] for r in daily),
        max_funding_gap=max(r['funding_gap'] for r in daily),min_cash=min(r['cash'] for r in daily),
        max_cash_deficit=max(r['cash_deficit'] for r in daily),
        budget_gap_calendar_days=sum(r['funding_gap']>0 for r in daily),
        cancelled_opening_count=len(fund.cancelled_openings),funding_exit_count=len(fund.funding_actions),
        rejected_signal_days=sum(r['reason']=='expected_annual_return_below_minimum' for r in fund.logs))
    return summary,years


def save_trial(root,fund,summary,years,config,events):
    root.mkdir(parents=True,exist_ok=True)
    attr=fund.ledger.group_pnl();cycles=[]
    for c in fund.completed+list(fund.live.values()):
        row={k:v for k,v in c.items() if k!='last_spot_value'}
        row['pnl']=attr[c['id']]+fund.dividends.receivables().get(c['id'],D(0))
        row['dividend_pnl']=sum((x['amount'] for x in fund.dividends.claims if x['group']==c['id'] and x['recognized']),D(0))
        if c['id'] in fund.live:row['exit_reason']='open_at_end'
        else:
            expected=(c['entry_future']-c['exit_future'])*fund.instruments[c['contract']].multiplier
            expected+=(c['exit_etf']-c['entry_etf'])*c['etf_quantity']+row['dividend_pnl']
            assert expected==row['pnl'],c['id']
        cycles.append(row)
    assert sum((r['pnl'] for r in cycles),D(0))+fund.repo_total==summary['total_pnl']
    assert all(c['rate']>=summary['floor'] for c in fund.candidate_history)
    for row in fund.allocation_audit:assert row['available_before']>=row['required_cash']
    tables={'daily':fund.daily,'cycles':cycles,'selections':fund.selected,
        'allocations':fund.logs,'candidate_plans':fund.candidate_history,'allocation_audit':fund.allocation_audit,
        'funding_actions':fund.funding_actions,'cancelled_openings':fund.cancelled_openings,
        'proposed_openings':fund.proposed_openings,'dividend_events':events,'dividend_audit':fund.dividends.audit,
        'dividend_claims':fund.dividends.claims,'etf_cashflows':fund.spot_fills,
        'ledger_entries':fund.ledger.entries,'summary':[summary],'yearly':years}
    for name,values in tables.items():csv_if(root/(name+'.csv'),values)
    for name,asset in (('future_fills',Asset.FUTURE),('etf_fills',Asset.FUND)):
        csv_if(root/(name+'.csv'),[dict(day=f.trading_date,**asdict(f.instruction),price=f.price,fee=f.fee)
            for f in fund.ledger.fills if fund.instruments[f.instruction.instrument].asset==asset])
    with (root/'all_trials.csv').open('w',encoding='utf-8-sig',newline='') as handle:
        csv.DictWriter(handle,fieldnames=TRIAL_FIELDS).writeheader()
    # Original 35-way scoring is immutable and referenced, rather than copied 164 times.
    dump(root/'config.json',config)
    dump(root/'audit.json',dict(closed_cycles_reconciled=len(fund.completed),
        calendar_days_reconciled=len(fund.daily),entry_floor_verified=True,
        sufficient_allocation_budget_verified=True,files={p.name:sha(p) for p in root.glob('*.csv')}))


def report(out,trials,years,config):
    winners=[];winner_years=[];lines=['# 各品种到期归零开仓下限寻参','',
        '区间2020-01-01至2026-10-08；每品种独立1000万元，四个账户互不竞争资金，结果不可相加冒充原1000万元组合。',
        '归零年化率=(期货收盘−指数收盘)/指数收盘×365/剩余自然日。开仓额外要求≥下限，等于允许；不减退出阈值。',
        '0%—4%包含两端、步长0.1个百分点，每品种41档。0%仍排除负升水，并非无额外限制版本。',
        '按全区间账户收益率含ETF分红及回购选择，固定初始本金和区间下与CAGR排序等价；并列选较低门槛。原35组滚动选择不重选，沿用固定逐日选择文件。',
        '**本次是全样本事后参数扫描，不是滚动样本外检验，不自动替换当前四品种组合参数。**','',
        '|品种|最优下限|PnL（万）|CAGR|最大回撤|夏普|组合份数|相对1.5%增加PnL（万）|',
        '|---|---:|---:|---:|---:|---:|---:|---:|']
    if config.get('nav_adjustment'):
        lines[0]='# 加入ETF折溢价的分品种开仓下限寻参'
        lines[3]='年化信号=[(期货−指数)/指数+(ETF净值−ETF价格)/ETF净值]×365/剩余自然日；低于本品种下限不新增计划，等于允许，不减退出阈值。'
        lines.insert(6,'NAV使用固定快照同日事后净值，历史公告时点未核实，不是盘中IOPV；缺净值无信号、不填充。滚动评分：'+config['candidate_objective'])
    if config.get('dividend_season_adjustment'):
        lines[0]='# 历史分红季调整后的分品种开仓下限寻参'
        lines[3]='年化信号=[(期货−指数)/指数+预测分红收益比例]×365/剩余自然日；预测仅用此前3个完整年度月度贡献，按自然日分配。'
        lines[4]='扫描0%—4%共41档；0%仍排除调整后的负信号，原始基差可以为负。'
        lines.insert(6,'逐日35组选择已由分红调整信号重新训练，本扫描复用该选择，不复用原基准选择；历史修订/PIT、原代理评分和执行假设限制保留。')
        if config.get('dividend_season_months'):
            lines.insert(6,'预测分红累计月份：'+','.join(map(str,config['dividend_season_months']))+'；其他月份为零，仅按决策日后至到期区间的实际重叠天数累计。')
    for product in PRODUCTS:
        group=[r for r in trials if r['product']==product]
        best=max(group,key=lambda r:(r['total_return'],-r['floor']))
        reference=next(r for r in group if r['floor']==D('.015'))
        ranked=sorted(group,key=lambda r:(-r['total_return'],r['floor']))
        for rank,row in enumerate(ranked,1):row['rank_within_product']=rank
        winners.append(dict(best,pnl_delta_vs_015=best['total_pnl']-reference['total_pnl']))
        winner_years.extend(r for r in years if r['product']==product and r['floor']==best['floor'])
        lines.append(f"|{product}|{best['floor']:.1%}|{best['total_pnl']/10000:.2f}|{best['cagr']:.2%}|{best['max_drawdown']:.2%}|{best['sharpe']:.2f}|{best['entries']}|{(best['total_pnl']-reference['total_pnl'])/10000:.2f}|")
    csv_if(out/'all_trials.csv',trials);csv_if(out/'yearly_all_trials.csv',years)
    csv_if(out/'best_by_product.csv',winners);csv_if(out/'yearly_best.csv',winner_years)
    gap_trials=[r for r in trials if r['max_funding_gap']>0 or r['max_cash_deficit']>0]
    csv_if(out/'funding_gap_trials.csv',gap_trials)
    no_best_gaps=all(r['max_funding_gap']==0 and r['max_cash_deficit']==0 for r in winners)
    lines+=['',f'资金约束检查：{len(gap_trials)}组候选出现过日终预算缺口，均在funding_gap_trials.csv标记；不补现金、不隐藏缺口。最优参数是否均无日终现金/预算缺口：{no_best_gaps}。']
    lines+=['','## 各自最优参数逐年实际收益率','',
        '|年份|IF|IH|IC|IM|','|---|---:|---:|---:|---:|']
    for year in sorted({r['year'] for r in years}):
        values=[next(r['actual_return'] for r in winner_years if r['year']==year and r['product']==p) for p in PRODUCTS]
        lines.append(f"|{year}|"+'|'.join(f'{v:.2%}' for v in values)+'|')
    lines+=['','2026截至10月8日。IM满足原上市后满一年条件前仅有回购，不应解释为早期套利业绩。分红已含在套利PnL，回购单列；不将分红再加到总收益。',
        '## 规则与限制','',
        '复用原历史Portfolio和同一replay_history日期调度，不增加执行或账务实现；实际ETF、整数手/100份向下取整、期货1tick、15%研究保证金预算、普通回款T+1以及原事后撤当天计划/紧急退出均保留。',
        '回购ACT/365：2020年2%，2021起1.5%，正现金减保证金和T+1锁。统一日历空仓日保留；252/零无风险夏普，周末利息计入下一市场日收益。',
        '选参源及行情快照在config.json登记SHA256，源评分仅计算一次且不随门槛改变；164组均留有每日、成交及账务明细，trial/audit.json记录校验和与独立两腿盈亏核对。',
        '原同收盘事后撤计划回看、历史元数据可知时间和完整费用/份额调整限制保留。旧日频研究不是统一分钟回调历史验收或可实盘净收益。','',
        '## 复跑','', '```powershell',
        f'.venv\\Scripts\\python.exe -m scripts.scan_arbitrage_entry_floor --source {config["source_directory"]} --output {out.as_posix()}',
        '```','', '![参数敏感性](sensitivity.png)']
    (out/'report.md').write_text('\n'.join(lines),encoding='utf-8')
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig,axes=plt.subplots(2,2,figsize=(12,8),constrained_layout=True)
    for ax,p in zip(axes.flat,PRODUCTS):
        group=[r for r in trials if r['product']==p];best=next(r for r in winners if r['product']==p)
        ax.plot([float(r['floor']*100) for r in group],[float(r['cagr']*100) for r in group],marker='.',label='CAGR')
        ax.axvline(1.5,color='grey',linestyle='--',label='Previous 1.5%')
        ax.scatter([float(best['floor']*100)],[float(best['cagr']*100)],color='red',zorder=5,label='Best in sample')
        ax.set(title=p,xlabel='Zero-convergence annualized entry floor (%)',ylabel='Account CAGR (%)')
        ax.grid(alpha=.25);ax.legend(fontsize=8)
    fig.savefig(out/'sensitivity.png',dpi=150);plt.close(fig)
    return winners


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--source',default='outputs/arbitrage_zero_convergence_015')
    parser.add_argument('--output',default='outputs/arbitrage_entry_floor_scan_0_4')
    parser.add_argument('--start',default='0');parser.add_argument('--stop',default='.04');parser.add_argument('--step',default='.001')
    parser.add_argument('--products',default=','.join(PRODUCTS))
    parser.add_argument('--summarize-only',action='store_true')
    args=parser.parse_args();grid=threshold_grid(args.start,args.stop,args.step)
    source=Path(args.source);out=Path(args.output);out.mkdir(parents=True,exist_ok=True)
    products=tuple(args.products.split(','))
    if not products or len(set(products))!=len(products) or any(p not in PRODUCTS for p in products):
        raise ValueError('Invalid products')
    if args.summarize_only:
        config=json.loads((out/'config.json').read_text(encoding='utf-8'));trials=[];yearly=[]
        versions=set();reused=0
        for p in PRODUCTS:
            for floor in grid:
                trial=out/p/f'{int(floor*10000):03d}bp'
                audit=json.loads((trial/'audit.json').read_text(encoding='utf-8'))
                assert all(sha(trial/name)==digest for name,digest in audit['files'].items())
                local=json.loads((trial/'config.json').read_text(encoding='utf-8'))
                assert local['source_config_sha256']==config['source_config_sha256']
                assert local['source_selections_sha256']==config['source_selections_sha256']
                versions.add(local['script_sha256']);reused+=bool(local.get('identical_signal_trial'))
                summary,years=restore_trial(trial)
                assert summary['product']==p and summary['floor']==floor
                trials.append(summary);yearly.extend(years)
        config['script_versions']=sorted(versions);config['summary_script_sha256']=sha(__file__)
        dump(out/'config.json',config)
        winners=report(out,trials,yearly,config)
        dump(out/'completion.json',dict(trials=len(trials),identical_signal_reuses=reused,
             per_trial_hashes_verified=True,source_selections_unchanged=
             sha(source/'selections.csv')==config['source_selections_sha256']))
        print(json.dumps(winners,default=str,ensure_ascii=False));return
    prior=json.loads((source/'config.json').read_text(encoding='utf-8'))
    prior.setdefault('etf_scoring',False)
    prior.setdefault('candidate_objective','inherited ideal index one-lot return')
    if not prior['dividends'] or not prior['actual_etf_leg']:
        raise ValueError('Scan requires an actual ETF dividend source account')
    snapshots=prior['source_snapshots']
    _,_,raw,facts,instruments=load_research_data('data/index_futures',snapshots['daily_version'],snapshots['metadata_version'])
    _,spot_rows=load_spot(version=snapshots['spot_version'])
    prices={(r['instrument'],r['trading_date']):r for r in raw}
    spot={(r['instrument'],r['trading_date']):r for r in spot_rows}
    calendars={p:sorted({r['trading_date'] for r in raw if r['product']==p}) for p in PRODUCTS}
    first={c:date.fromisoformat(f['first_trade_date']) for c,f in facts.items()}
    mapping=front_map(instruments,first,calendars);choices=load_choices(source/'selections.csv')
    events=load_events(prior['dividend_version']);dates=sorted({d for ds in calendars.values() for d in ds})
    nav_signal=None
    if prior['nav_adjustment']:
        from backtest.nav_arbitrage_signal import NAVSignal,load_nav
        nav_signal=NAVSignal(prices,spot,instruments,load_nav(prior['nav_version']))
    provider=nav_signal.rate if nav_signal else None
    season_signal=None
    if prior.get('dividend_season_adjustment',False):
        from backtest.dividend_season_signal import DividendSeasonSignal,load_total_return
        season_signal=DividendSeasonSignal(prices,spot,instruments,load_total_return(prior['total_return_version']),active_months=prior.get('dividend_season_months'))
        provider=season_signal.rate
    def market_for(p):
        if prior['etf_scoring']:
            from backtest.etf_candidate_scoring import ETFCandidateMarket
            return ETFCandidateMarket(p,calendars[p],mapping,prices,spot,instruments,provider,
                                      dividend_events=events,margin=prior['margin_rate'])
        return ProductMarket(p,calendars[p],mapping,prices,spot,instruments,provider)
    markets={p:FrozenChoices(market_for(p),choices) for p in PRODUCTS}
    if nav_signal:csv_if(out/'nav_signals.csv',list(nav_signal.audit.values()))
    if season_signal:csv_if(out/'dividend_season_signals.csv',list(season_signal.audit.values()))
    begin=date(2020,1,1);end=dates[-1]
    config=dict(prior,source_directory=source.as_posix(),source_selections_sha256=sha(source/'selections.csv'),
        source_config_sha256=sha(source/'config.json'),source_trials_sha256=sha(source/'all_trials.csv'),
        grid=grid,initial_per_product='10000000',independent_accounts=True,
        objective='full sample account return including cash dividends and repo; ex-post in-sample only',
        selection_trials_source=(source/'all_trials.csv').as_posix(),
        minimum_return_basis='zero_convergence',script_sha256=sha(__file__))
    if not (out/'config.json').exists():dump(out/'config.json',config)
    trials=[];yearly=[];start=time.perf_counter()
    for product in products:
        seen={}
        for floor in grid:
            trial=out/product/f'{int(floor*10000):03d}bp'
            trial_config=dict(config,products=[product],min_expected_annual_return=floor)
            signature=entry_signature(markets[product],choices,floor,begin,end)
            if (trial/'audit.json').exists():
                audit=json.loads((trial/'audit.json').read_text(encoding='utf-8'))
                old_config=json.loads((trial/'config.json').read_text(encoding='utf-8'))
                assert old_config['source_config_sha256']==config['source_config_sha256']
                assert old_config['source_selections_sha256']==config['source_selections_sha256']
                assert old_config['min_expected_annual_return']==str(floor)
                assert all(sha(trial/name)==digest for name,digest in audit['files'].items())
                summary,years=restore_trial(trial);mode='resumed'
            elif signature in seen:
                previous=seen[signature];summary,years=restore_trial(previous)
                summary['floor']=floor
                years=[dict(r,floor=floor) for r in years]
                trial.mkdir(parents=True,exist_ok=True)
                for file in previous.glob('*.csv'):
                    if file.name not in ('summary.csv','yearly.csv'):shutil.copyfile(file,trial/file.name)
                assert all(D(r['rate'])>=floor for r in read(trial/'candidate_plans.csv'))
                csv_if(trial/'summary.csv',[summary]);csv_if(trial/'yearly.csv',years)
                dump(trial/'config.json',dict(trial_config,identical_signal_trial=previous.as_posix()))
                audit=json.loads((previous/'audit.json').read_text(encoding='utf-8'))
                audit['files']={p.name:sha(p) for p in trial.glob('*.csv')}
                audit['identical_signal_trial']=previous.as_posix();dump(trial/'audit.json',audit)
                mode='identical-signals'
            else:
                fund=Portfolio(instruments,prices,spot,{product:markets[product]},'10000000','.15',True,True,True,events,provider,
                    min_expected_annual_return=floor,minimum_return_basis='zero_convergence')
                replay_history(fund,dates,begin,end,None,single_product_reporting=True)
                summary,years=summarize(fund,product,floor)
                save_trial(trial,fund,summary,years,trial_config,events);mode='replayed'
            seen[signature]=trial
            trials.append(summary);yearly.extend(years)
            csv_if(out/('progress_'+ '_'.join(products)+'.csv'),trials)
            print(f"{len(trials)}/{len(grid)*len(products)} {product} floor={floor:.1%} PnL={summary['total_pnl']:.2f} CAGR={summary['cagr']:.4%} {mode} elapsed={time.perf_counter()-start:.1f}s",flush=True)
    if set(products)==set(PRODUCTS):
        winners=report(out,trials,yearly,config)
        dump(out/'completion.json',dict(trials=len(trials),elapsed_seconds=time.perf_counter()-start,
            source_selections_unchanged=sha(source/'selections.csv')==config['source_selections_sha256']))
        print(json.dumps(winners,default=str,ensure_ascii=False),flush=True)


if __name__=='__main__':main()
