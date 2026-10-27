"""Independent Decimal reconciliation and deterministic output check."""
import csv
import argparse
import hashlib
import json
from decimal import Decimal as D
from pathlib import Path


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',default='outputs/pooled_rolling_arbitrage')
    parser.add_argument('--repeat',default='outputs/pooled_rolling_arbitrage_repeat')
    parser.add_argument('--require-no-gap',action='store_true')
    parser.add_argument('--etf',action='store_true')
    parser.add_argument('--cancel-new-openings',action='store_true')
    parser.add_argument('--dividends',action='store_true')
    parser.add_argument('--nav-adjustment',action='store_true')
    parser.add_argument('--etf-scoring',action='store_true')
    parser.add_argument('--original-signal',action='store_true')
    args=parser.parse_args()
    root = Path(args.output)
    repeat = Path(args.repeat)
    def rows(name):
        with (root / name).open(encoding='utf-8-sig', newline='') as stream:
            return list(csv.DictReader(stream))
    cycles = rows('cycles.csv')
    config=json.loads((root/'config.json').read_text(encoding='utf-8'))
    enabled=set(config.get('products',['IF','IH','IC','IM']))
    if args.original_signal:
        assert config['nav_adjustment'] is False and config['nav_version'] is None
        from backtest.factor_data import load_research_data
        from backtest.cash_futures_basis import load_spot,PAIRS
        from datetime import date
        source=config['source_snapshots']
        _,_,raw,_,instruments=load_research_data('data/index_futures',source['daily_version'],source['metadata_version'])
        _,spot_rows=load_spot(version=source['spot_version'])
        prices={(r['instrument'],r['trading_date']):r for r in raw}
        spot={(r['instrument'],r['trading_date']):r for r in spot_rows}
        for c in cycles:
            day=date.fromisoformat(c['signal_day']);f=prices[c['contract'],day]['close']
            s=spot[PAIRS[c['product']][0],day]['close']
            expected=(f-s)/s*D(365)/D((instruments[c['contract']].expiry-day).days)
            assert D(c['entry_rate'])==expected
    for name in ['cycles.csv','selections.csv','candidate_plans.csv','all_trials.csv']:
        assert all(c['product'] in enabled for c in rows(name)),name
    if args.etf_scoring:
        assert config['etf_scoring']
        choices={(c['product'],c['decision_day']):c for c in rows('selections.csv')}
        trial_groups={}
        for r in rows('all_trials.csv'):
            assert D(r['account_return'])==(D(r['arbitrage_pnl'])+D(r['estimated_repo_interest']))/D(r['common_capital'])
            assert D(r['score'])==D(r['account_return']).quantize(D('.00000001'))
            key=r['product'],r['decision_day']
            rank=(D(r['score']),-int(r['cycles']),D(r['entry_threshold']),D(r['exit_threshold']))
            best,count=trial_groups.get(key,(None,0))
            trial_groups[key]=(r if best is None or rank>(D(best['score']),-int(best['cycles']),D(best['entry_threshold']),D(best['exit_threshold'])) else best,count+1)
        for key,(best,count) in trial_groups.items():
            assert count==35
            assert choices[key]['entry_threshold']==best['entry_threshold'] and choices[key]['exit_threshold']==best['exit_threshold']
            assert choices[key]['score']==best['score']
    for c in cycles:
        assert c['exit_day']
        multiplier = D(300 if c['product'] in ('IF', 'IH') else 200)
        expected = (D(c['entry_future']) - D(c['exit_future'])) * multiplier
        if args.etf:
            quantity=int(c['etf_quantity'])
            assert quantity>0 and quantity%100==0
            assert quantity==int(D(c['entry_index'])*multiplier/D(c['entry_etf'])/100)*100
            assert c['exit_day']>c['entry_day']
            expected += (D(c['exit_etf'])-D(c['entry_etf']))*quantity
        else:
            expected += (D(c['exit_index'])-D(c['entry_index']))*multiplier
        if args.dividends:expected+=D(c['dividend_pnl'])
        assert expected == D(c['pnl']), c['id']
        assert c['entry_day'] > c['signal_day']
    allocations = rows('allocation_audit.csv')
    for row in allocations:
        assert D(row['available_before']) >= D(row['required_cash'])
    daily = rows('daily.csv')
    for row in daily:
        if args.require_no_gap:
            assert D(row['funding_gap'])==0 and D(row['cash_deficit'])==0
        arb = sum((D(row['pnl_' + p]) for p in ('IF', 'IH', 'IC', 'IM')), D(0))
        assert D(row['equity']) - D('10000000') == arb + D(row['cumulative_repo'])
        base = max(D(row['cash']) - D(row['repo_interest']) -
                   D(row['margin']) - D(row['locked_T1']), D(0))
        assert base == D(row['repo_base'])
        rate = D('.02') if int(row['day'][:4]) <= 2020 else D('.015')
        assert (base * rate / D(365)).quantize(D('.00000001')) == D(row['repo_interest'])
    names = ['daily.csv', 'cycles.csv', 'allocations.csv', 'selections.csv',
             'future_fills.csv', ('etf_cashflows.csv' if args.etf else 'ideal_spot_cashflows.csv'), 'ledger_entries.csv',
             'summary.csv', 'yearly.csv', 'candidate_plans.csv', 'allocation_audit.csv',
             'all_trials.csv']
    if args.require_no_gap:names.append('funding_actions.csv')
    if args.etf:names+=['etf_fills.csv','funding_actions.csv']
    if args.nav_adjustment:
        names.append('nav_signals.csv')
        signals=rows('nav_signals.csv')
        lookup={(r['product'],r['contract'],r['day']):r for r in signals}
        for r in signals:
            if not r['adjusted_annualized_rate']:continue
            f,s,p,n=map(D,[r['future_close'],r['index_close'],r['etf_close'],r['unit_nav']])
            assert D(r['adjusted_annualized_rate'])==((f-s)/s+(n-p)/n)*D(365)/D(r['days_to_expiry'])
        for c in cycles:
            assert D(c['entry_rate'])==D(lookup[c['product'],c['contract'],c['signal_day']]['adjusted_annualized_rate'])
    if args.dividends:
        names+=['dividend_events.csv','dividend_audit.csv','dividend_claims.csv']
        claims=rows('dividend_claims.csv')
        events=rows('dividend_events.csv')
        expected_claims={(c['id'],e['announcement_date']) for e in events for c in cycles
                         if c['etf']==e['instrument'] and c['entry_day']<=e['record_date']<c['exit_day']}
        assert expected_claims=={(c['group'],c['announcement_date']) for c in claims}
        assert sum((D(e['amount']) for e in rows('ledger_entries.csv') if e['kind']=='etf_dividend'),D(0))==sum(
            (D(c['amount']) for c in claims if c['paid']=='True'),D(0))
        for c in claims:
            assert D(c['amount'])==int(c['quantity'])*D(c['cash_per_unit'])
            assert c['record_date']<=c['ex_date']<=c['payment_date']
            cycle=next(x for x in cycles if x['id']==c['group'])
            assert cycle['etf']==c['instrument'] and cycle['entry_day']<=c['record_date']<cycle['exit_day']
            assert int(cycle['etf_quantity'])==int(c['quantity'])
        for c in cycles:
            assert D(c['dividend_pnl'])==sum((D(x['amount']) for x in claims if x['group']==c['id'] and x['recognized']=='True'),D(0))
    if args.cancel_new_openings:
        names+=['cancelled_openings.csv','proposed_openings.csv']
        proposed=rows('proposed_openings.csv');cancelled=rows('cancelled_openings.csv')
        active={c['id']:c for c in proposed};cancelled_ids=set()
        for c in cancelled:
            candidates=[p for p in active.values() if p['entry_day']==c['day']]
            chosen=min(candidates,key=lambda p:(D(p['expected_gap']),p['product'],p['id']))
            assert chosen['id']==c['group']
            assert D(c['gap_before'])>0 and D(c['expected_gap'])==D(chosen['expected_gap'])
            cancelled_ids.add(c['group']);del active[c['group']]
        assert set(active)=={c['id'] for c in cycles}
        for name in ['future_fills.csv','etf_fills.csv','ledger_entries.csv','etf_cashflows.csv']:
            assert not any(row['group'] in cancelled_ids for row in rows(name))
    hashes = {}
    for name in names:
        digest = hashlib.sha256((root / name).read_bytes()).hexdigest()
        assert digest == hashlib.sha256((repeat / name).read_bytes()).hexdigest(), name
        hashes[name] = digest
    result = dict(closed_cycles_reconciled=len(cycles), allocations_with_sufficient_budget=len(allocations),
                  calendar_days_reconciled=len(daily), identical_outputs=hashes,
                  no_funding_gap_verified=args.require_no_gap)
    (root / 'verification.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in result.items() if k != 'identical_outputs'}))


if __name__ == '__main__':
    main()
