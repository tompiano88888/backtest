"""Yearly diagnostics from verified logical trades; never reruns the strategy."""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path

from scripts.report_rbreaker_trade_types import D, LABELS, group, read, stats, table, write

PRODUCTS = ['IF', 'IH', 'IC', 'IM']
TYPES = ['breakout', 'reversal']
EXITS = ['end_of_day', 'reversal', 'main_roll', 'stop_atr_price',
         'stop_trade_risk_budget', 'open_at_end']
EXIT_COLORS = ['#547DA3', '#E6AB50', '#55A58B', '#D9535C', '#963C7D', '#C5CBD1']
DEFAULT_SOURCE = Path('outputs/rbreaker_signal_diagnostics_20261022')
DEFAULT_OUT = Path('outputs/rbreaker_yearly_multidim_20261022')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def analyze(source, out):
    out.mkdir(parents=True, exist_ok=True)
    source_file = source / 'trade_labels.csv'
    original_sha = sha(source_file)
    trades = read(source_file)
    verification = json.loads((source / 'classification_verification.json').read_text())
    assert verification['status'] == 'passed'
    assert len({t['id'] for t in trades}) == len(trades)
    assert all(t['entry_year'] == t['entry_time'][:4] for t in trades)
    overall = stats(trades)
    dimensions = {
        'year': ['entry_year'],
        'year_entry': ['entry_year', 'entry_type'],
        'year_product': ['entry_year', 'product'],
        'year_product_entry': ['entry_year', 'product', 'entry_type'],
        'year_entry_exit': ['entry_year', 'entry_type', 'exit_type'],
        'year_product_entry_exit': ['entry_year', 'product', 'entry_type', 'exit_type'],
        'year_product_entry_direction': ['entry_year', 'product', 'entry_type', 'direction'],
    }
    groups = {name: group(trades, keys) for name, keys in dimensions.items()}
    for name, values in groups.items():
        assert sum(v['trades'] for v in values) == len(trades)
        assert sum(v['closed'] for v in values) == overall['closed']
        assert sum(v['stops'] for v in values) == overall['stops']
        assert sum((v['net_pnl'] for v in values), D(0)) == overall['net_pnl']
        assert sum((v['closed_net_pnl'] for v in values), D(0)) == overall['closed_net_pnl']
        write(out / (name + '.csv'), values)

    detail = defaultdict(list)
    for r in groups['year_product_entry_exit']:
        detail[(r['entry_year'], r['product'], r['entry_type'])].append(r)
    cohorts = []
    for r in groups['year_product_entry']:
        key = (r['entry_year'], r['product'], r['entry_type'])
        exits = detail[key]
        assert sum(e['trades'] for e in exits) == r['trades']
        assert sum((e['net_pnl'] for e in exits), D(0)) == r['net_pnl']
        closed_r_sum = sum((e['average_r'] * e['closed'] for e in exits if e['closed']), D(0))
        assert abs(closed_r_sum / r['closed'] - r['average_r']) < D('1e-24')
        cohorts.append(dict(r, exits=exits))

    yearly_checks = []
    for year in sorted({t['entry_year'] for t in trades}):
        selected = [t for t in trades if t['entry_year'] == year]
        closed = [t for t in selected if t['closed'] == 'True']
        counts = Counter(t['exit_type'] for t in selected)
        assert counts['stop_atr_price'] + counts['stop_trade_risk_budget'] == sum(t['stopped'] == 'True' for t in selected)
        yearly_checks.append(dict(year=year, trades=len(selected), closed=len(closed),
            net_pnl=sum((D(t['net_pnl']) for t in selected), D(0)),
            average_r=sum((D(t['net_pnl']) / D(t['budget']) for t in closed), D(0)) / len(closed),
            stops=sum(t['stopped'] == 'True' for t in selected)))
    assert overall['net_pnl'] == D(verification['logical_trade_pnl'])
    assert overall['net_pnl'] + D(verification['inventory_cleanup_adjustment']) == D(verification['account_pnl'])
    assert original_sha == sha(source_file)
    assert not any(c['product'] == 'IM' and int(c['entry_year']) < 2022 for c in cohorts)
    checks = dict(status='passed', source=str(source_file.resolve()), source_sha256=original_sha,
        trades=overall['trades'], closed=overall['closed'], open=overall['open'], stops=overall['stops'],
        logical_trade_pnl=overall['net_pnl'], inventory_cleanup_adjustment=verification['inventory_cleanup_adjustment'],
        account_pnl=verification['account_pnl'], grouped_tables_reconciled=len(groups),
        cohorts=len(cohorts), exit_cohorts=len(groups['year_product_entry_exit']),
        independent_year_checks=yearly_checks, strategy_rerun=False)
    data = dict(overall=overall, groups=groups, cohorts=cohorts, checks=checks,
        definition=dict(year='Whole lifecycle PnL assigned to actual entry year; not calendar-year account PnL',
            r='Closed-trade net PnL / frozen initial risk budget; arithmetic mean, not portfolio return',
            stop_rate='Stopped direction trades / all observed direction trades, including open trades',
            pie='Exit-reason trade count / cohort trade count; stop overrides first exit intent',
            area='Outer-circle area proportional to all direction-trade count',
            labels='Logical net PnL in CNY 10,000 and observed stop rate',
            open_trades='Four open trades included in net PnL and counts; excluded from mean R and win rate',
            sample='2020-01-02 to 2026-10-08; IM begins in 2022; 2026 is incomplete'))
    (out / 'statistics.json').write_text(json.dumps(data, default=str, ensure_ascii=False, indent=2), encoding='utf-8')
    (out / 'analysis_checks.json').write_text(json.dumps(checks, default=str, ensure_ascii=False, indent=2), encoding='utf-8')
    report = '# R-Breaker年度、品种、入场与退出多维分析\n\n'
    report += '当前历史手续费对锁节点的只读归因，不改变策略、对锁路由或节点。以实际入场年归属整笔PnL，跨年交易不拆分，不等同账户年度收益。2026仅截至10月8日；IM从2022年才有样本。\n\n'
    report += '## 图的阅读方式\n\n'
    report += '横轴为入场年，纵轴为品种与入场类型。气泡总面积正比于逻辑方向交易笔数；外圈颜色表示已结束交易的平均R，红色为负、蓝色为正。内圆扇区按交易笔数展示退出原因构成；下方数字依次为方向净PnL（万元）和观察止损率。交互版悬停或点击气泡可看该组所有退出原因的笔数及PnL。退出原因包括日终、反转、换主力、ATR止损及期末持仓，不能与真实平仓/对锁执行方式混淆。\n\n'
    report += 'R=单笔净PnL/该笔入场时冻结风险预算，平均R仅使用已结束交易。这是每笔风险预算的表现，不是组合收益率。颜色全图共用线性、以零为中心的对称刻度；没有按行或按年重新缩放。\n\n'
    report += '## 年份与入场类型\n\n'
    columns = [('entry_year','入场年','label'),('entry_type','入场','label'),('trades','笔数','number'),
        ('net_pnl','PnL万元','wan'),('win_rate','完整胜率','pct'),('stops','止损笔数','number'),
        ('stop_rate_observed','观察止损率','pct'),('average_r','平均R','r')]
    report += table(groups['year_entry'], columns) + '\n'
    report += '## 年度观察\n\n'
    report += '- 2024年突破净PnL为1202.52万元，占全部年份突破累计净PnL的71.94%；平均R为0.232，说明高收益同时体现在风险预算标准化后的单笔表现。当年四品种突破均盈利、反转均亏损，反转合计−93.72万元。\n'
    report += '- 2025年突破−284.14万元、平均−0.029R，反转+195.29万元、平均0.065R，方向合计−88.85万元。反转在该年抵消部分突破亏损，两类入场存在阶段性互补。\n'
    report += '- 2025年突破止损率3.69%，低于2024年的6.80%，但净表现更差。该年突破反转退出61笔合计−688.56万元，日终退出200笔+679.03万元，ATR止损10笔−274.61万元。只压低ATR触发次数不能直接解决失败突破的问题。\n'
    report += '- 分品种看，2025年IF/IH/IC突破分别−190.46/−101.31/−45.23万元，IM突破仍+52.86万元；反转盈利主要来自IM+111.48万元、IC+57.96万元、IH+41.07万元，IF反转−15.22万元。\n'
    report += '- IH反转在7个年度观察窗中6个盈利，IM突破5个年度观察窗均盈利；但IM始于2022年，2026又是未完整年度，不能把这些窗口当成等长独立样本。IF突破与反转均只有4/7个窗口盈利，IC反转为3/7。\n'
    latest = {r['product']:r for r in cohorts if r['entry_year']=='2026' and r['entry_type']=='breakout'}
    report += '- 2026截至10月8日，突破平均仅0.011R、止损率6.88%，反转平均0.044R、止损率2.00%。'
    report += '突破分品种为'+'、'.join(f'{p} {D(latest[p]["net_pnl"])/10000:+.2f}万元' for p in PRODUCTS)+'，整体平均值掩盖了品种差异。\n'
    report += '- 下一步优先检验失败突破与反转退出的关系，并将IF/IH和IC/IM分开观察。若设计过滤条件，应只使用当时可知信息，保留当前对锁与历史费用口径，用单独回测检验，不从这张事后分组图直接推导策略收益。\n\n'
    report += '## 年份、品种与入场类型\n\n' + table(groups['year_product_entry'], [columns[0], ('product','品种','label')] + columns[1:]) + '\n'
    report += '## 年份、入场与退出原因\n\n'
    report += table(groups['year_entry_exit'], [('entry_year','入场年','label'),('entry_type','入场','label'),
        ('exit_type','退出原因','label'),('trades','笔数','number'),('net_pnl','PnL万元','wan'),('average_r','平均R','r')]) + '\n'
    report += '## 核对与解释限制\n\n'
    report += f'共{overall["trades"]}笔逻辑方向交易，{overall["closed"]}笔完整结束、{overall["open"]}笔仍持有，{overall["stops"]}笔触发止损。{len(cohorts)}个年度×品种×入场组与{len(groups["year_product_entry_exit"])}个年度×品种×入场×退出组，全部加总与原逐笔记录一致。\n\n'
    report += f'方向净PnL {overall["net_pnl"]:,.5f}元，加库存清理调整{D(verification["inventory_cleanup_adjustment"]):,.5f}元，等于账户PnL {D(verification["account_pnl"]):,.5f}元。库存调整未强行摊入信号分组。\n\n'
    report += '止损率按止损笔数/全部已观察交易笔数计算；完整样本分母与Wilson区间另存CSV。小样本零止损不表示真实概率为零，交易之间可能相关。金额受当时权益、手数和持有天数影响，平均R只部分改善可比性。退出类型为事后归因，不能在入场时知道，不能据此直接推导删去某类交易后的收益。\n\n'
    report += '原逐笔文件SHA256未变。此轮仅做分组加总与制图，未重新回测，不将旧361项框架测试冒称本轮新运行。\n'
    (out / 'report.md').write_text(report, encoding='utf-8')
    print(json.dumps(dict(checks=checks, year_entry=groups['year_entry'], year_product=groups['year_product']), default=str, ensure_ascii=False))


def render(out):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib import colors, patches, path
    import numpy as np

    data = json.loads((out / 'statistics.json').read_text(encoding='utf-8'))
    cohorts = data['cohorts']
    plt.rcParams.update({'font.sans-serif': ['Microsoft YaHei', 'SimHei'], 'axes.unicode_minus': False,
                         'svg.fonttype': 'none', 'font.size': 10})
    fig = plt.figure(figsize=(18, 12), facecolor='#FAFBFD')
    ax = fig.add_axes([.095, .17, .71, .69], facecolor='#FAFBFD')
    rows = [(p,t) for p in PRODUCTS for t in TYPES]
    years = sorted({c['entry_year'] for c in cohorts})
    max_n = max(c['trades'] for c in cohorts)
    max_r = math.ceil(max(abs(float(c['average_r'])) for c in cohorts) * 20) / 20
    norm = colors.TwoSlopeNorm(vmin=-max_r, vcenter=0, vmax=max_r)
    cmap = plt.get_cmap('RdBu')
    size_scale = 2050 / max_n
    ax.set(xlim=(-.65, len(years)-.35), ylim=(7.75, -.75))
    ax.set_xticks(range(len(years)), [y + ('*' if y=='2026' else '') for y in years], fontsize=12)
    ax.xaxis.tick_top()
    ax.tick_params(axis='both', length=0, pad=12)
    ax.set_yticks(range(len(rows)), [p+'  '+LABELS[t] for p,t in rows], fontsize=12)
    for spine in ax.spines.values(): spine.set_visible(False)
    for x in range(len(years)):
        ax.axvline(x, color='#DCE2EA', lw=.6, zorder=0)
    for y in range(8):
        if y%2==0: ax.axhspan(y-.47, y+1.47, color='#EEF2F7', zorder=-1)
    for p in range(1,4): ax.axhline(p*2-.5, color='#D1D8E2', lw=1)

    for index, c in enumerate(cohorts):
        x = years.index(c['entry_year']); y = rows.index((c['product'],c['entry_type']))
        area = c['trades'] * size_scale
        artist = ax.scatter(x,y,s=area,c=[cmap(norm(float(c['average_r'])))], edgecolors='white', linewidths=.8, zorder=3)
        artist.set_gid('cohort-'+str(index))
        begin = math.pi/2
        values = {e['exit_type']:e['trades'] for e in c['exits']}
        for reason,color in zip(EXITS,EXIT_COLORS):
            if not values.get(reason): continue
            end = begin + 2*math.pi*values[reason]/c['trades']
            angles = np.linspace(begin,end,max(3,math.ceil((end-begin)*30)))
            vertices = [(0,0)] + list(zip(np.cos(angles),np.sin(angles))) + [(0,0)]
            # MOVETO-only anchors fix every sector's marker bounds. Without them,
            # scatter normalizes narrow sectors separately and distorts pie areas.
            codes = [path.Path.MOVETO] + [path.Path.LINETO]*(len(vertices)-2) + [path.Path.CLOSEPOLY]
            wedge = path.Path(vertices+[(1,1),(-1,-1)], codes+[path.Path.MOVETO,path.Path.MOVETO])
            part = ax.scatter(x,y,s=area*.58,marker=wedge,c=color,edgecolors='white',linewidths=.35,zorder=4)
            part.set_gid('cohort-'+str(index)+'-'+reason)
            begin=end
        label = f'{float(c["net_pnl"])/10000:+.1f}万 / {c["stop_rate_observed"]:.1%}'
        text = ax.annotate(label,(x,y),xytext=(0,-31),textcoords='offset points',ha='center',va='top',
            fontsize=8.3,color='#26364A',zorder=5)
        text.set_gid('cohort-'+str(index)+'-label')
    for t in TYPES:
        y=rows.index(('IM',t))
        for x in [0,1]:ax.text(x,y,'尚无IM样本',ha='center',va='center',fontsize=9,color='#8A95A4')

    fig.text(.045,.954,'R-Breaker：年份、品种、入场与退出',fontsize=23,color='#172A40',weight='bold')
    fig.text(.045,.916,'横轴：入场年    纵轴：品种与入场类型    面积：交易笔数    外圈：平均R    内圆：退出原因占比',fontsize=12,color='#4C5F74')
    fig.text(.045,.891,'气泡下方：方向净PnL（万元）/ 观察止损率',fontsize=11,color='#4C5F74')

    legend_ax = fig.add_axes([.835,.25,.15,.60]);legend_ax.axis('off')
    legend_ax.text(0,1,'退出原因（内圆）',fontsize=12,weight='bold',transform=legend_ax.transAxes)
    existing = {e['exit_type'] for c in cohorts for e in c['exits']}
    present = [(k,v) for k,v in zip(EXITS,EXIT_COLORS) if k in existing]
    for i,(reason,color) in enumerate(present):
        legend_ax.add_patch(patches.Rectangle((0,.91-i*.065),.07,.024,facecolor=color,transform=legend_ax.transAxes))
        legend_ax.text(.12,.922-i*.065,LABELS[reason],va='center',fontsize=10,transform=legend_ax.transAxes)
    legend_ax.text(0,.53,'交易笔数（总面积）',fontsize=12,weight='bold',transform=legend_ax.transAxes)
    for i,n in enumerate([10,30,60]):
        legend_ax.scatter(.15,.45-i*.095,s=n*size_scale,c='#A9B6C6',edgecolors='white',transform=legend_ax.transAxes)
        legend_ax.text(.4,.45-i*.095,str(n)+'笔',va='center',transform=legend_ax.transAxes)
    cax = fig.add_axes([.848,.24,.12,.018])
    cb = fig.colorbar(plt.cm.ScalarMappable(norm=norm,cmap=cmap),cax=cax,orientation='horizontal')
    cb.set_ticks([-max_r,0,max_r]);cb.set_ticklabels([f'−{max_r:.2f}', '0', f'+{max_r:.2f}'])
    cb.ax.tick_params(labelsize=9)
    fig.text(.848,.278,'外圈：平均净R',fontsize=12,weight='bold')
    fig.text(.848,.185,'负值为红，正值为蓝\n全图共用同一刻度',fontsize=10,color='#4C5F74',linespacing=1.6)
    fig.text(.045,.095,'平均R = 已结束交易的净PnL / 各笔冻结风险预算，再逐笔平均；并非组合收益率。',fontsize=10,color='#4C5F74')
    fig.text(.045,.065,'整笔PnL归入实际入场年，含4笔期末持仓估值；不等同账户按日结算的年度PnL。库存清理调整另列。',fontsize=10,color='#4C5F74')
    fig.text(.045,.035,'* 2026截至10月8日。止损率 = 触发笔数 / 全部观察笔数。当前历史手续费与对锁节点保持不变。',fontsize=10,color='#4C5F74')
    fig.savefig(out/'yearly_multidim.png',dpi=180,facecolor=fig.get_facecolor())
    fig.savefig(out/'yearly_multidim.svg',facecolor=fig.get_facecolor())
    plt.close(fig)
    svg = (out/'yearly_multidim.svg').read_text(encoding='utf-8')
    svg = svg[svg.index('<svg'):]
    payload = json.dumps(cohorts,ensure_ascii=False).replace('</','<\\/')
    labels = json.dumps(LABELS,ensure_ascii=False)
    html = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>R-Breaker年度多维归因</title>
<style>body{margin:0;background:#fafbfd;font:15px 'Microsoft YaHei',sans-serif;color:#172a40}header{padding:18px 3%;border-bottom:1px solid #dce2ea}main{max-width:1600px;margin:auto}svg{width:100%;height:auto}#detail{padding:22px 3%;background:white;border-top:1px solid #dce2ea;min-height:170px}table{border-collapse:collapse;width:100%;max-width:1000px}td,th{text-align:right;padding:9px 18px;border-bottom:1px solid #edf0f5}td:first-child,th:first-child{text-align:left}.hint{color:#53657a;font-size:13px}[id^="cohort-"]{cursor:pointer}#tip{position:fixed;display:none;background:#172a40;color:white;padding:10px 14px;border-radius:6px;pointer-events:none;z-index:20;max-width:340px;font-size:13px;box-shadow:0 3px 20px #0003}button{background:white;border:1px solid #ccd5e1;border-radius:5px;padding:6px 12px;color:#172a40;cursor:pointer}</style>
<header><b>悬停查看概要，点击气泡查看该年度分组的退出明细。</b> <button onclick="window.print()">打印 / 另存PDF</button><p class="hint">本页离线可用。52个年度×品种×入场组，退出原因属于事后统计。</p></header>
<main>__SVG__</main><section id="detail">点击任一气泡，此处显示净PnL、完整胜率、平均R、止损率和退出原因明细。</section><div id="tip"></div>
<script>const DATA=__DATA__, LABELS=__LABELS__;
const fmt=(v,d=2)=>Number(v).toLocaleString('zh-CN',{minimumFractionDigits:d,maximumFractionDigits:d});
const pct=v=>v==null?'—':fmt(v*100)+'%';
const title=c=>c.entry_year+'年 '+c.product+' '+LABELS[c.entry_type];
const brief=c=>title(c)+'<br>净PnL '+fmt(Number(c.net_pnl)/10000)+'万元，'+c.trades+'笔<br>平均R '+fmt(c.average_r,3)+'，胜率 '+pct(c.win_rate)+'<br>止损 '+c.stops+'/'+c.trades+' = '+pct(c.stop_rate_observed);
function detail(c){document.getElementById('detail').innerHTML='<h2>'+title(c)+'</h2><p>'+brief(c).split('<br>').slice(1).join('；')+'</p><p class="hint">完整 '+c.closed+'笔，期末持仓 '+c.open+'笔。完整样本止损率 '+pct(c.stop_rate_closed)+'；Wilson 95%描述区间 '+pct(c.stop_rate_wilson_low)+'～'+pct(c.stop_rate_wilson_high)+'。</p><table><thead><tr><th>退出原因</th><th>交易笔数</th><th>占该组笔数</th><th>净PnL（万元）</th><th>平均R（完整样本）</th></tr></thead><tbody>'+c.exits.map(e=>'<tr><td>'+LABELS[e.exit_type]+'</td><td>'+e.trades+'</td><td>'+pct(e.trades/c.trades)+'</td><td>'+fmt(Number(e.net_pnl)/10000)+'</td><td>'+(e.average_r==null?'—':fmt(e.average_r,3))+'</td></tr>').join('')+'</tbody></table><p class="hint">跨年交易整笔归入入场年。期末估值纳入PnL，完整胜率及平均R排除未结束交易。止损区间未校正交易相关性。</p>';}
document.querySelectorAll('[id^="cohort-"]').forEach(el=>{const c=DATA[Number(el.id.split('-')[1])];el.addEventListener('mouseenter',()=>{const tip=document.getElementById('tip');tip.innerHTML=brief(c);tip.style.display='block'});el.addEventListener('mousemove',e=>{const tip=document.getElementById('tip');tip.style.left=Math.min(e.clientX+14,innerWidth-355)+'px';tip.style.top=Math.max(8,Math.min(e.clientY+14,innerHeight-140))+'px'});el.addEventListener('mouseleave',()=>document.getElementById('tip').style.display='none');el.addEventListener('click',()=>detail(c));el.setAttribute('tabindex','0');el.setAttribute('role','button');el.setAttribute('aria-label',title(c));el.addEventListener('keydown',e=>{if(e.key==='Enter'){detail(c);document.getElementById('detail').scrollIntoView({behavior:'smooth'})}})});
</script></html>'''
    html = html.replace('__SVG__',svg).replace('__DATA__',payload).replace('__LABELS__',labels)
    (out/'yearly_multidim.html').write_text(html,encoding='utf-8')
    print(json.dumps(dict(plot='saved',cohorts=len(cohorts),color_limit=max_r,maximum_cohort_count=max_n)))


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,default=DEFAULT_SOURCE)
    parser.add_argument('--out',type=Path,default=DEFAULT_OUT)
    parser.add_argument('--plot-only',action='store_true')
    parser.add_argument('--analyze-only',action='store_true')
    args=parser.parse_args()
    if not args.plot_only: analyze(args.source,args.out)
    if not args.analyze_only: render(args.out)
