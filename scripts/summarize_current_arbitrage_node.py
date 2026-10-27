"""Report the approved May-August checkpoint in the prior node's PnL layout."""
from pathlib import Path
from decimal import Decimal as D
import hashlib
import json

from backtest.factor_research import csv_if, dump
from scripts.compare_arbitrage_floor import rows, risk


def main():
    source=Path('outputs/arbitrage_dividend_may_aug_optimized_floors')
    base=Path('outputs/arbitrage_pooled_optimized_floors')
    out=Path('outputs/arbitrage_dividend_may_aug_current_node')
    out.mkdir(parents=True,exist_ok=True)
    config=json.loads((source/'config.json').read_text(encoding='utf-8'))
    assert config['dividend_season_months']==[5,6,7,8] and config['dividend_season_adjustment']
    floors={p:D(v) for p,v in config['min_expected_annual_return_by_product'].items()}
    assert floors=={'IF':D('.038'),'IH':D('.023'),'IC':D('.001'),'IM':D('.036')}
    comparisons=[]
    for label,root in [('旧节点',base),('当前节点',source)]:
        comparisons.append(dict(branch=label,**rows(root,'summary.csv')[0],**risk(root)))
    summary=comparisons[-1];yearly=rows(source,'yearly.csv');daily=rows(source,'daily.csv')
    cycles=rows(source,'cycles.csv');products=[];annual=[];previous={p:D(0) for p in floors}
    for p in ('IF','IH','IC','IM'):
        group=[c for c in cycles if c['product']==p]
        pnl=sum((D(c['pnl']) for c in group),D(0))
        assert pnl==D(summary['arbitrage_pnl_'+p])
        products.append(dict(product=p,entry_floor=floors[p],pnl=pnl,
            dividend_pnl=sum((D(c['dividend_pnl']) for c in group),D(0)),combinations=len(group)))
    for row in yearly:
        end=next(r for r in reversed(daily) if r['day'][:4]==row['year'])
        item=dict(year=row['year'])
        for p in floors:
            item[p]=D(end['pnl_'+p])-previous[p];previous[p]=D(end['pnl_'+p])
        assert sum((item[p] for p in floors),D(0))==D(row['arbitrage_pnl'])
        assert D(row['arbitrage_pnl'])+D(row['repo_interest'])==D(row['total_pnl'])
        assert D(row['closing_equity'])-D(row['opening_equity'])==D(row['total_pnl'])
        assert D(row['total_pnl'])/D(row['opening_equity'])==D(row['actual_return'])
        item.update(repo_interest=row['repo_interest'],total_pnl=row['total_pnl'],actual_return=row['actual_return'])
        annual.append(item)
    assert sum((D(r['total_pnl']) for r in yearly),D(0))==D(summary['total_pnl'])
    assert sum((r['pnl'] for r in products),D(0))+D(summary['repo_interest'])==D(summary['total_pnl'])
    csv_if(out/'comparison_summary.csv',comparisons);csv_if(out/'product_summary.csv',products)
    csv_if(out/'yearly.csv',yearly);csv_if(out/'yearly_product_pnl.csv',annual)
    old_years={r['year']:r for r in rows(base,'yearly.csv')}
    money=lambda v:f'{D(v)/10000:.2f}'
    lines=['# 当前研究节点：5—8月分红调整＋重寻额外开仓下限','',
        '用户于2026-10-19指定本版本为当前研究节点。区间2020-01-01至2026-10-08，统一初始资金1000万元；2026仅截至10月8日。',
        'IF3.8% / IH2.3% / IC0.1% / IM3.6%，下限作用于分红调整后的年化信号；信号还须严格大于当日滚动开仓阈值。',
        '此前3个完整年度同月平均分红贡献，仅预测(day,expiry]与5—8月交集；调整后重新进行35组过去一年滚动开平仓寻参，原代理评分目标保持，退出阈值入场冻结。','',
        '|指标|旧节点|当前节点|','|---|---:|---:|']
    for key,label,fmt in [('total_pnl','累计PnL（万元）',money),('final_equity','期末权益（万元）',money),
            ('cagr','CAGR',lambda v:f'{D(v):.4%}'),('max_drawdown','最大回撤',lambda v:f'{D(v):.4%}'),
            ('sharpe','夏普',lambda v:f'{float(v):.4f}'),('entries','组合份数',str),
            ('arbitrage_pnl','套利PnL含分红（万元）',money),('repo_interest','回购（万元）',money)]:
        lines.append('|'+label+'|'+'|'.join(fmt(r[key]) for r in comparisons)+'|')
    lines+=['','## 年度实际收益','',
        '|年份|套利PnL含分红（万）|回购（万）|总PnL（万）|实际收益率|旧节点收益率|',
        '|---|---:|---:|---:|---:|---:|']
    for r in yearly:
        lines.append(f"|{r['year']}|{money(r['arbitrage_pnl'])}|{money(r['repo_interest'])}|{money(r['total_pnl'])}|{D(r['actual_return']):.2%}|{D(old_years[r['year']]['actual_return']):.2%}|")
    lines+=['','年度收益率按本年PnL/年初权益；2026为截至10月8日的区间收益率，未年化。分红已包含在套利PnL中，不再加到总收益。','',
        '## 品种贡献','',
        '|品种|调整后信号额外下限|套利PnL含分红（万）|其中分红（万）|组合份数|','|---|---:|---:|---:|---:|']
    for r in products:
        lines.append(f"|{r['product']}|{r['entry_floor']:.1%}|{money(r['pnl'])}|{money(r['dividend_pnl'])}|{r['combinations']}|")
    lines+=['','四品种共享一个资金池，回购单列。年度品种收益按每日归因增量计算，见yearly_product_pnl.csv；不把平仓年份当成整笔收益所属年份。','',
        '## 执行及验证','',
        '实际ETF多头＋当月期货空头，整数手/ETF100份取整，收盘信号最早次交易日开盘；15%研究保证金预算、普通回款T+1、残余现金回购及原事后撤计划/紧急退出假设保持。',
        '1524份组合全部平仓，最大日终现金缺口和预算缺口均为0；取消10份当日计划、紧急退出21份。',
        '此前实现238项测试通过；1524份两腿及2473个自然日独立核对，当前组合21份CSV复跑逐字节一致；本报告仅整理已有结果，未重新回测。',
        '**全样本事后选参，非样本外收益；历史版本PIT、原代理评分、同日事后撤计划、完整费用/税/份额调整等限制继续有效，不是分钟统一框架验收。**','',
        '[当前节点记录](../../docs/RESEARCH_CHECKPOINT.md) · [完整分红对照](../arbitrage_dividend_may_aug_research/report.md)','',
        '![组合净值及PnL](../arbitrage_dividend_may_aug_optimized_floors/portfolio.png)']
    (out/'report.md').write_text('\n'.join(lines),encoding='utf-8')
    dump(out/'source_manifest.json',dict(source=source.as_posix(),
        sha256={name:hashlib.sha256((source/name).read_bytes()).hexdigest() for name in ('config.json','summary.csv','daily.csv','cycles.csv','yearly.csv')},
        annual_equity_and_pnl_reconciled=True,report_only=True))
    print(str(out/'report.md'))


if __name__=='__main__':main()
