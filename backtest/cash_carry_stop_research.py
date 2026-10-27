"""Compare unchanged carry, near-expiry entry filter and approved time-dependent stops."""
from pathlib import Path
import hashlib
import json

from .cash_futures_basis import load_spot
from .cash_carry_signals import make_signals
from .cash_carry_pnl import replay,read_cycles
from .expiry_spread_research import observations
from .factor_data import load_research_data
from .factor_research import csv_if,dump
from .futures_spread import PRODUCTS
from .models import D


def run(output='outputs/index_futures_cash_carry_stop',source='outputs/index_futures_cash_carry_signals'):
    out=Path(output);out.mkdir(parents=True,exist_ok=True);source=Path(source)
    config=json.loads((source/'config.json').read_text(encoding='utf-8'))
    _,_,raw,facts,inst=load_research_data('data/index_futures',config['daily_version'],config['metadata_version'])
    _,spot=load_spot(version=config['spot_version'])
    features,prices,calendars=observations(raw,facts,inst)
    days=sorted({r['trading_date'] for r in raw})
    comparisons=[]
    for variant,filtering,stops in [('original',False,False),('filter',True,False),('filter_stop',True,True)]:
        for cost,rate,ticks in [('base','0.00005',1),('stress','0.0001',2)]:
            signals,cycles,issues=make_signals(features,prices,spot,inst,config['min_history'],filtering,stops,rate,ticks)
            label=f'{variant}_{cost}'
            csv_if(out/f'{label}_signals.csv',signals)
            csv_if(out/f'{label}_signal_cycles.csv',cycles)
            csv_if(out/f'{label}_issues.csv',issues)
            result=replay(read_cycles(out/f'{label}_signal_cycles.csv'),prices,spot,inst,days,rate,ticks)
            for kind in ('daily','cycles','fills','entries'):csv_if(out/f'{label}_{kind}.csv',result[kind])
            dump(out/f'{label}_terminal_positions.json',result['terminal_positions'])
            for product in PRODUCTS:
                group=[r for r in result['cycles'] if r['product']==product]
                closed=[r for r in group if r['exit_day'] is not None]
                observations_=[r for r in cycles if r['product']==product]
                comparisons.append(dict(variant=variant,cost=cost,product=product,cycles=len(group),
                    raw_price_pnl=sum((r['raw_price_pnl'] for r in group),D(0)),
                    zero_cost_pnl=sum((r['zero_cost_pnl'] for r in group),D(0)),
                    fees=sum((r['fees'] for r in group),D(0)),slippage=sum((r['slippage'] for r in group),D(0)),
                    stop_exits=sum(r['exit_reason']=='stop_loss' for r in group),
                    mean_exits=sum(r['exit_reason']=='historical_mean' for r in group),
                    expiry_exits=sum(r['exit_reason']=='expiry' for r in group),
                    open_at_end=sum(r['exit_reason']=='open_at_data_end' for r in group),
                    negative_entries=sum(D(r['entry_rate'])<0 for r in group),
                    wins=sum(r['raw_price_pnl']>0 for r in closed),closed_cycles=len(closed),
                    flagged_cycles=sum(r['suspected_price_jump'] for r in group)))
            print(label,[(r['product'],str(r['raw_price_pnl'])) for r in comparisons if r['variant']==variant and r['cost']==cost],flush=True)
    csv_if(out/'comparison.csv',comparisons)
    dump(out/'config.json',dict(signal_config=config,filter='signal and entry calendar dte <5 and previous signal annualized rate <0',
          stop_levels={'dte>=15':'.01','5<=dte<15':'.005','dte<5':'.0025'},
          stop_mark='short future entry price minus current settlement plus long ETF close minus entry; entry costs included; exit costs not yet incurred',
          denominator='ETF quantity * actual raw entry open; fixed during cycle',
          priority='stop before mean; expiry final settlement; retry stop exits persist',
          cost_scenarios='recompute stop triggers under their own entry costs and slippage',
          source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
          signal_source_sha256=hashlib.sha256(Path(__file__).with_name('cash_carry_signals.py').read_bytes()).hexdigest()))
    report=['# 正向期现：近到期过滤与分期限止损','',
          '同此前三份快照、累计历史均值、252日预热、一手期货空仓和ETF信号日指数名义对冲份额（向下取100份）。不调参选优，分别比较原规则、仅过滤、过滤＋用户指定止损。',
          '不足5个自然日且年化价差率<0，不开仓；信号日和实际开仓日均检查，实际开仓日使用此前已知信号率。恰好5天不属于过滤范围，零收益率不被过滤。其他日期的负收益率入场仍允许。',
          '止损按原实际合约当前剩余自然日数：≥15天1%，5–14天0.5%，不足5天0.25%。当日两腿累计盈亏÷入场ETF原始名义金额≤负阈值即触发。期货用当日结算价、ETF用收盘价，包含已发生开仓费用滑点，不预扣未发生退出费用。',
          '触发后最早下一观察日开盘平两腿；缺行情持续保留退出，止损优先于均值。到期仍按期货现金结算＋ETF收盘退出；期末仅估值。跳空、下一日成交成本和未核实公司行为可导致实际损失超过止损阈值。',
          '每腿每次费用0.5bp＋1tick，压力加倍；ETF tick0.001、lot100、T+1仍为研究假设。压力场景按自己的入场成本重算止损信号，因此成交周期可能不同。',
          '**原价PnL诊断：未计ETF现金分红、份额调整和融资成本，不是已验收的总回报。疑似ETF价格突变不删除。日线全额成交不验证真实容量或部分成交。**','',
          '|规则|成本|品种|周期数|止损退出|均值退出|到期退出|原价PnL（元）|',
          '|---|---|---|---:|---:|---:|---:|---:|']
    for r in comparisons:
        report.append(f"|{r['variant']}|{r['cost']}|{r['product']}|{r['cycles']}|{r['stop_exits']}|{r['mean_exits']}|{r['expiry_exits']}|{r['raw_price_pnl']:.2f}|")
    report+=['','comparison.csv为全部对照；各配置输出逐日信号、止损估值、触发阈值、周期、成交、账务、权益及期末持仓。']
    (out/'report.md').write_text('\n'.join(report),encoding='utf-8')
    return comparisons
