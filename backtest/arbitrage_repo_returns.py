"""Calendar-day estimated repo accrual on fully flat product accounts."""
from datetime import date,timedelta
from pathlib import Path
import csv
import hashlib
import json

from .arbitrage_capital_returns import annualized
from .factor_research import csv_if,dump
from .models import D

PRODUCTS=('IF','IH','IC','IM')


def repo_rate(day):
    return D('.03') if day.year<2015 else D('.02') if day.year<=2020 else D('.015')


def apply_repo(capital,trading_rows,cycles):
    """Preserve trading PnL; accrue actual calendar dates excluding entry/exit/held dates."""
    capital=D(capital)
    if capital<=0:raise ValueError('Initial capital must be positive')
    daily={date.fromisoformat(str(r['day'])):r for r in trading_rows}
    if len(daily)!=len(trading_rows):raise ValueError('Duplicate trading date')
    start,end=min(daily),max(daily)
    intervals=[(date.fromisoformat(str(r['entry_day'])),date.fromisoformat(str(r['exit_day'])) if r['exit_day'] else end) for r in cycles]
    result=[];arbitrage=interest=D(0);day=start
    while day<=end:
        opening=capital+arbitrage+interest
        old_arbitrage=arbitrage
        if day in daily:arbitrage=D(daily[day]['cumulative_future_tick_pnl'])
        arb_change=arbitrage-old_arbitrage
        holding_or_trade=any(begin<=day<=finish for begin,finish in intervals)
        balance=capital+arbitrage+interest
        eligible=not holding_or_trade and balance>0
        rate=repo_rate(day)
        accrual=(balance*rate/D(365)).quantize(D('.00000001')) if eligible else D(0)
        interest+=accrual
        equity=capital+arbitrage+interest
        assert equity-opening==arb_change+accrual
        result.append(dict(day=day,is_observed_trading_day=day in daily,repo_eligible=eligible,
                           repo_rate=rate,interest_base=balance if eligible else D(0),
                           opening_equity=opening,arbitrage_pnl_change=arb_change,repo_interest=accrual,
                           cumulative_arbitrage_pnl=arbitrage,cumulative_repo_interest=interest,equity=equity,
                           actual_daily_return=(equity-opening)/opening if opening>0 else None))
        day+=timedelta(days=1)
    yearly=[]
    for year in sorted({r['day'].year for r in result}):
        group=[r for r in result if r['day'].year==year]
        opening=group[0]['opening_equity'];closing=group[-1]['equity']
        arb=sum((r['arbitrage_pnl_change'] for r in group),D(0));repo=sum((r['repo_interest'] for r in group),D(0))
        yearly.append(dict(year=year,start=group[0]['day'],end=group[-1]['day'],
                           calendar_days=len(group),repo_days=sum(r['repo_eligible'] for r in group),
                           assumed_repo_rate=repo_rate(group[0]['day']),arbitrage_pnl=arb,repo_interest=repo,
                           combined_pnl=arb+repo,opening_equity=opening,closing_equity=closing,
                           actual_return=(closing-opening)/opening if opening>0 else None))
    summary=dict(start=start,end=end,calendar_days=len(result),repo_days=sum(r['repo_eligible'] for r in result),
                 initial_capital=capital,arbitrage_pnl=arbitrage,repo_interest=interest,
                 combined_pnl=arbitrage+interest,final_equity=result[-1]['equity'],
                 total_return=(arbitrage+interest)/capital,
                 original_cagr=annualized(capital,capital+arbitrage,len(result)),
                 combined_cagr=annualized(capital,result[-1]['equity'],len(result)))
    return summary,yearly,result


def run(source='outputs/front_contract_cash_arbitrage',output='outputs/front_contract_cash_arbitrage_repo'):
    source=Path(source);out=Path(output);out.mkdir(parents=True,exist_ok=True)
    def read(name):
        with (source/name).open(encoding='utf-8-sig') as f:return list(csv.DictReader(f))
    returns=read('returns.csv');trading=read('capital_daily.csv');cycles=read('cycles.csv')
    summaries=[];yearly=[];calendar=[]
    for product in PRODUCTS:
        initial=next(r['fixed_initial_capital'] for r in returns if r['product']==product)
        summary,annual,daily=apply_repo(initial,[r for r in trading if r['product']==product],
                                         [r for r in cycles if r['product']==product])
        summaries.append(dict(product=product,**summary))
        yearly.extend(dict(product=product,**r) for r in annual)
        calendar.extend(dict(product=product,**r) for r in daily)
    csv_if(out/'summary.csv',summaries);csv_if(out/'yearly.csv',yearly);csv_if(out/'calendar_daily.csv',calendar)
    dump(out/'config.json',dict(source_config=json.loads((source/'config.json').read_text(encoding='utf-8')),
         rates={'before2015':'.03','2015through2020':'.02','2021onwards':'.015'},day_count='ACT/365; calendar-day accrual',
         eligibility='completely flat dates only; entry/exit dates excluded; weekend/holiday follows actual position interval',
         interest_base='current positive account equity; initial fixed reserve plus cumulative arbitrage PnL and prior interest',
         interest='estimated repo cash accrual; 1e-8 CNY rounding per day; retained and compounds daily; no interest on unused reserve while held',
         source_hashes={n:hashlib.sha256((source/n).read_bytes()).hexdigest() for n in ('returns.csv','capital_daily.csv','cycles.csv')},
         source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()))
    report=['# 当月单组合：空仓资金回购收益估算','',
            '原套利信号、数量、资金本金及交易PnL不变。仅每品种完全空仓的自然日，把当前资金按用户指定年率估算回购利息：2015年前3%，2015–2020含2020年2%，2021起1.5%。',
            '每天利息=当日可用正资金×年率/365，按1e−8元记账；当前资金为固定初始本金＋累计套利PnL＋此前累计利息。利息留存并按日复投；周末/节假日按实际持仓区间判断。开仓/退出日均保守不计回购；有持仓不把剩余预留资金也计息，非正权益不计息。',
            '这是用户年率假设下的现金收益估算，不调用真实回购行情，不模拟回购订单、费用、到期日或到账时点。年界按实际自然日切换，2020年仍为2%。',
            '本金仍为每品种单份入场指数名义×1.5的事后全期峰值；加入利息不扩大套利规模。权益=本金＋套利PnL＋回购利息；CAGR按自然日，年度实际收益率以年初权益为分母。套利PnL仍仅扣期货tick，未计完整手续费、融资、现货成本及股息，非实际ETF净回报。','',
            '|品种|固定本金（万元）|套利PnL（万元）|回购利息（万元）|合计PnL（万元）|原CAGR|加入回购CAGR|',
            '|---|---:|---:|---:|---:|---:|---:|']
    for r in summaries:
        report.append(f"|{r['product']}|{r['initial_capital']/10000:.2f}|{r['arbitrage_pnl']/10000:.2f}|{r['repo_interest']/10000:.2f}|{r['combined_pnl']/10000:.2f}|{r['original_cagr']:.2%}|{r['combined_cagr']:.2%}|")
    report+=['','## 各年实际收益率（套利＋估算回购）','','|年份|IF|IH|IC|IM|','|---|---:|---:|---:|---:|']
    for year in sorted({r['year'] for r in yearly}):
        values=[]
        for product in PRODUCTS:
            r=next((r for r in yearly if r['product']==product and r['year']==year),None)
            values.append(f"{r['actual_return']:.2%}" if r else '—')
        report.append('|'+str(year)+'|'+'|'.join(values)+'|')
    report+=['','2026截至行情末日，首年/末年不强行年化；calendar_daily.csv明确每个自然日是否计息、计息本金和权益变化；yearly.csv分开套利PnL及回购利息。']
    (out/'report.md').write_text('\n'.join(report),encoding='utf-8')
    print(summaries,flush=True)
    return summaries,yearly
