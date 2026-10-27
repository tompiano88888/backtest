"""Read-only comparison of historical research outputs with an entry-return floor."""
import argparse
import csv
import json
import math
from decimal import Decimal as D
from pathlib import Path


def rows(root,name):
    with (root/name).open(encoding='utf-8-sig',newline='') as stream:
        return list(csv.DictReader(stream))


def risk(root):
    daily=rows(root,'daily.csv');previous=D(rows(root,'summary.csv')[0]['initial_capital']);peak=previous;drawdown=D(0);returns=[]
    for r in daily:
        equity=D(r['equity']);peak=max(peak,equity);drawdown=max(drawdown,1-equity/peak)
        if r['is_market_day']=='True':
            returns.append(float(equity/previous-1));previous=equity
    mean=sum(returns)/len(returns)
    std=math.sqrt(sum((v-mean)**2 for v in returns)/(len(returns)-1))
    return dict(max_drawdown=str(drawdown),sharpe=mean/std*math.sqrt(252))


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',default='outputs/arbitrage_min_expected_013')
    parser.add_argument('--baseline',default='outputs/best_arbitrage_reproduction_20261016')
    args=parser.parse_args();root=Path(args.output);base=Path(args.baseline)
    new=rows(root,'summary.csv')[0];old=rows(base,'summary.csv')[0]
    config=json.loads((root/'config.json').read_text(encoding='utf-8'))
    floor=D(config['min_expected_annual_return']) if config['min_expected_annual_return'] is not None else None
    per_product=config.get('min_expected_annual_return_by_product',{})
    floors={p:D(v) for p,v in per_product.items()}
    floor_description='、'.join(f'{p} {v:.1%}' for p,v in floors.items()) if floors else f'{floor:.2%}'
    basis=config.get('minimum_return_basis','exit_target')
    measure='rate' if basis=='zero_convergence' else 'expected_gap'
    label='信号日年化升水率（按到期归零，不减平仓阈值）' if basis=='zero_convergence' else '信号日年化升水率−选定平仓年化阈值'
    plans=rows(root,'candidate_plans.csv')
    assert all(D(p[measure])>=floors.get(p['product'],floor) for p in plans)
    assert (root/'selections.csv').read_bytes()==(base/'selections.csv').read_bytes()
    current_risk=risk(root);old_risk=risk(base)
    (root/'risk_summary.json').write_text(json.dumps(current_risk,indent=2),encoding='utf-8')
    money=lambda v:f'{D(v)/10000:.2f}'
    lines=['# 预期开仓年化收益下限对照','',
      f"统计区间{new['start']}至{new['end']}；四品种、1000万元统一资金池、实际ETF、分红和回购。",
      f'开仓额外下限：{floor_description}；{label}不低于本品种下限才生成开仓计划，等于允许。排序仍按年化升水减平仓阈值，平仓规则不变。',
      '原滚动35组评分及平仓条件保持不变；逐日选参文件与基准完全一致。所用年化信号不是保证到手收益，也未转换为资本占用收益率。','',
      '|指标|基准|新增门槛|','|---|---:|---:|',
      f"|累计PnL（万元）|{money(old['total_pnl'])}|{money(new['total_pnl'])}|",
      f"|期末权益（万元）|{money(old['final_equity'])}|{money(new['final_equity'])}|",
      f"|CAGR|{D(old['cagr']):.4%}|{D(new['cagr']):.4%}|",
      f"|最大回撤|{D(old_risk['max_drawdown']):.2%}|{D(current_risk['max_drawdown']):.2%}|",
      f"|夏普（252，零无风险）|{old_risk['sharpe']:.3f}|{current_risk['sharpe']:.3f}|",
      f"|组合份数|{old['entries']}|{new['entries']}|",
      f"|套利PnL含分红（万元）|{money(old['arbitrage_pnl'])}|{money(new['arbitrage_pnl'])}|",
      f"|回购收益（万元）|{money(old['repo_interest'])}|{money(new['repo_interest'])}|",'',
      '|年份|原PnL（万元）|新PnL（万元）|原收益率|新收益率|','|---|---:|---:|---:|---:|']
    original={r['year']:r for r in rows(base,'yearly.csv')}
    for n in rows(root,'yearly.csv'):
        o=original[n['year']]
        lines.append(f"|{n['year']}|{money(o['total_pnl'])}|{money(n['total_pnl'])}|{D(o['actual_return']):.2%}|{D(n['actual_return']):.2%}|")
    lines+=['','2026为截至10月8日收益，非全年；年度收益按年初权益计算。','',
      '|品种|原套利PnL（万元）|新套利PnL（万元）|变化（万元）|','|---|---:|---:|---:|']
    for p in ('IF','IH','IC','IM'):
        k='arbitrage_pnl_'+p
        lines.append(f"|{p}|{money(old[k])}|{money(new[k])}|{money(D(new[k])-D(old[k]))}|")
    lines+=['','新增门槛属于旧独立日频研究的历史对照，不代表统一分钟框架验收。原有事后取消当天计划的回看偏差、元数据历史可知时间及完整历史费用/份额调整限制保留；不是完整可实盘净收益。',
      '资金检查、逐笔账务及复跑校验详见verification.json；门槛不满足记录在资金日志，其他明细与图见本目录。','',
      '复跑命令：','```powershell',
      f'.venv\\Scripts\\python.exe -m scripts.test_pooled_arbitrage --funding-exit --etf --cancel-new-openings --dividends '+('--entry-floors configs/arbitrage_entry_floor_best.json' if floors else f'--min-expected-annual-return {floor}')+f' --minimum-return-basis {basis} --output {root.as_posix()}',
      '```','', '![PnL及净值](portfolio.png)']
    (root/'comparison.md').write_text('\n'.join(lines),encoding='utf-8')
    print(json.dumps(dict(summary=new,risk=current_risk,pnl_improvement=str(D(new['total_pnl'])-D(old['total_pnl']))),ensure_ascii=False))


if __name__=='__main__':
    main()
