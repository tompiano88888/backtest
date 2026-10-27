"""Describe actual counter observations; never invent simulation performance."""
import argparse
import csv
import json
from decimal import Decimal as D
from pathlib import Path
from statistics import median


def summarize(state,out):
    state=Path(state);out=Path(out);out.mkdir(parents=True,exist_ok=True)
    path=state/'counter_account.jsonl'
    rows=[json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()] if path.is_file() else []
    usable=[r for r in rows if r.get('margin_comparison_usable')]
    ratios=[D(r['margin_actual_to_estimated']) for r in usable]
    errors=[abs(D(r['margin_difference']))/D(r['estimated_margin']) for r in usable]
    last_by_day={r['trading_day']:r for r in rows}
    summary=dict(counter_observations=len(rows),usable_margin_comparisons=len(usable),
        median_actual_to_estimated=str(median(ratios)) if ratios else None,
        mean_absolute_relative_error=str(sum(errors)/len(errors)) if errors else None,
        maximum_absolute_relative_error=str(max(errors)) if errors else None,
        within_10_percent_fraction=sum(e<=D('.1') for e in errors)/len(errors) if errors else None,
        last_counter_day_observations=last_by_day,
        status='no_live_observations' if not rows else 'observational_report_not_settlement_certification')
    (out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    fields=['trading_day','queried_at','equity','margin','estimated_margin','margin_difference',
            'margin_actual_to_estimated','implied_uniform_margin_rate','risk','frozen_margin','available','commission',
            'margin_comparison_usable','day_pnl']
    with (out/'margin_comparison.csv').open('w',encoding='utf-8-sig',newline='') as h:
        writer=csv.DictWriter(h,fieldnames=fields,extrasaction='ignore');writer.writeheader();writer.writerows(rows)
    text='# 无限易模拟盘资金估算核对\n\n'
    if not rows:
        text+='尚无实际柜台观察，不能判断15%估算是否准确，也不能报告模拟盘PnL。\n'
    else:
        text+=f'柜台资金观察{len(rows)}次，其中同步持仓且行情新鲜、可比较保证金的观察{len(usable)}次。\n\n'
        if ratios:
            text+=f'实际/估算比值中位数：{median(ratios):.4f}；平均绝对相对偏差：{sum(errors)/len(errors):.2%}；最大偏差：{max(errors):.2%}。\n'
        else:text+='尚无可用的持仓保证金比较，不能判断估计准确性。\n'
    text+='\n估算沿用原回测15%及单向大边口径。只与柜台已占保证金比较，冻结金额另列；折算统一保证金率并非单合约实际费率。柜台缓存与行情时点可能不同，此报告不证明Tick级资金充足。day_pnl为柜台动态权益减上日余额并调整当日出入金，不是另建的现金账本，仍需结算单核对。\n'
    (out/'report.md').write_text(text,encoding='utf-8')
    return summary


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--state',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();print(json.dumps(summarize(a.state,a.output),ensure_ascii=False,indent=2))
