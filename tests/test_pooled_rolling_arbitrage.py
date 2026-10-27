from datetime import date
from types import SimpleNamespace
import pytest
from backtest.pooled_rolling_arbitrage import Portfolio,run
from backtest.models import D,Instrument,Asset,Account


def fixture(initial=2500):
    days=[date(2025,1,i) for i in (3,6,7,8)]
    codes={'IF':'IF2501.CFE','IH':'IH2501.CFE'}
    instruments={code:Instrument(code,Asset.FUTURE,Account.FUTURES,'CFFEX',product,D(10),D('.1'),1,
                                 expiry=days[-1],delivery=days[-1]) for product,code in codes.items()}
    prices={(code,d):dict(open=D(100),close=D(100),settlement_price=D(100)) for code in codes.values() for d in days}
    spot={(index,d):dict(open=D(100),close=D(100)) for index in ('000300.SH','000016.SH') for d in days}
    markets={product:SimpleNamespace(dates=list(days),front=[code]*len(days),eligible_open=lambda code,i:True)
             for product,code in codes.items()}
    p=Portfolio(instruments,prices,spot,markets,initial,'.15')
    return p,days


def plan(product,gap):
    return dict(product=product,contract=f'{product}2501.CFE',signal_day=date(2025,1,2),rate=D('.1'),
                entry_threshold=D('.02'),exit_threshold=D('-.04'),expected_gap=D(gap))


def test_expected_return_floor_uses_exit_gap_inclusive_and_keeps_exits():
    # Raw premium is negative, but expected exit is still lower: gap is positive.
    for gap,accepted in [('.012999',False),('.013',True),('.013001',True)]:
        p,days=fixture()
        p.min_expected_annual_return=D('.013')
        p.markets={'IF':SimpleNamespace(dates=[date(2023,1,3),days[0]],
            front=['IF2501.CFE']*2,rates=[D(0),D('-.02')+D(gap)],
            select=lambda i,w:dict(entry_threshold=D('-.03'),exit_threshold=D('-.02')))}
        p.decide(days[0],None)
        assert bool(p.plans)==accepted
        if accepted:assert p.plans[0]['expected_gap']==D(gap)
        else:assert p.logs[-1]['reason']=='expected_annual_return_below_minimum'
        p.live={'existing':dict(contract='IF2501.CFE',product='IF',exit_threshold=D('.5'),id='existing')}
        p.decide(days[0],None)
        assert p.pending_exits['existing']==days[0]


def test_zero_convergence_floor_ignores_negative_exit_target_and_keeps_exits():
    for rate,accepted in [('.005',False),('.014999',False),('.015',True),('.015001',True),('-.001',False)]:
        p,days=fixture()
        p.min_expected_annual_return=D('.015');p.minimum_return_basis='zero_convergence'
        p.markets={'IF':SimpleNamespace(dates=[date(2023,1,3),days[0]],
            front=['IF2501.CFE']*2,rates=[D(0),D(rate)],
            select=lambda i,w:dict(entry_threshold=D('-.01'),exit_threshold=D('-.04')))}
        p.live={'existing':dict(contract='IF2501.CFE',product='IF',exit_threshold=D('.5'),id='existing')}
        p.decide(days[0],None)
        assert bool(p.plans)==accepted
        assert p.pending_exits['existing']==days[0]
        if accepted:
            assert p.plans[0]['expected_gap']==D(rate)+D('.04')
        else:
            assert p.logs[-1]['amount']==D(rate)


def test_product_floors_use_own_threshold_inclusive_and_do_not_change_exit_target():
    original,days=fixture()
    for rates,expected in [({'IF':'.03','IH':'.03'},{'IH'}),
                           ({'IF':'.04','IH':'.024'},{'IF','IH'}),
                           ({'IF':'.039999','IH':'.023999'},set())]:
        markets={p:SimpleNamespace(dates=[date(2023,1,3),days[0]],front=[f'{p}2501.CFE']*2,
                    rates=[D(0),D(rate)],select=lambda i,w:dict(entry_threshold=D(0),exit_threshold=D('-.04')))
                 for p,rate in rates.items()}
        fund=Portfolio(original.instruments,original.prices,original.spot,markets,
                       minimum_return_basis='zero_convergence',
                       min_expected_annual_return_by_product={'IF':'.04','IH':'.024'})
        fund.decide(days[0],None)
        assert {p['product'] for p in fund.plans}==expected
        assert all(p['exit_threshold']==D('-.04') for p in fund.plans)
        assert all(p['expected_gap']==p['rate']+D('.04') for p in fund.plans)


def test_product_floor_config_requires_full_coverage_and_finite_values():
    fund,_=fixture()
    for floors in ({'IF':'.04'},{'IF':'.04','IH':'.024','IC':'.015'},
                   {'IF':'NaN','IH':'.024'},{'IF':'-.01','IH':'.024'}):
        with pytest.raises(ValueError):
            Portfolio(fund.instruments,fund.prices,fund.spot,fund.markets,
                      min_expected_annual_return_by_product=floors)
    with pytest.raises(ValueError):
        Portfolio(fund.instruments,fund.prices,fund.spot,fund.markets,
                  min_expected_annual_return='.015',min_expected_annual_return_by_product={'IF':'.04','IH':'.024'})


def test_frozen_rolling_source_cannot_be_overwritten(tmp_path):
    source=tmp_path/'source'
    with pytest.raises(ValueError,match='overwrite'):
        run(output=str(source),rolling_source=str(source))
    assert not source.exists()


def test_highest_gap_repeated_entries_affordability_and_no_index_registration():
    p,days=fixture()
    p.plans=[plan('IF','.1'),plan('IH','.2')]
    p.allocate_open(days[0],days[1])
    assert len(p.live)==2 and {c['product'] for c in p.live.values()}=={'IH'}
    assert p.ledger.cash==D(500)
    assert p.available(days[0],'open')==D(200)
    assert all(i.asset!=Asset.INDEX for i in p.ledger.instruments.values())


def test_closed_release_locked_until_next_trade_and_not_counted_as_repo():
    p,days=fixture(1300)
    p.plans=[plan('IF','.1')];p.allocate_open(days[0],days[1]);p.settle(days[0],days[1]);p.record(days[0],True)
    group=next(iter(p.live));p.pending_exits[group]=days[0]
    free_before=p.available(days[1],'open')
    p.close_open(group,days[1],days[2])
    assert p.available(days[1],'open')==free_before
    assert p.locks[0]['release_day']==days[2]
    p.record(days[1],True)
    assert p.daily[-1]['repo_base']==free_before
    cash_before=p.ledger.cash
    p.locks=[r for r in p.locks if r['release_day']>days[2]]
    assert p.available(days[2],'open')==cash_before


def test_repo_deducts_cash_paid_for_spot_only_once_and_margin_never_debits_ledger():
    p,days=fixture(1300)
    p.plans=[plan('IF','.1')];p.allocate_open(days[0],days[1]);p.settle(days[0],days[1]);p.record(days[0],True)
    # Spot purchased1000, entry future tick loss1 settled -> cash299, margin150, repo base149.
    assert p.daily[-1]['repo_base']==D(149)
    assert p.daily[-1]['repo_interest']==(D(149)*D('.015')/365).quantize(D('.00000001'))
    assert p.daily[-1]['ETF_proxy_value']==D(1000)
    assert p.daily[-1]['equity']==D(1299)+p.daily[-1]['repo_interest']


def test_delivery_settles_shared_future_once_and_tracks_independent_cash_assets():
    p,days=fixture()
    p.plans=[plan('IF','.1')];p.allocate_open(days[0],days[1]);p.settle(days[0],days[1]);p.record(days[0],True)
    p.settle(days[-1],None);p.record(days[-1],True)
    assert len(p.completed)==2 and not p.live
    assert len(p.locks)==2
    assert len([e for e in p.ledger.entries if e['kind']=='delivery'])==2
    assert p.daily[-1]['ETF_proxy_value']==0
    assert p.daily[-1]['pnl_IF']==D(-2)


def test_skip_unaffordable_highest_rank_and_use_affordable_lower_candidate():
    p,days=fixture(1500)
    for d in days:p.spot['000300.SH',d]['open']=D(200)
    p.plans=[plan('IF','.2'),plan('IH','.1')]
    p.allocate_open(days[0],days[1])
    assert len(p.live)==1 and next(iter(p.live.values()))['product']=='IH'


def test_friday_close_release_stays_locked_through_weekend_until_monday():
    p,days=fixture(1300)
    thursday=date(2025,1,2)
    for code in p.instruments:p.prices[code,thursday]=dict(open=D(100),close=D(100),settlement_price=D(100))
    for index in ('000300.SH','000016.SH'):p.spot[index,thursday]=dict(open=D(100),close=D(100))
    for m in p.markets.values():m.dates.insert(0,thursday);m.front.insert(0,m.front[0])
    p.plans=[plan('IF','.1')];p.allocate_open(thursday,days[0]);p.settle(thursday,days[0]);p.record(thursday,True)
    group=next(iter(p.live));p.pending_exits[group]=thursday
    p.close_open(group,days[0],days[1]);p.record(days[0],True)
    locked=p.locks[0]['amount']
    p.record(date(2025,1,4),False);p.record(date(2025,1,5),False)
    assert all(r['locked_T1']==locked for r in p.daily[-3:])
    assert p.daily[-1]['repo_base']==p.daily[-1]['cash']-p.daily[-1]['repo_interest']-locked
    p.locks=[r for r in p.locks if r['release_day']>days[1]]
    assert p.available(days[1],'open')==p.ledger.cash


def test_emergency_closes_nearest_frozen_target_and_reconciles_without_duplicate_settlement():
    p,days=fixture(2500)
    p.plans=[plan('IF','.2')];p.allocate_open(days[0],days[1])
    groups=list(p.live)
    p.live[groups[0]]['exit_threshold']=D('1000')
    p.live[groups[1]]['exit_threshold']=D('0')
    p.prices['IF2501.CFE',days[0]].update(close=D(140),settlement_price=D(140))
    p.resolve_funding(days[0]);p.settle(days[0],days[1]);p.record(days[0],True)
    assert p.completed[0]['id']==groups[1]
    assert len(p.completed)==1 and p.daily[-1]['funding_gap']==0
    assert p.daily[-1]['pnl_IF']==D(-803)
    assert not p.locks
    assert p.funding_actions[0]['gap_before']==D(722)


def test_emergency_repeats_when_one_lot_is_insufficient():
    p,days=fixture(2500)
    p.plans=[plan('IF','.2')];p.allocate_open(days[0],days[1])
    p.prices['IF2501.CFE',days[0]].update(close=D(180),settlement_price=D(180))
    p.resolve_funding(days[0]);p.settle(days[0],days[1]);p.record(days[0],True)
    assert len(p.funding_actions)==2 and not p.live
    assert p.daily[-1]['funding_gap']==0
    assert p.daily[-1]['pnl_IF']==D(-1604)


def test_missing_emergency_close_does_not_assume_fill_and_reports_gap():
    p,days=fixture(1300)
    p.plans=[plan('IF','.2')];p.allocate_open(days[0],days[1])
    p.prices['IF2501.CFE',days[0]].update(close=None,settlement_price=D(140))
    p.resolve_funding(days[0]);p.settle(days[0],days[1]);p.record(days[0],True)
    assert not p.funding_actions and p.daily[-1]['funding_gap']>0


def etf_fixture():
    p,days=fixture(1300)
    for day in days:
        p.spot['510300.SH',day]=dict(open=D(3),close=D('3.2'),ohlcv_complete=True)
        p.spot['510050.SH',day]=dict(open=D(3),close=D(3),ohlcv_complete=True)
    return Portfolio(p.instruments,p.prices,p.spot,p.markets,1300,'.15',True,True),days


def test_etf_hundred_share_rounding_actual_cash_and_ledger_pnl():
    p,days=etf_fixture()
    p.plans=[plan('IF','.2')];p.allocate_open(days[0],days[1]);p.settle(days[0],days[1]);p.record(days[0],True)
    c=next(iter(p.live.values()))
    assert c['etf_quantity']==300  # floor(100 *10 /3 /100)*100
    assert p.ledger.quantity('510300.SH',1,c['id'])==300
    assert p.daily[-1]['ETF_proxy_value']==D(960)
    assert p.daily[-1]['pnl_IF']==D(59)  # ETF60, future-1
    assert p.daily[-1]['repo_base']==D(249)
    assert p.allocation_audit[0]['stock_purchase_cash']==D(900)
    p.spot['510300.SH',days[1]]['open']=D('3.4')
    p.pending_exits[c['id']]=days[0];p.close_open(c['id'],days[1],days[2])
    p.record(days[1],True)
    assert p.completed[0]['etf_quantity']==300
    assert p.daily[-1]['pnl_IF']==D(118)  # ETF120, future-2
    assert p.completed[0]['exit_etf']==D('3.4')


def test_etf_missing_complete_quote_blocks_whole_entry_without_future_leg():
    p,days=etf_fixture();p.spot['510300.SH',days[0]]['ohlcv_complete']=False
    p.plans=[plan('IF','.2')];p.allocate_open(days[0],days[1])
    assert not p.live and not p.ledger.fills


def test_etf_t1_prevents_same_day_emergency_sale_then_next_day_can_exit():
    p,days=etf_fixture();p.plans=[plan('IF','.2')];p.allocate_open(days[0],days[1])
    p.prices['IF2501.CFE',days[0]].update(close=D(160),settlement_price=D(160))
    p.resolve_funding(days[0]);assert not p.funding_actions
    p.settle(days[0],days[1]);p.record(days[0],True)
    assert p.daily[-1]['funding_gap']>0
    p.prices['IF2501.CFE',days[1]].update(close=D(160),settlement_price=D(160))
    p.resolve_funding(days[1]);p.settle(days[1],days[2]);p.record(days[1],True)
    assert len(p.funding_actions)==1 and not p.live and p.daily[-1]['funding_gap']==0


def test_cancel_today_lowest_original_gap_replays_only_retained_effective_trades():
    p,days=etf_fixture()
    p=Portfolio(p.instruments,p.prices,p.spot,p.markets,2500,'.15',True,True,True)
    p.plans=[plan('IF','.2')];p.allocate_open(days[0],days[1])
    groups=list(p.live);p.live[groups[0]]['expected_gap']=D('.01')
    p.prices['IF2501.CFE',days[0]].update(close=D(160),settlement_price=D(160))
    p.resolve_funding(days[0])
    assert p.cancelled_openings[0]['group']==groups[0]
    assert list(p.live)==[groups[1]] and not p.funding_actions
    assert p.projected_available(days[0])==D(759)
    assert len(p.ledger.fills)==2 and all(f.instruction.group==groups[1] for f in p.ledger.fills)
    assert all(e['group']!=groups[0] for e in p.ledger.entries)
    assert len(p.spot_fills)==1 and len(p.allocation_audit)==1
    p.settle(days[0],days[1]);p.record(days[0],True)
    assert p.daily[-1]['pnl_IF']==D(-541)  # retained ETF60 minus futures601
    assert p.daily[-1]['funding_gap']==0


def test_cancel_all_new_plans_when_one_is_not_enough_without_fake_closing():
    p,days=etf_fixture()
    p=Portfolio(p.instruments,p.prices,p.spot,p.markets,2500,'.15',True,True,True)
    p.plans=[plan('IF','.2')];p.allocate_open(days[0],days[1])
    p.prices['IF2501.CFE',days[0]].update(close=D(300),settlement_price=D(300))
    p.resolve_funding(days[0]);p.settle(days[0],days[1]);p.record(days[0],True)
    assert len(p.cancelled_openings)==2 and not p.live and not p.completed
    assert not p.ledger.fills and not p.spot_fills and not p.locks
    assert p.daily[-1]['pnl_IF']==0 and p.daily[-1]['repo_base']==D(2500)


def test_new_plan_cancelled_before_old_position_close_even_if_old_near_target():
    p,days=etf_fixture()
    p=Portfolio(p.instruments,p.prices,p.spot,p.markets,3000,'.15',True,True,True)
    p.plans=[plan('IF','.2')];p.allocate_open(days[0],days[1]);p.settle(days[0],days[1]);p.record(days[0],True)
    old=set(p.live)
    p.prices['IF2501.CFE',days[1]]['open']=D(50)
    p.plans=[plan('IF','.2')];p.allocate_open(days[1],days[2])
    assert len(p.live)==3
    p.resolve_funding(days[1]);p.settle(days[1],days[2]);p.record(days[1],True)
    assert len(p.cancelled_openings)==1 and set(p.live)==old
    assert not p.funding_actions


def test_without_new_plans_old_position_uses_original_same_close_exit():
    p,days=etf_fixture();p.cancel_new_openings=True
    p.plans=[plan('IF','.2')];p.allocate_open(days[0],days[1]);p.settle(days[0],days[1]);p.record(days[0],True)
    p.prices['IF2501.CFE',days[1]].update(close=D(160),settlement_price=D(160))
    p.plans=[];p.allocate_open(days[1],days[2]);p.resolve_funding(days[1])
    assert not p.cancelled_openings and len(p.funding_actions)==1


def dividend_event(days):
    return dict(instrument='510300.SH',announcement_date=date(2025,1,1),
                record_date=days[0],exchange_ex_date=days[1],payment_date=days[2],cash_per_unit=D('.1'))


def test_dividend_record_ex_payment_and_claim_survives_etf_sale():
    from backtest.research_dividends import DividendBook
    p,days=etf_fixture();p.dividends=DividendBook([dividend_event(days)])
    p.plans=[plan('IF','.2')];p.allocate_open(days[0],days[1])
    p.dividends.capture(days[0],p.ledger);p.dividends.capture(days[0],p.ledger)
    assert len(p.dividends.claims)==1 and not p.dividends.receivables()
    p.settle(days[0],days[1]);p.record(days[0],True)
    before=p.ledger.cash;p.dividends.process(days[1],p.ledger)
    assert p.ledger.cash==before and p.dividends.receivables()=={'IF_1':D(30)}
    group=next(iter(p.live));p.pending_exits[group]=days[0];p.close_open(group,days[1],days[2])
    p.record(days[1],True)
    assert not p.live and p.daily[-1]['dividend_receivable']==D(30)
    assert p.daily[-1]['pnl_IF']==D(28)
    before=p.ledger.cash;p.dividends.process(days[2],p.ledger);p.dividends.process(days[2],p.ledger)
    assert p.ledger.cash==before+D(30) and not p.dividends.receivables()
    p.record(days[2],True)
    assert p.daily[-1]['pnl_IF']==D(28) and p.dividends.total==D(30)


def test_dividend_no_entitlement_for_position_sold_on_record_date():
    from backtest.research_dividends import DividendBook
    p,days=etf_fixture();event=dividend_event(days);event['record_date']=days[1]
    p.dividends=DividendBook([event]);p.plans=[plan('IF','.2')]
    p.allocate_open(days[0],days[1]);p.settle(days[0],days[1]);p.record(days[0],True)
    group=next(iter(p.live));p.pending_exits[group]=days[0];p.close_open(group,days[1],days[2])
    p.dividends.capture(days[1],p.ledger);p.dividends.process(days[2],p.ledger)
    assert not p.dividends.claims and p.dividends.total==0


def test_cancelled_opening_never_receives_dividend_entitlement():
    from backtest.research_dividends import DividendBook
    p,days=etf_fixture();p.cancel_new_openings=True;p.dividends=DividendBook([dividend_event(days)])
    p.plans=[plan('IF','.2')];p.allocate_open(days[0],days[1])
    p.prices['IF2501.CFE',days[0]].update(close=D(160),settlement_price=D(160))
    p.resolve_funding(days[0]);p.dividends.capture(days[0],p.ledger)
    p.dividends.process(days[2],p.ledger)
    assert not p.dividends.claims and p.dividends.total==0
