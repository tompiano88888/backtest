"""Read-only standard performance reporting; never changes execution or accounting."""
import csv
from contextlib import contextmanager
from datetime import date
from decimal import Decimal as D
import json
import math
import os
from pathlib import Path
from statistics import mean, stdev
import tempfile


@contextmanager
def _matplotlib_config_dir():
    """Keep matplotlib's font cache out of the report directory.

    A caller-provided MPLCONFIGDIR is respected. Otherwise one stable temp location is reused
    (rebuilding the font cache costs seconds), with a throwaway directory as the fallback.
    Matplotlib reads the variable on first import only, as before.
    """
    if os.environ.get('MPLCONFIGDIR'):
        yield
        return
    scratch = None
    cache = Path(tempfile.gettempdir()) / 'cn-backtest-matplotlib'
    try:
        cache.mkdir(parents=True, exist_ok=True)
        if not os.access(cache, os.W_OK):
            raise OSError('matplotlib cache directory is not writable')
    except OSError:
        scratch = tempfile.TemporaryDirectory(prefix='cn-backtest-mpl-', ignore_cleanup_errors=True)
        cache = Path(scratch.name)
    os.environ['MPLCONFIGDIR'] = str(cache)
    try:
        yield
    finally:
        os.environ.pop('MPLCONFIGDIR', None)
        if scratch is not None:
            scratch.cleanup()


def statistics(rows, initial, start):
    """One observation per explicit date. Nonpositive equity yields undefined ratios."""
    rows = list(rows)
    if not rows:
        raise ValueError('Performance requires at least one dated equity observation')
    start = date.fromisoformat(str(start))
    initial = D(str(initial))
    if not initial.is_finite():
        raise ValueError('Initial equity must be finite')
    prior = peak = market_prior = initial
    returns, series, years, issues = [], [], {}, []
    previous_day = None
    valid = initial > 0
    for row in rows:
        day = date.fromisoformat(str(row['day']))
        equity = D(str(row['equity']))
        if not equity.is_finite() or day < start or (previous_day and day <= previous_day):
            raise ValueError('Equity must be finite and dates strictly increasing from start')
        previous_day = day
        year = years.setdefault(day.year, dict(opening=prior, closing=prior, peak=prior,
                                              max_drawdown=D(0) if prior > 0 else None,
                                              returns=[], valid=prior > 0))
        valid = valid and equity > 0
        year['valid'] = year['valid'] and equity > 0
        peak = max(peak, equity)
        year['peak'] = max(year['peak'], equity)
        if year['max_drawdown'] is not None:
            year['max_drawdown'] = max(year['max_drawdown'], 1-equity/year['peak'])
        year['closing'] = equity
        flag = row.get('is_market_day', True)
        if flag not in (True, False, 'True', 'False'):
            raise ValueError('is_market_day must be boolean')
        if flag is True or flag == 'True':
            if market_prior > 0 and equity > 0:
                r = float(equity/market_prior-1)
                returns.append(r); year['returns'].append(r)
            else:
                year['valid'] = False
            market_prior = equity
        series.append(dict(day=day.isoformat(), equity=str(equity), pnl=str(equity-initial),
                           drawdown=str(1-equity/peak) if initial > 0 else None))
        prior = equity
    def risk(rs, usable):
        sd = stdev(rs) if len(rs)>1 and usable else None
        return dict(sharpe=mean(rs)/sd*math.sqrt(252) if sd else None,
                    volatility=sd*math.sqrt(252) if sd is not None else None)
    end = previous_day
    elapsed = (end-start).days
    cagr = math.expm1(math.log(float(prior/initial))*365.25/elapsed) if elapsed>0 and valid else None
    dd = max(D(r['drawdown']) for r in series) if initial>0 else None
    annual = []
    for y, r in years.items():
        annual.append(dict(year=y, pnl=str(r['closing']-r['opening']),
                           return_rate=str(r['closing']/r['opening']-1) if r['opening']>0 else None,
                           max_drawdown=str(r['max_drawdown']) if r['max_drawdown'] is not None else None,
                           partial_year=start>date(y,1,1) or end<date(y,12,31),
                           **risk(r['returns'],r['valid'])))
    # A real exception, not `assert`: the reconciliation must survive `python -O`.
    if sum(D(r['pnl']) for r in annual) != prior-initial:
        raise ValueError('Annual PnL does not reconcile with total PnL')
    if not valid: issues.append('nonpositive_equity: CAGR/Sharpe/volatility/Calmar undefined')
    if elapsed==0: issues.append('zero_elapsed_days: CAGR/Calmar undefined')
    if len(returns)<2: issues.append('insufficient_market_returns: Sharpe/volatility undefined')
    return dict(schema_version=1, initial=str(initial), final_equity=str(prior), pnl=str(prior-initial),
                total_return=str(prior/initial-1) if initial>0 else None,
                start=start.isoformat(), end=end.isoformat(), cagr=cagr,
                max_drawdown=str(dd) if dd is not None else None,
                calmar=cagr/float(dd) if dd and cagr is not None else None,
                **risk(returns,valid), annual=annual, series=series, issues=issues,
                methodology=dict(cagr='calendar elapsed days / 365.25', sharpe='sample std; 252; risk-free 0',
                                 drawdown='daily observations including initial equity',
                                 annual='opening equity; annual high-water mark reset',
                                 nonmarket_income='included in next market observation return'))


def export_performance(rows, initial, start, output, *, title='回测表现', subtitle='统一资金池',
                       minute_drawdown=None, caveat='研究假设及数据边界以本次回测配置为准。',
                       observation_basis='explicit_daily_equity'):
    """Public finalizer. Call once on full-account daily data after a completed run."""
    metrics = statistics(rows, initial, start)
    metrics['observation_basis'] = observation_basis
    metrics['minute_max_drawdown'] = str(minute_drawdown) if minute_drawdown is not None else None
    out = Path(output) / 'standard_report'
    out.mkdir(parents=True, exist_ok=True)
    with _matplotlib_config_dir():
        extra = f'分钟最大回撤 {float(minute_drawdown):.2%} · 主指标采用日终口径' if minute_drawdown is not None else ''
        if observation_basis != 'explicit_daily_equity':
            extra = '按交易日最后可见观察估值；非完整日终结算验收'
        if metrics['issues']:
            caveat = '注意：样本不足或权益非正时，不适用的风险指标显示“—”；PnL与实际亏损完整保留。'
        plot(metrics,title,subtitle,'#3d62a8',extra,caveat,out/'performance.png')
    (out/'metrics.json').write_text(json.dumps(metrics,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    for name, records in [('annual.csv',metrics['annual']),('daily.csv',metrics['series'])]:
        with (out/name).open('w',encoding='utf-8-sig',newline='') as f:
            writer=csv.DictWriter(f,fieldnames=list(records[0]));writer.writeheader();writer.writerows(records)
    return metrics


def scenario_observations(config, snapshots):
    """Use explicit trading dates, never the wall-clock date of a night session."""
    by_day = {}
    if len(config.get('frames',[])) != len(snapshots):
        raise ValueError('Scenario frame/snapshot alignment missing; supply explicit daily observations')
    for frame, snapshot in zip(config['frames'],snapshots):
        day = frame.get('accounting_day_end') or frame.get('accounting_day_start')
        dates = {str(r['trading_date']) for key in ('bars','auctions','settlements') for r in frame.get(key,[])}
        if day is None:
            if len(dates)!=1:
                raise ValueError('Explicit report trading date required for empty or mixed-date frames')
            day = dates.pop()
        day = str(day)
        market = bool(frame.get('open_instruments')) or bool(frame.get('bars')) or bool(frame.get('auctions'))
        by_day[day] = dict(day=day,equity=snapshot['equity'],
                          is_market_day=market or by_day.get(day,{}).get('is_market_day',False))
    return [by_day[d] for d in sorted(by_day)]


def export_scenario_report(payload, config, output):
    rows = scenario_observations(config,payload['snapshots'])
    if not rows:
        raise ValueError('Cannot finalize an empty backtest')
    return export_performance(rows,config['initial_cash'],rows[0]['day'],output,
                              title=config.get('name','回测表现'),
                              observation_basis='last_observed_trading_day_equity')


def plot(metrics, title, subtitle, accent, extra, caveat, destination):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    from matplotlib.ticker import FuncFormatter
    from matplotlib.patches import FancyBboxPatch
    up, down, neutral, line = '#c73535', '#16804a', '#111111', '#3d62a8'
    def change_color(value):
        return neutral if value is None else up if D(str(value)) > 0 else down if D(str(value)) < 0 else neutral
    plt.rcParams.update({'font.sans-serif':['Microsoft YaHei','DejaVu Sans'],
                         'axes.unicode_minus':False, 'font.size':10,
                         'text.color':'#203047', 'axes.labelcolor':'#65758b',
                         'xtick.color':neutral, 'ytick.color':neutral})
    fig = plt.figure(figsize=(14,max(13, 10+len(metrics['annual'])*.43)), facecolor='#f3f6fa')
    fig.text(.06,.957,title,fontsize=26,weight='bold')
    fig.text(.06,.926,subtitle,fontsize=11,color='#65758b')
    fig.text(.94,.958,'标准回测报告  /  v1',ha='right',fontsize=10,color=neutral)
    fig.text(.94,.926,f"{metrics['start']} — {metrics['end']}  ·  初始资金 {D(metrics['initial'])/10000:,.2f} 万元",ha='right',fontsize=10,color=neutral)
    pct=lambda v:'—' if v is None else f'{float(v):.2%}'
    ratio=lambda v:'—' if v is None else f'{v:.3f}'
    cards=[('累计 PnL',f'{D(metrics["pnl"])/10000:,.2f} 万元'),
           ('年化收益 CAGR',pct(metrics['cagr'])), ('夏普 Sharpe',ratio(metrics['sharpe'])),
           ('卡尔玛 Calmar',ratio(metrics['calmar'])),
           ('累计收益',pct(metrics['total_return'])), ('日终最大回撤',pct(metrics['max_drawdown'])),
           ('年化波动率',pct(metrics['volatility'])),('期末权益',f'{D(metrics["final_equity"])/10000:,.2f} 万元')]
    card_colors=[change_color(metrics['pnl']), change_color(metrics['cagr']), neutral, neutral,
                 change_color(metrics['total_return']), change_color(-D(metrics['max_drawdown'])) if metrics['max_drawdown'] is not None else neutral,
                 neutral, neutral]
    for i,(label,value) in enumerate(cards):
        col,row=i%4,i//4
        x=.06+col*.225; y=.824-row*.079
        fig.add_artist(FancyBboxPatch((x,y),.212,.068,boxstyle='round,pad=0.006,rounding_size=0.008',
                                     transform=fig.transFigure,facecolor='white',edgecolor='none',zorder=0))
        fig.text(x+.012,y+.045,label,fontsize=10,color='#65758b')
        fig.text(x+.012,y+.016,value,fontsize=19,weight='bold',color=card_colors[i])
    def axis(rect):
        ax=fig.add_axes(rect, facecolor='white')
        ax.spines[['top','right','left']].set_visible(False)
        ax.spines['bottom'].set_color('#dae2eb')
        ax.grid(axis='y',color='#e8edf3',lw=.8)
        ax.set_axisbelow(True);ax.tick_params(length=0,pad=7)
        return ax
    x=[date.fromisoformat(metrics['start'])]+[date.fromisoformat(r['day']) for r in metrics['series']]
    pnl=[0]+[float(D(r['pnl'])/10000) for r in metrics['series']]
    dd=[0]+[-100*float(r['drawdown']) if r['drawdown'] is not None else float('nan') for r in metrics['series']]
    ax=axis([.075,.49,.85,.205])
    ax.set_title('累计 PnL',loc='left',fontsize=13,weight='bold',pad=13)
    ax.plot(x,pnl,color=line,lw=1.8);ax.fill_between(x,pnl,0,color=line,alpha=.07)
    ax.axhline(0,color='#a9b6c5',lw=.7)
    ax.set_ylabel('万元');ax.yaxis.set_major_formatter(FuncFormatter(lambda v,p:f'{v:,.0f}'))
    locator=mdates.AutoDateLocator(minticks=3,maxticks=8);ax.xaxis.set_major_locator(locator);ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
    ax.margins(x=.025)
    ax.text(.99,1.06,f'期末  {pnl[-1]:+,.2f} 万元',ha='right',transform=ax.transAxes,color=change_color(metrics['pnl']),
            fontsize=12,bbox=dict(facecolor='white',edgecolor='none',alpha=.9))
    ax=axis([.075,.345,.85,.095])
    ax.set_title('日终回撤',loc='left',fontsize=13,weight='bold',pad=11)
    ax.fill_between(x,dd,0,color='#d27777',alpha=.30);ax.plot(x,dd,color='#b96064',lw=.9)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v,p:f'{v:.0f}%'))
    locator=mdates.AutoDateLocator(minticks=3,maxticks=8);ax.xaxis.set_major_locator(locator);ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
    ax.margins(x=.025)
    ax.text(.99,1.14,extra,ha='right',transform=ax.transAxes,fontsize=9,color=neutral,
            bbox=dict(facecolor='white',edgecolor='none',alpha=.9))
    ax=fig.add_axes([.06,.103,.88,.188]);ax.axis('off')
    ax.set_title('年度表现',loc='left',fontsize=13,weight='bold',pad=13)
    cells=[]
    for r in metrics['annual']:
        label=str(r['year'])+(' 区间' if r['partial_year'] else '')
        cells.append([label,f'{D(r["pnl"])/10000:+,.2f}',pct(r['return_rate']),
                      pct(r['max_drawdown']),ratio(r['sharpe']),pct(r['volatility'])])
    table=ax.table(cellText=cells,colLabels=['年度','PnL / 万元','年度收益率','年内最大回撤','夏普','年化波动率'],
                   cellLoc='center',colLoc='center',bbox=[0,0,1,1],colWidths=[.13,.20,.18,.19,.14,.16])
    table.auto_set_font_size(False);table.set_fontsize(11)
    for (r,c),cell in table.get_celld().items():
        cell.set_linewidth(0)
        cell.set_facecolor('#e5ecf4' if r==0 else ('white' if r%2 else '#edf2f7'))
        if r==0: cell.set_text_props(weight='bold',color='#40536b')
        elif c in (1,2): cell.set_text_props(color=change_color(metrics['annual'][r-1]['pnl' if c==1 else 'return_rate']))
        elif c==3: cell.set_text_props(color=change_color(-D(metrics['annual'][r-1]['max_drawdown'])) if metrics['annual'][r-1]['max_drawdown'] is not None else neutral)
        else: cell.set_text_props(color=neutral)
    fig.text(.06,.065,'口径：CAGR按自然日/365.25；夏普为交易日收益、252日年化、无风险0；卡尔玛=CAGR/日终最大回撤。',fontsize=9,color='#65758b')
    fig.text(.06,.046,'年度收益按期初权益；年内回撤逐年重置高点；休市收益并入下一交易日。不完整年份标“区间”，年度收益未年化。',fontsize=9,color='#65758b')
    fig.text(.06,.027,caveat[:115],fontsize=9,color='#65758b')
    fig.savefig(destination,dpi=180,facecolor=fig.get_facecolor())
    plt.close(fig)
