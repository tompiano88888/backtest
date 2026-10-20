from datetime import timedelta, datetime, timezone
from dataclasses import replace
import pytest
from backtest.engine import ExecutionConfig
from backtest.fees import FeeRule, FeeService, HistoricalFees, publish_fee_snapshot
from backtest.models import D, Instruction, Fill
from backtest.runner import execution_from_record
from backtest.storage import Store
from test_engine import engine, frame, T


def setup_engine(matching='today_first', today='.00023'):
    e = engine()
    e.config = ExecutionConfig(participation=1, fee_rate='.000023',
                              close_today_fee_rate=today, fee_matching=matching)
    e.ledger.apply(Fill(T-timedelta(days=1), T.date()-timedelta(days=1),
                        Instruction('A',1,10,group='old'),D(100),D(0)))
    return e


def step(e,n,leg,**kwargs):
    e.submit([leg],T+timedelta(minutes=n-1))
    e.step(frame(n,**kwargs))


def test_today_first_cross_group_and_exhaustion():
    e=setup_engine()
    step(e,1,Instruction('A',1,3,group='new'))
    step(e,2,Instruction('A',1,5,'close','old'))
    d=e.fees.details[-1]
    assert (d['close_today_quantity'],d['normal_quantity'])==(3,2)
    assert d['total']==D(1000)*(3*D('.00023')+2*D('.000023'))
    assert e.ledger.quantity('A',1,'new')==3  # actual FIFO/group unchanged
    step(e,3,Instruction('A',1,1,'close','old'))
    assert e.fees.details[-1]['close_today_quantity']==0
    assert sum(d['total'] for d in e.fees.details)==sum(f.fee for f in e.ledger.fills)


@pytest.mark.parametrize('rate',['0','.0000115','.00023'])
def test_fifo_preserved_and_today_rates(rate):
    e=setup_engine('fifo',rate)
    step(e,1,Instruction('A',1,3,group='old'))
    step(e,2,Instruction('A',1,5,'close','old'))
    assert e.fees.details[-1]['close_today_quantity']==0
    step(e,3,Instruction('A',1,8,'close','old'))
    assert e.fees.details[-1]['close_today_quantity']==3
    assert e.fees.details[-1]['total']==D(1000)*(5*D('.000023')+3*D(rate))


def test_close_before_open_no_retroactive_and_lock_open_is_ordinary():
    e=setup_engine()
    step(e,1,Instruction('A',1,2,'close','old'))
    original=e.ledger.fills[-1].fee
    step(e,2,Instruction('A',1,2,group='new'))
    step(e,3,Instruction('A',-1,2,group='lock'))
    assert e.fees.details[-1]['close_today_quantity']==0
    assert e.ledger.fills[0].fee==0 and e.ledger.fills[1].fee==original


def test_partial_and_guard_quote_does_not_consume_balance():
    e=setup_engine()
    step(e,1,Instruction('A',1,3,group='new'))
    def guard(ctx,fill):
        assert ctx.fill_for_quantity(1).fee==ctx.fill_for_quantity(1).fee==D('.23')
        return 1
    e.execution_guard=guard
    step(e,2,Instruction('A',1,5,'close','old'),volumes={'A':2})
    assert e.fees.details[-1]['close_today_quantity']==1
    e.execution_guard=None
    e.step(frame(3))
    assert (e.fees.details[-1]['close_today_quantity'],e.fees.details[-1]['normal_quantity'])==(2,2)


def test_balance_day_contract_and_side_isolation():
    e=setup_engine()
    step(e,1,Instruction('A',1,2))
    cfg=e.config; inst=e.ledger.instruments['A']
    q=lambda code,side,day:e.fees.quote(Instruction(code,side,1,'close'),1,D(100),day,
                                      e.ledger.instruments[code],cfg,())['close_today_quantity']
    assert q('A',1,T.date())==1
    assert q('B',1,T.date())==q('A',-1,T.date())==q('A',1,T.date()+timedelta(days=1))==0


def test_json_commission_separate_from_free_exchange_fee():
    cfg=execution_from_record(dict(fee_rate='.000023',close_today_fee_rate='0',
                                  commission_rate='.00001',commission_per_unit='2'))
    e=setup_engine(); inst=e.ledger.instruments['A']
    d=FeeService().quote(Instruction('A',1,3,'close'),3,D(100),T.date(),inst,cfg,((3,True),))
    assert d['exchange_fee']==0 and d['commission']==D('6.03')
    assert cfg.close_today_fee_rate==D(0)


@pytest.mark.parametrize('field',['fee_rate','fee_per_unit','close_today_fee_rate',
                                  'close_today_fee_per_unit','commission_rate','commission_per_unit'])
@pytest.mark.parametrize('value',['-1','NaN','Infinity'])
def test_invalid_fee_settings(field,value):
    with pytest.raises(ValueError):ExecutionConfig(**{field:value})


def payload(rate):
    return dict(open_rate=rate,open_per_unit='0',close_rate=rate,close_per_unit='0',
                close_today_rate='0.001',close_today_per_unit='0',matching='today_first',source='test-import')


def test_historical_version_switch_missing_and_conflict(tmp_path):
    start=datetime(2026,9,20,tzinfo=timezone.utc)
    change=start+timedelta(days=2)
    with Store(tmp_path) as store:
        store.put_metadata('fee_rule','A',start,start,payload('.0001'),change)
        store.put_metadata('fee_rule','A',change,start,payload('.0002'))
        version=publish_fee_snapshot(store,'fees-test')
    provider=HistoricalFees(tmp_path,version,change)
    assert provider.resolve('A',T.date()).open_rate==D('.0001')
    assert provider.resolve('A',T.date()+timedelta(days=2)).open_rate==D('.0002')
    with pytest.raises(KeyError):provider.resolve('B',T.date())
    with Store(tmp_path) as store:
        store.put_metadata('fee_rule','A',start,start,payload('.0009'),change)
    # Pinned cutoff excludes subsequent metadata.
    assert HistoricalFees(tmp_path,version,change).resolve('A',T.date()).open_rate==D('.0001')
    with Store(tmp_path) as store:
        conflict_version=publish_fee_snapshot(store,'fees-conflict')
    with pytest.raises(ValueError,match='Conflicting'):
        HistoricalFees(tmp_path,conflict_version,change)
    e=setup_engine();e.config=replace(e.config,fee_mode='historical');e.fees.provider=provider
    step(e,1,Instruction('A',1,1))
    assert e.ledger.fills[-1].fee==D('.1')
    assert e.fees.details[-1]['rule_revision']!='fixed'


def test_historical_never_falls_back():
    e=setup_engine(); e.config=replace(e.config,fee_mode='historical')
    with pytest.raises(ValueError,match='provider'):step(e,1,Instruction('A',1,1))


@pytest.mark.parametrize('callback',[False,True])
def test_research_entrypoints_and_full_instrument_override(callback):
    from backtest.cli import demo_config
    from backtest.runner import run_scenario,result
    cfg=demo_config()
    cfg['execution'].update(commission_rate='.00001',commission_per_unit='1',slippage_ticks=1)
    cfg['execution_by_instrument']={'B':dict(participation='1',commission_per_unit='7')}
    if callback:
        cfg['instructions']=[]
        def on_bar(ctx):
            if not ctx.positions:
                ctx.submit([Instruction('A',1,1),Instruction('B',1,1)])
        e=run_scenario(cfg,on_bar)
    else:
        e=run_scenario(cfg)
    report=result(e,cfg)
    assert e.fees.details
    for fill,d in zip(e.ledger.fills,e.fees.details):
        assert fill.fee==d['total']==d['exchange_fee']+d['commission']
        if fill.instruction.instrument=='B':
            assert d['commission']==fill.instruction.quantity*7
        else:
            assert d['commission']==fill.instruction.quantity*(1+fill.price*10*D('.00001'))
    assert report['fee_details']==e.fees.details
    assert sum(d['total'] for d in e.fees.details)==-sum(D(r['amount']) for r in e.ledger.entries if r['kind']=='fee')


def test_auction_uses_same_today_first_and_commission():
    from backtest.models import Auction
    from backtest.engine import Frame
    e=setup_engine()
    step(e,1,Instruction('A',1,3,group='new'))
    e.config=replace(e.config,commission_per_unit=D(2),slippage_ticks=5)
    at=T+timedelta(minutes=2)
    e.submit([Instruction('A',1,5,'close','old')],at-timedelta(seconds=1))
    auction=Auction('A',at,T.date(),D(100),100)
    e.step(Frame(at,{},frozenset({'A'}),auctions={'A':auction}))
    assert e.ledger.fills[-1].price==100
    assert e.fees.details[-1]['close_today_quantity']==3
    assert e.fees.details[-1]['commission']==10


def test_saved_guard_quote_is_frozen():
    e=setup_engine();step(e,1,Instruction('A',1,3,group='new'))
    contexts=[]
    def guard(ctx,fill):
        contexts.append(ctx)
        return fill.instruction.quantity
    e.execution_guard=guard
    step(e,2,Instruction('A',1,5,'close','old'))
    assert contexts[0].fill_for_quantity(3).fee==D('.69')


def test_historical_overlap_and_as_of(tmp_path):
    start=T-timedelta(days=3)
    with Store(tmp_path) as store:
        store.put_metadata('fee_rule','A',start,start,payload('.0001'))
        store.put_metadata('fee_rule','A',start+timedelta(days=1),T,payload('.0002'))
        version=publish_fee_snapshot(store,'test-overlap')
    provider=HistoricalFees(tmp_path,version,T-timedelta(seconds=1))
    assert provider.resolve('A',T.date()).open_rate==D('.0001')
    with pytest.raises(ValueError,match='Overlapping'):HistoricalFees(tmp_path,version,T)
    with pytest.raises(KeyError):HistoricalFees(tmp_path,'missing',T)
    with pytest.raises(ValueError):HistoricalFees(tmp_path,version,T.replace(tzinfo=None))


def test_snapshot_rejects_incomplete_rules(tmp_path):
    with Store(tmp_path) as store:
        with pytest.raises(ValueError,match='No fee'):publish_fee_snapshot(store,'test')
        store.put_metadata('fee_rule','A',T,T,{'open_rate':'0'})
        with pytest.raises(ValueError,match='Incomplete'):publish_fee_snapshot(store,'test')


def test_historical_runner_loads_provider_and_records_provenance(tmp_path):
    from backtest.cli import demo_config
    from backtest.runner import run_scenario,result
    with Store(tmp_path) as store:
        for code in ('A','B'):
            store.put_metadata('fee_rule',code,T-timedelta(days=1),T-timedelta(days=1),payload('.0001'))
        version=publish_fee_snapshot(store,'runner-fees')
    cfg=demo_config()
    cfg['execution']=dict(fee_mode='historical',participation='1',commission_per_unit='2')
    cfg['historical_fees']=dict(root=str(tmp_path),version=version,as_of=T.isoformat())
    e=run_scenario(cfg)
    report=result(e,cfg)
    assert report['fee_provenance']['version']==version
    assert all(d['mode']=='historical' and d['rule_revision']!='fixed' for d in report['fee_details'])
    assert all(d['commission']==2*f.instruction.quantity for f,d in zip(e.ledger.fills,e.fees.details))
    with pytest.raises(KeyError):e.fees.provider.resolve('A',T.date()-timedelta(days=3))
