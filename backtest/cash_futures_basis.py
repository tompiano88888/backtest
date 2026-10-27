"""Read-only front-future/index basis and contract-anchored ETF hedge spreads."""
from collections import defaultdict
from pathlib import Path
import hashlib
import json

from .factor_data import load_research_data
from .expiry_spread_research import observations
from .factor_research import csv_if, dump
from .futures_spread import PRODUCTS, statistics
from .models import D

PAIRS={'IF':('000300.SH','510300.SH'),'IH':('000016.SH','510050.SH'),
       'IC':('000905.SH','510500.SH'),'IM':('000852.SH','512100.SH')}


def load_spot(root='data/index_etf',version=None):
    import duckdb
    import pyarrow.parquet as pq
    root=Path(root).resolve()
    with duckdb.connect(str(root/'metadata.duckdb'),read_only=True) as db:
        if version is None:
            item=db.execute('SELECT version,manifest FROM index_etf_daily_snapshots ORDER BY created_at DESC,version DESC LIMIT 1').fetchone()
        else:
            item=db.execute('SELECT version,manifest FROM index_etf_daily_snapshots WHERE version=?',[version]).fetchone()
    if item is None:
        raise ValueError('Index/ETF snapshot not found')
    rows=[]
    for entry in json.loads(item[1]):
        path=(root/entry['path']).resolve()
        if not path.is_relative_to(root) or hashlib.sha256(path.read_bytes()).hexdigest()!=entry['sha256']:
            raise ValueError('Invalid index/ETF manifest or checksum')
        group=pq.read_table(path).to_pylist()
        if len(group)!=entry['rows']:
            raise ValueError('Index/ETF row count mismatch')
        rows.extend(group)
    if len({(r['instrument'],r['trading_date']) for r in rows})!=len(rows):
        raise ValueError('Duplicate index/ETF date')
    return item[0],rows


def pair_rows(features,prices,spot,instruments):
    indexed={(r['instrument'],r['trading_date']):r for r in spot}
    anchors={};result=[];previous={};suspect=set()
    for r in sorted(features,key=lambda r:(r['day'],r['product'])):
        product,day,code=r['product'],r['day'],r['near']
        index_code,etf_code=PAIRS[product]
        future=prices.get((code,day)); index=indexed.get((index_code,day));etf=indexed.get((etf_code,day))
        inst=instruments.get(code)
        valid_f=future is not None and future.get('close') is not None and future['close']>0
        valid_i=index is not None and index.get('close') is not None and index['close']>0
        valid_e=etf is not None and etf.get('close') is not None and etf['close']>0
        issues=[]
        if not valid_f: issues.append('missing_future_close')
        if not valid_i: issues.append('missing_index_close')
        if not valid_e: issues.append('missing_etf_close')
        if valid_e and not etf['ohlcv_complete']: issues.append('etf_ohlcv_incomplete_analysis_only')
        tracking_jump=None
        if valid_i and valid_e:
            if product in previous:
                old_i,old_e=previous[product]
                tracking_jump=(etf['close']/old_e)/(index['close']/old_i)-1
                if abs(tracking_jump)>D('.05'):
                    suspect.add(code)
                    issues.append('etf_index_relative_return_jump_gt5pct')
            previous[product]=(index['close'],etf['close'])
        if valid_f and valid_i and valid_e and code not in anchors:
            anchors[code]=(day,index['close']/etf['close'])
        anchor_day,k=anchors.get(code,(None,None))
        row=dict(product=product,trading_date=day,future=code,index=index_code,etf=etf_code,
                 expiry=inst.expiry if inst else None,dte=(inst.expiry-day).days if inst else None,
                 future_close=future['close'] if valid_f else None,index_close=index['close'] if valid_i else None,
                 etf_close=etf['close'] if valid_e else None,etf_ohlcv_complete=etf['ohlcv_complete'] if etf else False,
                 anchor_date=anchor_day,etf_scale=k,etf_index_points=None,index_basis=None,index_basis_ratio=None,
                 settlement_index_basis=None,etf_spread=None,etf_spread_ratio=None,etf_tracking_points=None,
                 raw_etf_spread=None,raw_etf_spread_ratio=None,etf_index_relative_return_jump=tracking_jump,
                 index_basis_annualized=None,etf_shares_exact=None,etf_shares_lot100=None,
                 hedge_rounding_error=None,issues=';'.join(issues))
        if valid_f and valid_i:
            row['index_basis']=future['close']-index['close']
            row['index_basis_ratio']=row['index_basis']/index['close']
            if future.get('settlement_price') is not None:
                row['settlement_index_basis']=future['settlement_price']-index['close']
            if row['dte'] is not None and row['dte']>0:
                row['index_basis_annualized']=row['index_basis_ratio']*D(365)/D(row['dte'])
        if k is not None and valid_e and valid_f:
            proxy=k*etf['close']
            row['etf_index_points']=proxy
            row['etf_spread']=future['close']-proxy
            row['etf_spread_ratio']=row['etf_spread']/proxy
            row['raw_etf_spread']=row['etf_spread']
            row['raw_etf_spread_ratio']=row['etf_spread_ratio']
            row['etf_shares_exact']=k*inst.multiplier
            row['etf_shares_lot100']=int(row['etf_shares_exact']/100)*100
            row['hedge_rounding_error']=(row['etf_shares_exact']-row['etf_shares_lot100'])*etf['close']
            if valid_i:
                row['etf_tracking_points']=proxy-index['close']
            if code in suspect:
                row['etf_spread']=row['etf_spread_ratio']=row['etf_tracking_points']=None
                issues.append('anchor_invalid_after_suspected_structural_break')
        row['issues']=';'.join(issues)
        result.append(row)
    return result


def run(output='outputs/index_futures_cash_basis',daily_version=None,metadata_version=None,spot_version=None):
    out=Path(output);out.mkdir(parents=True,exist_ok=True)
    dv,mv,raw,facts,instruments=load_research_data('data/index_futures',daily_version,metadata_version)
    sv,spot=load_spot(version=spot_version)
    features,prices,calendars=observations(raw,facts,instruments)
    rows=pair_rows(features,prices,spot,instruments)
    csv_if(out/'daily.csv',rows)
    metrics=('index_basis','index_basis_ratio','etf_spread','etf_spread_ratio','etf_tracking_points','raw_etf_spread_ratio')
    stats,yearly,dte=[],[],[]
    for product in PRODUCTS:
        group=[r for r in rows if r['product']==product]
        for metric in metrics:
            stats.append(dict(product=product,**statistics(group,metric)))
        for year in sorted({r['trading_date'].year for r in group}):
            subset=[r for r in group if r['trading_date'].year==year]
            for metric in ('index_basis_ratio','etf_spread_ratio'):
                yearly.append(dict(product=product,year=year,**statistics(subset,metric)))
        for days in sorted({r['dte'] for r in group if r['dte'] is not None}):
            subset=[r for r in group if r['dte']==days]
            for metric in ('index_basis_ratio','etf_spread_ratio'):
                dte.append(dict(product=product,dte=days,**statistics(subset,metric)))
    csv_if(out/'statistics.csv',stats);csv_if(out/'yearly.csv',yearly);csv_if(out/'by_expiry_days.csv',dte)
    csv_if(out/'missing.csv',[r for r in rows if r['issues']])
    dump(out/'provenance.json',dict(daily_version=dv,metadata_version=mv,spot_version=sv,
         source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),pairs=PAIRS,
         method='ETF scale fixed per front-contract cycle at first fully priced common date; no future prices'))
    plot(out,rows,dte)
    report=['# 当月股指期货与ETF两腿价差分析','',
            '期货报价为指数点、ETF为元/份，不能直接相减。先分析价格结构，不回测收益、不假定能融券。',
            '指数基差 = F−S；比例 = (F−S)/S。正值期货升水，负值贴水。',
            '每个近月合约周期首个期货、指数、ETF均有价格的日期设k=S/E，此后固定k。ETF换算价=k×E；两腿价差=F−k×E。该指标锚定于周期起点，衡量相对变化，不是ETF净值折溢价或独立的理论公允基差。每次换月重新锚定，会有重置跳变。',
            '恒等式：ETF两腿价差 = 指数基差 − ETF跟踪偏离，其中跟踪偏离=kE−S。锚定日ETF两腿价差与指数基差相同。实际ETF不保证与到期指数收敛。',
            '按一手期货的指数名义金额，参考ETF份额为k×期货乘数；展示按100份向下取整的误差，仅供研究，不代表交易单位或风险对冲已验收。',
            '原价不复权、不填充；分红、份额调整、跟踪误差会影响ETF序列。尚未计股息账务、融资融券、现金利息、交易成本或真实开盘可成交性，因此不能由本报告判断存在可交易套利。',
            '当ETF/指数的单日价格增长比偏离1超过5%，标记疑似结构突变，不判断具体原因；受影响近月周期此后ETF换算价差留空，下一周期重新设锚。raw_etf_spread保留未筛选结果。阈值是工程质量检查，不是已核实公司行为，也不是择优收益筛选。',
            '结算价对照是期货结算价减指数收盘，两个字段口径不同，不冒充同步可成交价。年化基差仅为描述性比值×365/dte，dte=0为空，非预期投资收益。', '',
            '|品种／ETF|价格共同区间|有效天数|指数基差均值|ETF锚定价差均值|ETF价差标准差|',
            '|---|---|---:|---:|---:|---:|']
    for product in PRODUCTS:
        a=statistics([r for r in rows if r['product']==product and r['etf_spread'] is not None],'index_basis')
        b=next(r for r in stats if r['product']==product and r['metric']=='etf_spread')
        report.append(f"|{product}／{PAIRS[product][1]}|{b['start']}～{b['end']}|{b['count']}|{a['mean']:.2f}|{b['mean']:.2f}|{b['std']:.2f}|")
    report += ['',f'固定期货行情版本 {dv}；合约元数据版本 {mv}；指数ETF版本 {sv}。',
               'daily.csv保留真实代码、原价格、锚点、换算比例和缺失原因；statistics/yearly/by_expiry_days提供分布和到期天数分组。ETF成交量缺失观察保留价格用于分析，并标记不可据此撮合；无价格则留空。历史上市、PIT公告、交易日历尚未完整核实。']
    (out/'report.md').write_text('\n'.join(report),encoding='utf-8')
    print([r for r in stats if r['metric']=='etf_spread_ratio'],flush=True)
    return rows,stats


def plot(out,rows,dte):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig,axes=plt.subplots(4,1,figsize=(14,12),constrained_layout=True)
    for product,ax in zip(PRODUCTS,axes):
        group=[r for r in rows if r['product']==product and r['etf_close'] is not None]
        ax.plot([r['trading_date'] for r in group],[float(r['index_basis']) if r['index_basis'] is not None else float('nan') for r in group],label='Future - underlying index',linewidth=.7)
        ax.plot([r['trading_date'] for r in group],[float(r['etf_spread']) if r['etf_spread'] is not None else float('nan') for r in group],label='Future - cycle-anchored ETF proxy',linewidth=.7,alpha=.8)
        ax.axhline(0,color='gray',linewidth=.6);ax.set_ylabel('Index points');ax.set_title(f'{product} / {PAIRS[product][1]}',loc='left');ax.legend()
    fig.savefig(out/'basis_trends.png',dpi=160);plt.close(fig)
    fig,axes=plt.subplots(2,2,figsize=(12,8),constrained_layout=True)
    for product,ax in zip(PRODUCTS,axes.flat):
        for metric in ('index_basis_ratio','etf_spread_ratio'):
            group=[r for r in dte if r['product']==product and r['metric']==metric and r['count']>=10]
            ax.plot([r['dte'] for r in group],[100*float(r['mean']) for r in group],label=metric)
        ax.axhline(0,color='gray');ax.set_title(product);ax.set_xlabel('Calendar days to expiry');ax.set_ylabel('Mean spread (%)');ax.legend()
    fig.savefig(out/'expiry_profiles.png',dpi=160);plt.close(fig)
