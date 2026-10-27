from dataclasses import replace, asdict
from collections import defaultdict
from datetime import date, datetime, timedelta
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from backtest.models import D, Instruction
from backtest.rbreaker import TZ
from backtest.rbreaker_fee_avoidance import FeeAvoidingRBreaker
from infinitrader_adapter.clock import MinuteClock
from infinitrader_adapter.domain import Contract, ContractRules, CounterAccount, Quote
from infinitrader_adapter.gateway import ConfirmedRules, CheckpointEstimates, PythonGoGateway, prepare_history
from infinitrader_adapter.journal import Journal
from infinitrader_adapter.risk import DualAdmission
from infinitrader_adapter.runtime import Runtime
from infinitrader_adapter.stops import ObservedStops
from backtest.models import Fill
from tests.test_rbreaker import bar

DAY=date(2026,1,5)
CODE='IF2601'


def stamp(hm='09:31',day=DAY):return datetime.fromisoformat(f'{day}T{hm}:00').replace(tzinfo=TZ)


def contract():return Contract(CODE,'IF','CFFEX',D(300),D('.2'),date(2026,1,16))


def rules(day=DAY):
    return ContractRules(CODE,day,D('.2'),D('.25'),D('.000023'),D(0),D('.000023'),D(0),D('.00023'),D(0),
                         'FAKE COUNTER TEST FIXTURE',stamp('08:00',day),day)


def account(now=None,margin=0,frozen=0,commission=0,available=None):
    return CounterAccount(D(10000000),D(margin),D(frozen),D(10000000-margin-frozen if available is None else available),
                          D(margin)/10000000,D(commission),now or stamp())


def strategy():
    c=contract();s=FeeAvoidingRBreaker({CODE:c},{(CODE,DAY):D(40)})
    s.begin_day({'IF':CODE},{CODE:dict(high=100,low=90,close=95)})
    return s


def admit(action='enter_open',effect='open',side=1,margin=0,positions=(),now=None,qty=7,frozen=0):
    s=strategy();g=s.group(CODE,side);at=now or stamp()
    result=DualAdmission().check(s,Instruction(CODE,side,qty,effect,g),action,1,D('120.4'),at,
        account(at,margin=margin,frozen=frozen),positions,{CODE:D(120)},{CODE:rules()})
    return result


def test_original_guard_passes_but_counter_90_rejects_new_open():
    result=admit(margin=9000000)
    assert result.research_allowed==7
    assert result.allowed==0 and result.reason=='counter_current_risk'


def test_unlock_is_also_blocked_at_counter_90():
    s=strategy();g=s.group(CODE,-1)
    positions=[dict(instrument=CODE,side=-1,quantity=7,group=g,trading_date=DAY-timedelta(days=1)),
               dict(instrument=CODE,side=1,quantity=7,group=s.group(CODE,1),trading_date=DAY-timedelta(days=1))]
    result=admit('enter_unlock','close',-1,9000000,positions)
    assert result.research_allowed==7 and result.allowed==0 and result.reason=='counter_current_risk'


def test_counter_projected_margin_and_frozen_money_shrink_quantity():
    result=admit(margin=8970000,frozen=20000)
    # Only ~10,000 CNY remain; one long contract costs 7,224 CNY margin.
    assert result.allowed==1 and result.research_allowed==7


def test_available_cash_blocks_even_when_both_current_ratios_are_low():
    s=strategy();g=s.group(CODE,1)
    r=DualAdmission().check(s,Instruction(CODE,1,7,'open',g),'enter_open',1,D('120.4'),stamp(),
        account(available=1),(),{CODE:D(120)},{CODE:rules()})
    assert r.research_allowed==7 and r.allowed==0


def test_real_close_reducing_risk_is_allowed_above_counter_cap():
    s=strategy();g=s.group(CODE,1)
    positions=[dict(instrument=CODE,side=1,quantity=7,group=g,trading_date=DAY-timedelta(days=1))]
    result=admit('exit_close','close',1,9500000,positions)
    assert result.allowed==7


def test_protective_physical_open_does_not_bypass_counter_cap():
    s=strategy();g=s.group(CODE,1)
    positions=[dict(instrument=CODE,side=1,quantity=7,group=g,trading_date=DAY)]
    result=admit('exit_hedge','open',-1,9000000,positions)
    assert result.allowed==0


def test_stale_snapshot_and_unknown_rules_fail_closed():
    s=strategy();g=s.group(CODE,1);leg=Instruction(CODE,1,1,'open',g)
    gate=DualAdmission()
    r=gate.check(s,leg,'enter_open',1,D(120),stamp()+timedelta(seconds=11),account(),(),{CODE:D(120)},{CODE:rules()})
    assert r.reason=='missing_or_stale_counter_snapshot'
    r=gate.check(s,leg,'enter_open',1,D(120),stamp(),account(),(),{CODE:D(120)},{})
    assert r.reason=='missing_or_stale_contract_rules'


def test_fee_balance_blocks_close_even_if_physical_lot_is_yesterday():
    s=strategy();g=s.group(CODE,1);s.today[DAY,CODE,1]=1
    p=[dict(instrument=CODE,side=1,quantity=5,group=g,trading_date=DAY-timedelta(days=1))]
    r=DualAdmission().check(s,Instruction(CODE,1,1,'close',g),'enter_unlock',-1,D(120),stamp(),account(),p,
                            {CODE:D(120)},{CODE:rules()})
    assert r.allowed==0 and r.reason=='close_today_forbidden'


def test_counter_ratio_scale_is_explicit_not_auto_guessed():
    with pytest.raises(ValueError,match='risk scale'):
        replace(account(margin=100000),risk=D(1))


def test_manual_rule_has_no_default_fee_or_margin(tmp_path):
    path=tmp_path/'rules.json';path.write_text('{"contracts":{}}')
    with pytest.raises(KeyError):ConfirmedRules(path).query(CODE,DAY,stamp())
    row={k:str(v) for k,v in asdict(rules()).items() if k not in ('instrument','trading_day')}
    row['valid_from']=str(DAY);del row['close_today_rate']
    path.write_text(json.dumps(dict(contracts={CODE:row})))
    with pytest.raises(KeyError):ConfirmedRules(path).query(CODE,DAY,stamp())


def test_confirmed_rule_expiry_and_future_confirmation_are_rejected(tmp_path):
    path=tmp_path/'rules.json'
    row={k:str(v) for k,v in asdict(rules()).items() if k not in ('instrument','trading_day')}
    row['valid_from']=str(DAY)
    path.write_text(json.dumps(dict(contracts={CODE:row})))
    assert ConfirmedRules(path).query(CODE,DAY,stamp()).long_margin_rate==D('.2')
    with pytest.raises(ValueError,match='cover'):ConfirmedRules(path).query(CODE,DAY+timedelta(days=1),stamp())
    with pytest.raises(ValueError,match='Future'):ConfirmedRules(path).query(CODE,DAY,stamp('07:00'))


class FakeGateway:
    def __init__(self):self.sent=[];self.cancels=[]
    def is_tradeable(self,code,auction=False):return True
    def send(self,row):self.sent.append(dict(row));return len(self.sent)
    def cancel(self,order_id):self.cancels.append(order_id)


def runtime(tmp_path):
    rt=Runtime({CODE:contract()},tmp_path/'events.jsonl',{'test':'v1'},refresh_delay=0)
    rt.prepare_day(DAY,{'IF':CODE},{CODE:dict(high=100,low=90,close=95)},{CODE:D(40)})
    rt.rules={CODE:rules()};rt.synchronize(account(),{},stamp())
    b=replace(bar(CODE,DAY,'09:31',120),volume=10000)
    rt.frame(b.end,{CODE:b})
    rt.quotes[CODE]=Quote(D(120),D('119.8'),D('120.2'),stamp(),DAY,D(200),D(50))
    return rt,FakeGateway()


def accept_fill(rt,key,q=None,at=None):
    o=rt.orders[key];q=o['quantity'] if q is None else q
    return rt.receive_fill(key,('test','CFFEX',str(DAY),str(len(rt.seen)+1),str(o['broker_id'])),
                           q,D(o['price']),at or stamp(),DAY)


def test_real_fill_only_no_virtual_fill_and_no_duplicate_open(tmp_path):
    rt,g=runtime(tmp_path)
    assert not rt.lots
    rt.pump(g,stamp());rt.pump(g,stamp())
    assert len(g.sent)==1 and not rt.lots
    key=g.sent[0]['client_id'];accept_fill(rt,key,1)
    assert sum(p['quantity'] for p in rt.lots)==1
    assert rt.callback.logical_positions()[0]['quantity']==1
    assert len(rt.callback.logical_fills)==1


def test_fill_deduplication_and_frozen_route_survive_mutable_route(tmp_path):
    rt,g=runtime(tmp_path);rt.pump(g,stamp());key=g.sent[0]['client_id'];o=rt.orders[key]
    rt.callback.route[o['group'],'open']=('exit_hedge',-1)
    trade_key=('test','CFFEX',str(DAY),'001','SYS')
    assert rt.receive_fill(key,trade_key,1,D(o['price']),stamp(),DAY)
    assert not rt.receive_fill(key,trade_key,1,D(o['price']),stamp(),DAY)
    assert rt.callback.routing_fills[-1]['action']=='enter_open'
    assert len(rt.fills)==1


def test_cancel_waits_for_late_reported_fills_and_never_replaces_early(tmp_path):
    rt,g=runtime(tmp_path);rt.pump(g,stamp());key=g.sent[0]['client_id'];o=rt.orders[key]
    rt.targets[o['group']]=(CODE,0)
    rt.pump(g,stamp()+timedelta(seconds=1))
    assert g.cancels==[1] and len(g.sent)==1
    rt.order_status(key,1,True)  # cancellation report overtakes one trade report
    assert rt.live_orders()
    accept_fill(rt,key,1)
    assert not rt.live_orders()
    rt.synchronize(account(stamp(),commission=10),{(CODE,1):1},stamp())
    rt.pump(g,stamp())
    # No stale-target synthetic close: wait for a fresh strategy decision.
    assert len(g.sent)==1


def test_restart_replays_inventory_budget_and_day_fee_balance(tmp_path):
    rt,g=runtime(tmp_path);rt.pump(g,stamp());key=g.sent[0]['client_id'];accept_fill(rt,key)
    before=rt.callback.logical_positions();seed=dict(rt.callback.stops.seeds[CODE])
    restored=Runtime({CODE:contract()},tmp_path/'events.jsonl',{'test':'v1'},refresh_delay=0)
    assert restored.callback.logical_positions()==before
    assert restored.callback.stops.seeds[CODE]==seed
    assert restored.callback.today[DAY,CODE,1]==rt.orders[key]['quantity']
    assert restored.physical_positions()==rt.physical_positions()
    assert not restored.restored_unresolved


def test_restart_with_unknown_outstanding_order_stops_sending(tmp_path):
    rt,g=runtime(tmp_path);rt.pump(g,stamp())
    restored=Runtime({CODE:contract()},tmp_path/'events.jsonl',{'test':'v1'})
    assert restored.restored_unresolved
    restored.pump(g,stamp())
    assert len(g.sent)==1


def test_counter_position_and_fee_watermark_prevent_stale_account_reuse(tmp_path):
    rt,g=runtime(tmp_path);rt.pump(g,stamp());key=g.sent[0]['client_id'];accept_fill(rt,key)
    q=rt.orders[key]['quantity']
    assert not rt.synchronize(account(commission=0),{(CODE,1):q},stamp())
    assert not rt.synchronize(account(commission=10),{},stamp())
    assert rt.synchronize(account(commission=10),{(CODE,1):q},stamp())
    assert not rt.synchronize(account(commission=10,frozen=1),{(CODE,1):q},stamp())


def test_hedge_needs_cash_for_full_order_freeze_not_ungranted_margin_relief():
    s=strategy();g=s.group(CODE,-1)
    positions=[dict(instrument=CODE,side=1,quantity=7,group=s.group(CODE,1),trading_date=DAY)]
    a=account(available=100)
    result=DualAdmission().check(s,Instruction(CODE,-1,7,'open',g),'exit_hedge',1,D('120.4'),stamp(),a,
        positions,{CODE:D(120)},{CODE:rules()})
    assert result.research_allowed==7 and result.allowed==0


def test_account_fee_already_updated_before_trade_callback_does_not_deadlock(tmp_path):
    rt,g=runtime(tmp_path);rt.pump(g,stamp());key=g.sent[0]['client_id']
    rt.account=account(commission=10)
    accept_fill(rt,key)
    q=rt.orders[key]['quantity']
    assert rt.synchronize(account(commission=10),{(CODE,1):q},stamp())


def test_unknown_external_inventory_prevents_new_orders(tmp_path):
    rt,g=runtime(tmp_path)
    assert not rt.synchronize(account(),{('IC2601',1):1},stamp())
    rt.pump(g,stamp());assert not g.sent


def test_torn_journal_or_changed_configuration_is_not_silently_recovered(tmp_path):
    p=tmp_path/'events';j=Journal(p,{'strategy':'a'});j.append({'kind':'test'})
    with pytest.raises(ValueError):Journal(p,{'strategy':'b'})
    with p.open('a') as h:h.write('{')
    with pytest.raises(ValueError):Journal(p,{'strategy':'a'})


def test_completed_minute_clock_does_not_use_future_ticks_or_auction_volume():
    c=MinuteClock()
    c.tick(CODE,stamp('09:30'),DAY,100,1000)
    c.tick(CODE,stamp('09:30')+timedelta(seconds=30),DAY,102,1003)
    assert c.pop(stamp(),{CODE})==[]
    [(at,bars)]=c.pop(stamp()+timedelta(seconds=2),{CODE})
    assert at==stamp() and bars[CODE].volume==3 and bars[CODE].high==102
    with pytest.raises(ValueError,match='already completed'):
        c.tick(CODE,stamp('09:30')+timedelta(seconds=59),DAY,103,1004)


def test_missing_main_minute_does_not_forward_fill():
    c=MinuteClock();c.tick(CODE,stamp('09:30'),DAY,100,1000)
    with pytest.raises(ValueError,match='Missing completed'):
        c.pop(stamp()+timedelta(seconds=2),{CODE,'IH2601'})


def test_history_uses_yesterday_open_interest_and_simple_prior_atr():
    contracts={p+'2602':Contract(p+'2602',p,'CFFEX',D(300),D('.2'),date(2026,2,20)) for p in ('IF','IH','IC','IM')}
    records=[]
    for i in range(30):
        d=DAY-timedelta(days=30-i)
        records.append(dict(datetime=str(stamp(day=d)),high=105,low=95,close=100,volume=100,open_interest=200))
    # Current unfinished day must not set yesterday's levels or ATR.
    records.append(dict(datetime=str(stamp()),high=10000,low=1,close=5000,volume=100000,open_interest=99999))
    class Market:
        def get_kline_data(self,**kwargs):return records
    mains,previous,atr=prepare_history(Market(),contracts,DAY)
    assert len(mains)==4 and all(v==D(10) for v in atr.values())
    assert all(r['high']=='105' for r in previous.values())


def test_gateway_matches_installed_sdk_productclass_dictionary():
    class Api:
        def get_instruments_by_product(self,exchange,product,raw_data):return {'1':[product+'2602']}
        def get_instrument_data(self,exchange,code):
            return SimpleNamespace(expire_date='20260220',product_id=code[:2],exchange='CFFEX',size=300,price_tick=.2)
    g=PythonGoGateway(Api(),'explicit-test-account','unused.json')
    assert len(g.discover(DAY))==4


def test_live_stop_never_uses_extreme_before_actual_entry():
    s=ObservedStops({CODE:contract()},{(CODE,DAY):D(10)})
    ctx=SimpleNamespace(time=stamp(),marks={CODE:D(120)},account=SimpleNamespace(equity=D(10000000)))
    s.limit(CODE,1,ctx,fresh=True)
    fill_at=stamp()+timedelta(seconds=10)
    fills=[Fill(fill_at,DAY,Instruction(CODE,1,1,'open'),D(120),D(1))]
    s.fills_now(fills,fill_at)
    # The complete minute low occurred BEFORE acquisition.
    b=replace(bar(CODE,DAY,'09:32',120),low=D(80),high=D(121))
    ctx=SimpleNamespace(time=b.end,bars={CODE:b},auctions={},fills_since=lambda n:tuple(fills[n:]))
    assert not s.observe(ctx) and not s.active[CODE]['stopped']


def test_live_stop_evidence_waits_until_minute_completed():
    s=ObservedStops({CODE:contract()},{(CODE,DAY):D(10)})
    s.limit(CODE,1,SimpleNamespace(time=stamp(),marks={CODE:D(120)},account=SimpleNamespace(equity=D(10000000))),fresh=True)
    fill_at=stamp()+timedelta(seconds=10)
    fills=[Fill(fill_at,DAY,Instruction(CODE,1,1,'open'),D(120),D(1))]
    s.fills_now(fills,fill_at)
    s.quote(CODE,D(105),stamp()+timedelta(seconds=20),stamp('09:32'))
    assert not s.active[CODE]['stopped']
    b=replace(bar(CODE,DAY,'09:32',120),low=D(105))
    blocked=s.observe(SimpleNamespace(time=b.end,bars={CODE:b},auctions={},fills_since=lambda n:tuple(fills[n:])))
    assert 'IF' in blocked and s.active[CODE]['stopped']
    assert s.events[0]['trigger_time']==str(b.end)


def test_delayed_fill_can_use_already_observed_post_fill_ticks():
    s=ObservedStops({CODE:contract()},{(CODE,DAY):D(10)})
    s.limit(CODE,1,SimpleNamespace(time=stamp(),marks={CODE:D(120)},account=SimpleNamespace(equity=D(10000000))),fresh=True)
    s.quote(CODE,D(105),stamp()+timedelta(seconds=20),stamp('09:32'))
    fill_at=stamp()+timedelta(seconds=10)
    fills=[Fill(fill_at,DAY,Instruction(CODE,1,1,'open'),D(120),D(1))]
    s.fills_now(fills,fill_at)
    b=replace(bar(CODE,DAY,'09:32',120),low=D(105))
    assert 'IF' in s.observe(SimpleNamespace(time=b.end,bars={CODE:b},auctions={},fills_since=lambda n:tuple(fills[n:])))


@pytest.mark.parametrize('mode',['observe','simulation'])
def test_controller_four_products_actual_callback_flow(tmp_path,monkeypatch,mode):
    import sys
    from infinitrader_adapter.controller import Controller
    codes=[p+'2702' for p in ('IF','IH','IC','IM')]
    positions={};sent=[]
    class Api:
        def output(self,*args):pass
        def sub_market_data(self,**kwargs):pass
        def get_instruments_by_product(self,exchange,product,raw_data):return {'1':[product+'2702']}
        def get_instrument_data(self,exchange,code):
            return SimpleNamespace(expire_date='20271217',product_id=code[:2],exchange='CFFEX',size=300,price_tick=.2)
        def get_account_fund_data(self,investor):
            return SimpleNamespace(investor=investor,dynamic_rights=10000000,margin=0,frozen_margin=0,
                available=10000000,risk=0,commission=0,pre_balance=10000000,deposit=0,withdraw=0)
        def make_order_req(self,**kwargs):sent.append(kwargs);return len(sent)
        def cancel_order(self,order_id):return 0
    class Market:
        def get_kline_data(self,**kwargs):
            return [dict(datetime=str(stamp(day=DAY-timedelta(days=30-i))),high=105,low=95,close=100,
                         volume=100,open_interest=200) for i in range(30)]
    fake=SimpleNamespace(infini=SimpleNamespace(get_investor_position=lambda *a,**kw:[]))
    monkeypatch.setitem(sys.modules,'pythongo',fake)
    config=dict(environment='simulation',mode=mode,investor='FAKE_TEST_ACCOUNT',counter_risk_cap='.9',
                rules_file='rules.json',state_directory='state')
    conf=tmp_path/'config.json';conf.write_text(json.dumps(config))
    values={k:str(v) for k,v in asdict(rules()).items() if k not in ('instrument','trading_day')}
    values['valid_from']=str(DAY)
    (tmp_path/'rules.json').write_text(json.dumps(dict(contracts={c:values for c in codes})))
    ctrl=Controller(Api(),conf,Market())
    try:
        for c in codes:ctrl.gateway.status[c]='连续交易'
        for hm,seconds,volume,price in [('09:30',0,1000,120),('09:30',30,1100,121),('09:31',0,1200,121)]:
            at=stamp(hm)+timedelta(seconds=seconds)
            for code in codes:
                tick=SimpleNamespace(exchange='CFFEX',instrument_id=code,trading_day=DAY.strftime('%Y%m%d'),datetime=at,
                    last_price=price,bid_price1=price-.2,ask_price1=price+.2,upper_limit_price=200,lower_limit_price=50,
                    open_price=120,volume=volume)
                ctrl.tick(tick,at)
        ctrl.run_timer(stamp()+timedelta(seconds=2))
        assert len(sent)==(1 if mode=='simulation' else 0)
        assert not ctrl.runtime.lots  # no synthetic execution in either mode
        if sent:
            order=sent[0]
            assert order['offset']=='0' and order['volume']==10
            trade=SimpleNamespace(memo=order['memo'],order_id=1,trade_id='TRADE1',order_sys_id='SYS1',
                instrument_id=order['instrument_id'],exchange='CFFEX',hedgeflag='1',direction='0',offset='0',
                trade_time=DAY.strftime('%Y%m%d')+' 09:31:03',volume=10,price=order['price'])
            ctrl.trade(trade,stamp()+timedelta(seconds=3))
            assert sum(p['quantity'] for p in ctrl.runtime.lots)==10
            assert ctrl.runtime.callback.stops.active[order['instrument_id']]['opened_quantity']==10
    finally:ctrl.close()


def test_user_authorized_checkpoint_fees_are_estimates_not_live_counter_rates(tmp_path):
    r=dict(open_rate='.000023',open_per_unit='0',close_rate='.000023',close_per_unit='0',
           close_today_rate='.00023',close_today_per_unit='0',source_contract='IF2612.CFE')
    p=tmp_path/'snapshot.json'
    p.write_text(json.dumps(dict(contracts={},products={'IF':r},research_margin_rate='.15',fee_version='fixed-test-snapshot',
                                as_of='2026-01-01T00:00:00+08:00')))
    value=CheckpointEstimates(p).query('IF2703',DAY,stamp())
    assert value.long_margin_rate==D('.15') and value.close_today_rate==D('.00023')
    assert value.source.startswith('ESTIMATE:') and 'IF2612.CFE' in value.source
    with pytest.raises(KeyError):CheckpointEstimates(p).query('IC2703',DAY,stamp())


def test_paper_report_does_not_invent_accuracy_without_counter_observations(tmp_path):
    from scripts.summarize_infinitrader_paper import summarize
    r=summarize(tmp_path/'no-state',tmp_path/'report')
    assert r['status']=='no_live_observations' and r['median_actual_to_estimated'] is None


def test_paper_report_uses_only_synchronized_fresh_margin_comparisons(tmp_path):
    from scripts.summarize_infinitrader_paper import summarize
    rows=[dict(trading_day=str(DAY),margin_comparison_usable=True,estimated_margin='100',margin_difference='20',margin_actual_to_estimated='1.2'),
          dict(trading_day=str(DAY),margin_comparison_usable=False,estimated_margin='1',margin_difference='200',margin_actual_to_estimated='201')]
    (tmp_path/'counter_account.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows))
    r=summarize(tmp_path,tmp_path/'report')
    assert r['usable_margin_comparisons']==1 and D(r['median_actual_to_estimated'])==D('1.2')
    assert D(r['mean_absolute_relative_error'])==D('.2')


def test_runtime_same_day_flip_hedges_then_reopens_without_close_today(tmp_path):
    rt,g=runtime(tmp_path);rt.pump(g,stamp());key=g.sent[-1]['client_id'];accept_fill(rt,key)
    total_commission=D(0)
    for hm,price in [('09:32',121),('09:33',94),('09:34',93)]:
        at=stamp(hm)
        quantities=defaultdict(int)
        for p in rt.lots:quantities[p['instrument'],p['side']]+=p['quantity']
        total_commission=sum((f.fee for f in rt.fills),D(0))
        rt.synchronize(account(at,commission=total_commission),dict(quantities),at)
        b=replace(bar(CODE,DAY,hm,price),low=D(price)-1,high=D(price)+1,volume=10000)
        rt.frame(at,{CODE:b})
        rt.quotes[CODE]=Quote(D(price),D(price)-D('.2'),D(price)+D('.2'),at,DAY,D(200),D(50))
        count=len(g.sent);rt.pump(g,at)
        if len(g.sent)>count:accept_fill(rt,g.sent[-1]['client_id'],at=at)
    assert [r['action'] for r in rt.callback.routing_fills]==['enter_open','exit_hedge','enter_open']
    assert all(f.instruction.effect=='open' for f in rt.fills)
    assert sum(p['side']*p['quantity'] for p in rt.lots)<0
