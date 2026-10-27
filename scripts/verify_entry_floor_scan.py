"""Independent read-only audit and full-scan reproducibility comparison."""
import argparse
import csv
import hashlib
import json
from decimal import Decimal as D,InvalidOperation
from pathlib import Path


def read(path):
    with path.open(encoding='utf-8-sig',newline='') as stream:return list(csv.DictReader(stream))


def numeric_equal(left,right):
    if left==right:return True
    try:return D(left)==D(right)
    except InvalidOperation:return False


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',default='outputs/arbitrage_entry_floor_scan_0_4')
    parser.add_argument('--repeat',default='outputs/arbitrage_entry_floor_scan_0_4_repeat')
    args=parser.parse_args();root=Path(args.output);repeat=Path(args.repeat)
    trials=read(root/'all_trials.csv');other=read(repeat/'all_trials.csv')
    assert trials==other
    assert len(trials)==164
    assert {(r['product'],D(r['floor'])) for r in trials}=={
        (p,D(i)/1000) for p in ('IF','IH','IC','IM') for i in range(41)}
    config=json.loads((root/'config.json').read_text(encoding='utf-8'))
    nav_lookup={}
    if config.get('nav_adjustment'):
        for r in read(root/'nav_signals.csv'):
            if not r['adjusted_annualized_rate']:continue
            f,s,e,n=map(D,(r['future_close'],r['index_close'],r['etf_close'],r['unit_nav']))
            days_to_expiry=int(r['days_to_expiry'])
            assert days_to_expiry>0
            value=((f-s)/s+(n-e)/n)*D(365)/days_to_expiry
            assert value==D(r['adjusted_annualized_rate'])
            nav_lookup[r['product'],r['contract'],r['day']]=value
    compared=closed=days=0;format_only=0;core_equal=0;gap_trials=[]
    for row in trials:
        p=row['product'];floor=D(row['floor']);path=Path(p)/f'{int(floor*10000):03d}bp'
        for source in (root,repeat):
            config=json.loads((source/path/'config.json').read_text(encoding='utf-8'))
            assert config['minimum_return_basis']=='zero_convergence'
            assert D(config['min_expected_annual_return'])==floor
        for file in (root/path).glob('*.csv'):
            a=file.read_bytes();b=(repeat/path/file.name).read_bytes()
            compared+=1
            if a==b:core_equal+=1;continue
            left=read(file);right=read(repeat/path/file.name)
            assert len(left)==len(right),(path,file.name)
            for x,y in zip(left,right):
                assert x.keys()==y.keys()
                for key in x:
                    assert numeric_equal(x[key],y[key]),(path,file.name,key,x[key],y[key])
                    format_only+=x[key]!=y[key]
        if D(row['max_funding_gap'])>0 or D(row['max_cash_deficit'])>0:
            gap_trials.append({k:row[k] for k in ('product','floor','max_funding_gap','max_cash_deficit','budget_gap_calendar_days')})
        cycles=read(root/path/'cycles.csv')
        for c in cycles:
            assert D(c['entry_rate'])>=floor
            if nav_lookup:assert D(c['entry_rate'])==nav_lookup[p,c['contract'],c['signal_day']]
            assert D(c['entry_rate'])>D(c['entry_threshold'])
            if not c['exit_day']:continue
            mult=D(300 if p in ('IF','IH') else 200)
            quantity=int(c['etf_quantity']);assert quantity>0 and quantity%100==0
            assert quantity==int(D(c['entry_index'])*mult/D(c['entry_etf'])/100)*100
            expected=(D(c['entry_future'])-D(c['exit_future']))*mult
            expected+=(D(c['exit_etf'])-D(c['entry_etf']))*quantity+D(c['dividend_pnl'])
            assert expected==D(c['pnl']),c['id']
            assert c['signal_day']<c['entry_day']<c['exit_day']
            closed+=1
        daily=read(root/path/'daily.csv')
        assert max(D(d['funding_gap']) for d in daily)==D(row['max_funding_gap'])
        assert max(D(d['cash_deficit']) for d in daily)==D(row['max_cash_deficit'])
        for day in daily:
            pnl=sum((D(day['pnl_'+product]) for product in ('IF','IH','IC','IM')),D(0))
            assert D(day['equity'])-D('10000000')==pnl+D(day['cumulative_repo'])
            base=max(D(day['cash'])-D(day['repo_interest'])-D(day['margin'])-D(day['locked_T1']),D(0))
            assert base==D(day['repo_base'])
            rate=D('.02') if day['day'][:4]=='2020' else D('.015')
            assert (base*rate/365).quantize(D('.00000001'))==D(day['repo_interest'])
            days+=1
    for best in read(root/'best_by_product.csv'):
        group=[r for r in trials if r['product']==best['product']]
        expected=max(group,key=lambda r:(D(r['total_return']),-D(r['floor'])))
        assert best['floor']==expected['floor']
    result=dict(trials=164,identical_aggregate_trials=True,compared_csv_files=compared,
        byte_identical_csv_files=core_equal,numeric_format_only_cells=format_only,
        closed_cycles_independently_reconciled=closed,calendar_days_reconciled=days,
        funding_gap_trials=gap_trials,best_trials_no_funding_gap=all(D(r['max_funding_gap'])==0 and D(r['max_cash_deficit'])==0 for r in read(root/'best_by_product.csv')),
        minimum_rate_verified=True,
        independent_nav_rates_checked=len(nav_lookup),
        aggregate_trials_sha256=hashlib.sha256((root/'all_trials.csv').read_bytes()).hexdigest(),
        note='Older full group reporting and single-product reporting may stringify equal Decimal zero/trailing zeros differently; all values must be exactly equal.')
    (root/'verification.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result))


if __name__=='__main__':main()
