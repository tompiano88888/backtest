"""Statistics and figures from verified, causally labelled checkpoint trades."""
from collections import defaultdict
import argparse,csv,json,math,statistics
from decimal import Decimal as D
from pathlib import Path

LABELS=dict(breakout='突破',reversal='反转',end_of_day='日终退出',main_roll='换主力退出',
            stop_atr_price='ATR止损',stop_trade_risk_budget='风险预算止损',open_at_end='期末持仓',
            new_open='新开',unlock='解锁',mixed='混合',lock='对锁退出',direct_close='直接平仓',long='多头',short='空头')


def read(path):
    with Path(path).open(encoding='utf-8-sig',newline='') as h:return list(csv.DictReader(h))


def write(path,rows):
    with Path(path).open('w',encoding='utf-8-sig',newline='') as h:
        w=csv.DictWriter(h,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)


def wilson(k,n,z=1.959963984540054):
    if not n:return None,None
    p=k/n;denom=1+z*z/n
    center=(p+z*z/(2*n))/denom
    half=z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/denom
    return max(0,center-half),min(1,center+half)


def stats(trades):
    closed=[r for r in trades if r['closed']=='True'];n=len(trades);nc=len(closed)
    pnl=[D(r['net_pnl']) for r in closed]
    all_pnl=sum((D(r['net_pnl']) for r in trades),D(0))
    gain=sum((p for p in pnl if p>0),D(0));loss=-sum((p for p in pnl if p<0),D(0))
    stops=[r for r in trades if r['stopped']=='True'];sc=[r for r in closed if r['stopped']=='True']
    stop_pnl=sum((D(r['net_pnl']) for r in sc),D(0))
    stopped_losses=[r for r in sc if D(r['net_pnl'])<0]
    stop_loss=-sum((D(r['net_pnl']) for r in stopped_losses),D(0))
    rs=[D(r['net_pnl'])/D(r['budget']) for r in closed]
    low,high=wilson(len(sc),nc)
    return dict(trades=n,closed=nc,open=n-nc,net_pnl=all_pnl,closed_net_pnl=sum(pnl,D(0)),
                open_mtm_pnl=all_pnl-sum(pnl,D(0)),wins=sum(p>0 for p in pnl),losses=sum(p<0 for p in pnl),
                win_rate=sum(p>0 for p in pnl)/nc if nc else None,
                stops=len(stops),closed_stops=len(sc),stop_rate_observed=len(stops)/n if n else None,
                stop_rate_closed=len(sc)/nc if nc else None,stop_rate_wilson_low=low,stop_rate_wilson_high=high,
                average_pnl=sum(pnl,D(0))/nc if nc else None,median_pnl=statistics.median(pnl) if pnl else None,
                gross_winning_pnl=gain,gross_losing_pnl=loss,profit_factor=gain/loss if loss else None,
                average_win=gain/sum(p>0 for p in pnl) if gain else None,
                average_loss=loss/sum(p<0 for p in pnl) if loss else None,
                average_r=sum(rs,D(0))/nc if nc else None,median_r=statistics.median(rs) if rs else None,
                worst_r=min(rs) if rs else None,best_r=max(rs) if rs else None,
                average_holding_sessions=sum(int(r['holding_sessions']) for r in closed)/nc if nc else None,
                overnight_fraction=sum(int(r['holding_sessions'])>1 for r in closed)/nc if nc else None,
                stopped_net_pnl=stop_pnl,stopped_gross_loss=stop_loss,
                stopped_share_of_losses=stop_loss/loss if loss else None,
                stop_probability_given_loss=len(stopped_losses)/sum(p<0 for p in pnl) if loss else None,
                losses_beyond_budget=sum(D(r['net_pnl']) < -D(r['budget']) for r in closed),
                fees=sum((D(r['fees']) for r in trades),D(0)))


def group(trades,keys):
    groups=defaultdict(list)
    for r in trades:groups[tuple(r[k] for k in keys)].append(r)
    order={v:i for i,v in enumerate(('IF','IH','IC','IM','breakout','reversal','end_of_day','main_roll','stop_atr_price','stop_trade_risk_budget','open_at_end'))}
    return [{**dict(zip(keys,k)),**stats(v)} for k,v in sorted(groups.items(),key=lambda item:tuple((order.get(v,99),v) for v in item[0]))]


def fmt(value,kind='number'):
    if value is None:return '—'
    if kind=='pct':return f'{float(value):.2%}'
    if kind=='wan':return f'{float(value)/10000:,.2f}'
    if kind=='r':return f'{float(value):.3f}'
    if kind=='label':return LABELS.get(str(value),str(value))
    return f'{float(value):,.2f}' if isinstance(value,(float,D)) else str(value)


def table(rows,columns):
    text='|'+'|'.join(c[1] for c in columns)+'|\n|'+'|'.join('---' for c in columns)+'|\n'
    for row in rows:text+='|'+'|'.join(fmt(row.get(key),kind) for key,label,kind in columns)+'|\n'
    return text


def run(root,render=True):
    root=Path(root);trades=read(root/'trade_labels.csv')
    verify=json.loads((root/'classification_verification.json').read_text())
    assert verify['status']=='passed'
    overall=stats(trades)
    for row in trades:row['holding_bucket']='overnight' if int(row['holding_sessions'])>1 else 'intraday'
    assert str(overall['net_pnl'])==verify['logical_trade_pnl']
    dimensions={'by_entry':['entry_type'],'by_product':['product'],'product_entry':['product','entry_type'],
                'by_exit':['exit_type'],'entry_exit':['entry_type','exit_type'],
                'product_entry_exit':['product','entry_type','exit_type'],
                'product_entry_direction':['product','entry_type','direction'],
                'entry_execution':['entry_type','entry_execution'],
                'product_entry_execution':['product','entry_type','entry_execution'],
                'exit_execution':['exit_execution'],'entry_exit_execution':['entry_type','exit_execution'],
                'by_entry_year':['entry_year','entry_type'],'entry_holding':['entry_type','holding_bucket']}
    results={name:group(trades,keys) for name,keys in dimensions.items()}
    write(root/'overall.csv',[overall])
    for name,values in results.items():
        assert sum(r['trades'] for r in values)==len(trades)
        assert sum((r['net_pnl'] for r in values),D(0))==overall['net_pnl']
        assert sum(r['stops'] for r in values)==overall['stops']
        write(root/(name+'.csv'),values)
    closed=[t for t in trades if t['closed']=='True']
    write(root/'best_trades.csv',sorted(closed,key=lambda t:D(t['net_pnl']),reverse=True)[:20])
    write(root/'worst_trades.csv',sorted(closed,key=lambda t:D(t['net_pnl']))[:20])
    write(root/'stop_trades.csv',[t for t in trades if t['stopped']=='True'])
    winners=sorted((D(t['net_pnl']) for t in closed if D(t['net_pnl'])>0),reverse=True)
    concentration={str(k):sum(winners[:k],D(0))/sum(winners,D(0)) for k in (1,5,10,20)}
    account=json.loads((Path(verify['source'])/'summary.json').read_text())
    product_reconciliation=[]
    for product,account_pnl in account['product_pnl'].items():
        signal_pnl=sum((D(t['net_pnl']) for t in trades if t['product']==product),D(0))
        product_reconciliation.append(dict(product=product,logical_direction_pnl=signal_pnl,
                                           inventory_cleanup_adjustment=D(account_pnl)-signal_pnl,account_pnl=D(account_pnl)))
    assert sum((r['inventory_cleanup_adjustment'] for r in product_reconciliation),D(0))==D(verify['inventory_cleanup_adjustment'])
    write(root/'product_reconciliation.csv',product_reconciliation)
    summary=dict(overall=overall,groups=results,top_winner_share_of_gross_wins=concentration,verification=verify,product_reconciliation=product_reconciliation,
                 definitions=dict(net_pnl='logical direction PnL; includes final marked open trades; excludes separately reconciled physical inventory cleanup',
                                  win_rate='profitable closed direction trades / all closed direction trades',
                                  stop_rate_observed='triggered stops / all observed direction trades including open trades',
                                  stop_rate_closed='triggered stops in closed direction trades / all closed direction trades',
                                  r='net PnL / frozen initial risk budget',exit_type='stop takes precedence; otherwise first exit decision; full transition history retained',
                                  by_entry_year='entire lifecycle PnL assigned to entry year; not account calendar-year performance'))
    (root/'statistics.json').write_text(json.dumps(summary,ensure_ascii=False,default=str,indent=2),encoding='utf-8')
    text='# R-Breaker突破、反转与退出形态分析\n\n'
    text+='基于2026-10-20封存的历史手续费对锁节点，覆盖2020-01-02至2026-10-08。保持当前节点、信号与对锁路由不变，仅回放保存的真实成交和当时可见行情补齐分类。\n\n'
    text+=f'共{overall["trades"]}笔逻辑方向交易，{overall["closed"]}笔已结束、{overall["open"]}笔期末仍持有，触发止损{overall["stops"]}笔。观察触发率{overall["stop_rate_observed"]:.2%}，已结束样本触发率{overall["stop_rate_closed"]:.2%}。方向净PnL {overall["net_pnl"]:,.2f}元，加库存清理调整{D(verify["inventory_cleanup_adjustment"]):,.2f}元，等于账户净PnL {D(verify["account_pnl"]):,.2f}元。\n\n'
    text+='## 统计口径\n\n'
    text+='入场类型在原回调冻结入场风险预算时确定，反转等待平旧再入场的状态也保留，未按成交价事后猜测。退出类型分为反转、日终、换主力、ATR止损、风险预算止损；若先有其他退出意图、之后又触发止损，则主分类归止损，完整变更历史见exit_decisions.csv。实际执行方式另分直接平仓、对锁和混合，不能把保护性反向开仓再计成反转信号入场。\n\n'
    text+='净PnL包含期末未平仓估值；胜率、平均盈亏、盈亏因子和平均R仅用已结束交易。R=净PnL/该笔冻结风险预算，便于比较权益及手数不同的交易。止损率同时输出全体观察样本和已结束样本分母；置信区间是已结束样本的Wilson描述区间，未假定交易独立或作为未来概率保证。\n\n'
    cols=[('entry_type','入场','label'),('trades','笔数','number'),('closed','结束','number'),('net_pnl','净PnL万元','wan'),('win_rate','胜率','pct'),('stops','止损笔数','number'),('stop_rate_observed','观察止损率','pct'),('average_r','平均R','r'),('profit_factor','盈亏因子','r'),('average_holding_sessions','平均持有交易日','number')]
    text+='## 两种入场总体表现\n\n'+table(results['by_entry'],cols)+'\n'
    text+='## 分品种与入场类型\n\n'+table(results['product_entry'],[('product','品种','label')]+cols)+'\n'
    text+='下表将各品种方向PnL与真实账户归因核对，库存清理调整不强行分配给突破或反转：\n\n'
    text+=table(product_reconciliation,[('product','品种','label'),('logical_direction_pnl','方向PnL万元','wan'),('inventory_cleanup_adjustment','库存调整万元','wan'),('account_pnl','账户PnL万元','wan')])+'\n'
    text+='## 入场类型与退出原因\n\n'+table(results['entry_exit'],[('entry_type','入场','label'),('exit_type','退出原因','label'),('trades','笔数','number'),('net_pnl','净PnL万元','wan'),('win_rate','胜率','pct'),('average_r','平均R','r')])+'\n'
    text+='## 品种、入场和退出三维交叉\n\n'+table(results['product_entry_exit'],[('product','品种','label'),('entry_type','入场','label'),('exit_type','退出原因','label'),('trades','笔数','number'),('net_pnl','净PnL万元','wan'),('win_rate','胜率','pct'),('average_r','平均R','r')])+'\n'
    text+='## 多空方向\n\n'+table(results['product_entry_direction'],[('product','品种','label'),('entry_type','入场','label'),('direction','方向','label'),('trades','笔数','number'),('net_pnl','净PnL万元','wan'),('win_rate','胜率','pct'),('stop_rate_observed','观察止损率','pct'),('average_r','平均R','r')])+'\n'
    text+='## 实际执行方式\n\n'+table(results['entry_exit_execution'],[('entry_type','入场','label'),('exit_execution','退出执行','label'),('trades','笔数','number'),('net_pnl','净PnL万元','wan'),('win_rate','胜率','pct'),('average_r','平均R','r')])+'\n'
    text+='执行方式按已经发生的实际退出动作分类，含部分退出后期末仍持有的交易；closed字段另行标记是否完整结束。方式由当日开仓余额、库存及持有天数共同决定，分组收益是条件描述，不能解释为直接平仓或对锁本身的因果收益。\n\n'
    text+='## 止损与尾部损失\n\n'
    text+=f'已结束交易中共有{overall["losses"]}笔亏损，止损交易累计净PnL {overall["stopped_net_pnl"]:,.2f}元；止损负收益占全部亏损交易毛亏损的{overall["stopped_share_of_losses"]:.2%}，亏损交易中触发止损的比例{overall["stop_probability_given_loss"]:.2%}。有{overall["losses_beyond_budget"]}笔亏损超过冻结风险预算，不能把1%当作保证亏损上限。\n\n'
    text+='全部91条止损记录的主标签是ATR触价，没有风险预算单独触发的记录；原逻辑在两者同时满足时优先记录ATR标签，不能据此断言风险预算条件从未满足。\n\n'
    text+='## 实际跨日持有\n\n'+table(results['entry_holding'],[('entry_type','入场','label'),('holding_bucket','实际持有','label'),('trades','笔数','number'),('net_pnl','净PnL万元','wan'),('stop_rate_observed','观察止损率','pct'),('average_r','平均R','r')])+'\n'
    overnight_reversals=[t for t in closed if t['entry_type']=='reversal' and int(t['holding_sessions'])>1]
    assert all(t['exit_decision_time'][:10]==t['entry_time'][:10] for t in overnight_reversals)
    text+=f'有{len(overnight_reversals)}笔反转交易实际跨日，但其首次退出意图均在入场当天已经发出。这些属于退出未完成而被动跨日，不能描述为策略主动允许反转仓隔夜。突破持仓的较长观察时间也会影响逐笔止损率比较。\n\n'
    text+='持有时长和退出类型均是交易结束后才能完整确定的分组。仍有效的突破更可能获得主动隔夜资格，日内组包括提前失败退出的突破，存在存活和事后选择特征；不能据此在开仓时直接剔除“日内组”，也不能把全部跨日组收益归因于隔夜本身。\n\n'
    text+='## 按入场年份\n\n'+table(results['by_entry_year'],[('entry_year','入场年','label')]+cols)+'\n'
    text+='年度表把整笔方向交易归入入场年，跨年交易不拆分，因此不等同原账户按每日估值统计的年度收益。IM自2022年才有样本。\n\n'
    text+='## 解读与下一步\n\n'
    for r in results['by_entry']:
        text+=f'- {LABELS[r["entry_type"]]}：{r["trades"]}笔，净PnL {r["net_pnl"]/10000:,.2f}万元，已结束胜率{r["win_rate"]:.2%}，平均R {r["average_r"]:.3f}，观察止损率{r["stop_rate_observed"]:.2%}。\n'
    text+=f'- 前5／10笔最大盈利占全部盈利交易毛利润的{concentration["5"]:.2%}／{concentration["10"]:.2%}。\n'
    text+='- 当前设计只允许仍在突破位外的突破模式仓主动隔夜，反转模式通常日终退出；两种类型的持有机会本就不同，不能仅按胜率比较优劣。\n'
    text+='- 分组归因不是“删去某类信号”的独立策略回测。取消反转入场、变更反转退出或调整隔夜会改变此后持仓、锁仓库存、资金和信号路径，需保留当前对锁原则另设对照，不从本统计直接推导优化收益。\n\n'
    text+='### 结合品种、退出与年份的观察\n\n'
    text+='- 突破贡献方向净PnL的84.72%，但其胜率只有37.99%，依赖较大的盈利交易覆盖较多亏损。反转贡献15.28%，胜率53.36%、盈亏因子1.439，收益规模较小但不能据此认定无效。\n'
    text+='- IF反转136笔仅贡献9.43万元，盈亏因子1.041，接近盈亏平衡。IH反转贡献123.71万元、盈亏因子1.787；IM反转贡献126.41万元、盈亏因子2.577，但IM反转仅75笔，止损仅1次，发生率估计仍不稳定。\n'
    text+='- IF突破空头237笔累计−108.43万元，平均−0.017R；IC反转空头55笔累计−12.73万元，平均−0.006R，是值得单独核验的弱分组。它们是样本内定位，并非已经证明删除后更优。\n'
    text+='- 突破后因反转退出的318笔累计−1633.80万元，其中IH占−616.74万元；突破ATR止损83笔累计−1643.97万元，IF/IH分别为−650.56/−557.57万元。反转退出通常是在确认之前的方向已失效，不能把旧仓亏损直接解释为反转新开仓无效或应取消保护退出。\n'
    text+='- 按入场年份归属，2024突破+1202.52万元、反转−93.72万元；2025突破−284.14万元、反转+195.29万元，两类入场在不同阶段存在互补。跨年交易整笔归入入场年，不是账户年度收益表。\n'
    text+='- 优先研究的假设是IF突破空头、IC反转空头的入场过滤，以及失败突破的更早识别；IH/IM反转保留为比较组。任何新信号测试继续固定当前对锁、历史手续费与真实资金口径，另存结果，不覆盖节点。\n\n'
    text+='## 核验\n\n'
    text+=f'封存{verify["sealed_files_verified"]}份文件SHA256一致，原回调在{verify["frames_replayed"]}个保存事件上只读回放。所有{verify["all_trade_fields_matched"]}笔交易的原始字段、{verify["stop_events_matched"]}条止损事件和{verify["allocation_decisions_matched"]}条定仓决策逐项相等。未重新撮合、修改手续费或改变节点。细节见classification_verification.json及trade_labels.csv。\n'
    (root/'report.md').write_text(text,encoding='utf-8')
    if render:plot(root,results)
    print(json.dumps(dict(overall=overall,by_entry=results['by_entry'],product_entry=results['product_entry'],by_exit=results['by_exit']),default=str,ensure_ascii=False,indent=2))


def plot(root,results):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    products=['IF','IH','IC','IM'];types=['breakout','reversal'];colors=['#2563A6','#CF7924']
    groups={(r['product'],r['entry_type']):r for r in results['product_entry']}
    fig,axes=plt.subplots(2,2,figsize=(14,10),layout='constrained')
    x=np.arange(4);width=.34
    for i,t in enumerate(types):
        xs=x+(i-.5)*width
        vals=[float(groups[p,t]['net_pnl'])/10000 for p in products]
        bars=axes[0,0].bar(xs,vals,width,color=colors[i],label=t.title())
        axes[0,0].bar_label(bars,labels=[f'{v:,.0f}' for v in vals],fontsize=9,padding=3)
        axes[0,1].bar(xs,[float(groups[p,t]['average_r']) for p in products],width,color=colors[i],label=t.title())
    for ax in axes[0]:ax.set_xticks(x,products);ax.axhline(0,color='#555555',lw=.6);ax.grid(axis='y',alpha=.18)
    axes[0,0].set_title('Net PnL by product and entry type');axes[0,0].set_ylabel('CNY 10,000');axes[0,0].legend()
    axes[0,1].set_title('Mean net R per completed direction trade');axes[0,1].set_ylabel('Net PnL / frozen risk budget')
    labels=[]
    for j,(p,t) in enumerate((p,t) for p in products for t in types):
        r=groups[p,t];rate=r['stop_rate_closed']*100;lo=r['stop_rate_wilson_low']*100;hi=r['stop_rate_wilson_high']*100
        axes[1,0].errorbar(rate,j,xerr=[[rate-lo],[hi-rate]],fmt='o',color=colors[types.index(t)],capsize=3)
        labels.append(f'{p} {t[:3]} ({r["closed_stops"]}/{r["closed"]})')
    axes[1,0].set_yticks(range(len(labels)),labels);axes[1,0].invert_yaxis();axes[1,0].grid(axis='x',alpha=.18)
    axes[1,0].set_title('Stop incidence: completed trades, Wilson 95% interval');axes[1,0].set_xlabel('Stop-trigger rate (%)')
    exits=['reversal','end_of_day','main_roll','stop_atr_price','stop_trade_risk_budget']
    e={(r['entry_type'],r['exit_type']):r for r in results['entry_exit']}
    for i,t in enumerate(types):
        axes[1,1].bar(np.arange(len(exits))+(i-.5)*width,[float(e.get((t,k),{}).get('net_pnl',0))/10000 for k in exits],width,color=colors[i],label=t.title())
    axes[1,1].set_xticks(range(len(exits)),['Reverse','Day-end','Roll','ATR stop','Risk stop'],rotation=20)
    axes[1,1].set_title('PnL by first exit reason (stops take precedence)');axes[1,1].set_ylabel('CNY 10,000')
    axes[1,1].axhline(0,color='#555555',lw=.6);axes[1,1].grid(axis='y',alpha=.18)
    fig.suptitle('R-Breaker current checkpoint: entry patterns and stop incidence\nHistorical fees and physical lock routing preserved',fontsize=14)
    fig.savefig(root/'entry_exit_diagnostics.png',dpi=160);plt.close(fig)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',type=Path,default=Path('outputs/rbreaker_signal_diagnostics_20261022'))
    p.add_argument('--no-plot',action='store_true');p.add_argument('--plot-only',action='store_true')
    a=p.parse_args()
    if a.plot_only:plot(a.root,json.loads((a.root/'statistics.json').read_text(encoding='utf-8'))['groups'])
    else:run(a.root,render=not a.no_plot)
