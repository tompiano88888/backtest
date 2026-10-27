"""Read-only causal classification of the sealed R-Breaker checkpoint trades."""
from collections import defaultdict,deque
from datetime import datetime,timedelta,time
from decimal import Decimal as D
import argparse,csv,hashlib,json,math,statistics,sys
from pathlib import Path
from types import SimpleNamespace

PROJECT=Path(__file__).resolve().parents[1]
SEALED=PROJECT/'outputs/rbreaker_checkpoint_20261020_fee_safe_atr20_historical'
# Import the user-selected implementation, regardless of later working changes.
sys.path.insert(0,str(SEALED/'source'))
from backtest.factor_data import load_research_data
from backtest.models import Bar,Auction,Fill,Instruction
from backtest.rbreaker import DAILY,METADATA
from backtest.rbreaker_portfolio import PortfolioRBreaker
from backtest.rbreaker_stops import TradeStops,prior_atr
from backtest.rbreaker_fee_avoidance import InventoryAwareSignals
from scripts.verify_rbreaker_exposure import RawPrices


def read(path):
    with Path(path).open(encoding='utf-8-sig',newline='') as h:return list(csv.DictReader(h))


def write(path,rows):
    if not rows:return
    with Path(path).open('w',encoding='utf-8-sig',newline='') as h:
        w=csv.DictWriter(h,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)


def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
    return h.hexdigest()


class TaggedStops(TradeStops):
    def limit(self,code,quantity,ctx,fresh=False):
        answer=super().limit(code,quantity,ctx,fresh)
        if fresh and quantity>0 and code not in self.active and code in self.seeds and self.seeds[code]['decision_time']==str(ctx.time):
            product=self.instruments[code].product
            mode=self.owner.strategies[product].mode.get(code)
            assert mode in ('breakout','reversal'),(code,ctx.time,mode)
            self.seeds[code]['entry_type']=mode
        return answer


def equivalent(actual,saved):
    if isinstance(actual,bool):return str(actual)==saved
    if isinstance(actual,(D,int)):return D(str(actual))==D(saved)
    return str(actual)==saved


def replay(output,source=SEALED/'results',end=None):
    """Replay original decisions from saved fills/account values; never execute."""
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    source=Path(source)
    manifest=json.loads((SEALED/'manifest.json').read_text(encoding='utf-8'))
    assert all(sha(SEALED/r['path'])==r['sha256'] for r in manifest['files'])
    summary=json.loads((source/'summary.json').read_text());config=summary['config']
    saved_trades=read(source/'trades.csv')
    by_entry={(t['instrument'],int(t['side']),t['entry_time']):int(t['id']) for t in saved_trades}
    assert len(by_entry)==len(saved_trades)
    _,_,raw,_,instruments=load_research_data('data/index_futures',DAILY,METADATA)
    indexed={(r['instrument'],str(r['trading_date'])):r for r in raw}
    mapping=defaultdict(dict)
    for r in read(source/'main_mapping.csv'):mapping[r['day']][r['product']]=r
    policy=TaggedStops(instruments,prior_atr(raw,config['stop']['atr_window']),D(config['stop']['risk_fraction']),D(config['stop']['atr_multiple']))
    logic=PortfolioRBreaker(instruments,D(config['margin_rate']),policy);policy.owner=logic
    owner=SimpleNamespace(instruments=instruments,physical_positions=())
    logic.strategies={p:InventoryAwareSignals(owner,p) for p in logic.strategies}
    prices=RawPrices();physical=defaultdict(int);logical_lots=defaultdict(deque)
    history=[];directives={};exit_decisions=defaultdict(list)
    per_trade=defaultdict(lambda:defaultdict(int));trade_active={};trade_qty=defaultdict(int)
    fills=read(source/'fills.csv');routes=read(source/'routing_fills.csv')
    assert len(fills)==len(routes)
    visible=[]
    for f in fills:
        t=datetime.fromisoformat(f['time'])
        visible.append(t if f['execution_phase']=='auction' else t+timedelta(minutes=1))
    fi=0;last_day=None;last_marks={};frames=0
    with (source/'funding_observations.csv').open(encoding='utf-8-sig',newline='') as h:
        for observation in csv.DictReader(h):
            if observation['stage']!='minute_close':continue
            now=datetime.fromisoformat(observation['time']);day=observation['time'][:10]
            if end and day>end:break
            if day!=last_day:
                mains={p:r['instrument'] for p,r in mapping[day].items()}
                previous={r['instrument']:indexed[r['instrument'],r['information_day']] for r in mapping[day].values()}
                logic.begin_day(mains,previous);last_day=day
            while fi<len(fills) and visible[fi]<=now:
                f,r=fills[fi],routes[fi]
                assert int(r['fill_index'])==fi
                code=f['instrument'];side=int(f['side']);q=int(f['quantity'])
                physical[code,side]+=q*(1 if f['effect']=='open' else -1)
                assert physical[code,side]>=0
                if r['action']!='cleanup':
                    logical_side=int(r['logical_side']);effect=r['logical_effect']
                    event=Fill(datetime.fromisoformat(f['time']),datetime.fromisoformat(f['trading_date']).date(),
                               Instruction(code,logical_side,q,effect),D(f['price']),D(f['fee']))
                    history.append(event);lots=logical_lots[code,logical_side]
                    if effect=='open':
                        if code not in trade_active:trade_active[code]=by_entry[code,logical_side,f['time']]
                        lots.append([q,D(f['price']),event.trading_date]);trade_qty[code]+=q
                    else:
                        left=q
                        while left:
                            used=min(left,lots[0][0]);left-=used;lots[0][0]-=used
                            if not lots[0][0]:lots.popleft()
                        trade_qty[code]-=q
                    tid=trade_active[code];per_trade[tid][r['action']+'_quantity']+=q
                    per_trade[tid][r['action']+'_fills']+=1
                    if not trade_qty[code]:del trade_active[code]
                fi+=1
            owner.physical_positions=tuple(dict(instrument=c,side=s,quantity=n) for (c,s),n in physical.items() if n)
            positions=tuple(dict(instrument=c,side=s,quantity=q,entry=p,basis=p,trading_date=d,group='standalone')
                            for (c,s),lots in logical_lots.items() for q,p,d in lots)
            codes=set(mains.values())|{p['instrument'] for p in owner.physical_positions}|{p['instrument'] for p in positions}
            bars={};auctions={};marks=dict(last_marks)
            if now.time()!=time(9,25):
                for code in codes:
                    r=prices.get(code,now)
                    if now.time()==time(9,30):
                        assert D(r['open'])==D(indexed[code,day]['open'])
                        auctions[code]=Auction(code,now,now.date(),D(r['open']),int(D(r['volume'])))
                        marks[code]=D(r['open'])
                    else:
                        bars[code]=Bar(code,now-timedelta(minutes=1),now,now.date(),*(D(r[k]) for k in ('open','high','low','close')),int(D(r['volume'])))
                        marks[code]=D(r['close'])
            directives={}
            ctx=SimpleNamespace(time=now,bars=bars,auctions=auctions,marks=marks,positions=positions,
                                account=SimpleNamespace(equity=D(observation['equity'])),
                                fills_since=lambda index:tuple(history[index:]),target=lambda c,q:directives.__setitem__(c,q))
            logic(ctx)
            helper_events={}
            for helper in logic.strategies.values():
                for event in helper.audit:
                    if event['reason']!='overnight_decision':helper_events[event['instrument']]=event['reason']
                helper.audit.clear()
            for code,target in directives.items():
                t=policy.active.get(code)
                if target or t is None:continue
                if t['stopped']:reason='stop_'+t['trigger_reason']
                elif code!=mains.get(t['product']):reason='main_roll'
                elif helper_events.get(code)=='reversal' or code in logic.reverse_after_close:reason='reversal'
                elif helper_events.get(code)=='end_of_day_non_breakout':reason='end_of_day'
                elif exit_decisions[t['id']]:continue
                else:raise AssertionError(('unclassified exit',code,now,t,directives,helper_events))
                past=exit_decisions[t['id']]
                if not past or past[-1]['reason']!=reason:past.append(dict(time=str(now),reason=reason))
            logic.risk.clear();frames+=1
            if frames%60000==0:print(f'CLASSIFY {day} frames={frames} trades={policy.counter}',flush=True)
            if now.time()==time(15):
                for code in codes:marks[code]=D(indexed[code,day]['settlement_price'])
            last_marks=marks
    assert end is None,'Full checkpoint required for final analysis'
    assert fi==len(fills) and frames==summary['frames']
    final_records=policy.records(last_marks)
    assert len(final_records)==len(saved_trades)
    for actual,saved in zip(final_records,saved_trades):
        for name,value in saved.items():
            assert equivalent(actual[name],value),('trade replay mismatch',saved['id'],name,actual[name],value)
    saved_stops=read(source/'stop_events.csv')
    assert len(saved_stops)==len(policy.events)
    for actual,saved in zip(policy.events,saved_stops):
        assert all(equivalent(actual[k],v) for k,v in saved.items())
    saved_allocations=read(source/'allocation_audit.csv')
    assert len(saved_allocations)==len(logic.audit)
    for actual,saved in zip(logic.audit,saved_allocations):
        assert all(equivalent(actual[k],v) for k,v in saved.items()),('allocation mismatch',saved,actual)
    days=sorted(mapping);day_index={day:i for i,day in enumerate(days)}
    enriched=[]
    for trade in final_records:
        t={**trade};tid=t['id'];decisions=exit_decisions[tid];route=per_trade[tid]
        closed=t['remaining_quantity']==0
        if closed:assert decisions,('missing exit decision',tid)
        first=decisions[0]['reason'] if decisions else ''
        cause='stop_'+t['trigger_reason'] if t['stopped'] else first if closed else 'open_at_end'
        assert t['entry_type'] in ('breakout','reversal')
        end_time=t['exit_time'] or summary['config']['end']+' 15:00:00+08:00'
        holding_sessions=day_index[end_time[:10]]-day_index[t['entry_time'][:10]]+1
        t.update(exit_type=cause,first_exit_type=first,last_exit_type=decisions[-1]['reason'] if decisions else '',
                 exit_decision_time=decisions[0]['time'] if decisions else '',closed=closed,
                 holding_sessions=holding_sessions,holding_calendar_hours=(datetime.fromisoformat(end_time)-datetime.fromisoformat(t['entry_time'])).total_seconds()/3600,
                 net_r=t['net_pnl']/t['budget'],entry_year=t['entry_time'][:4],direction='long' if t['side']>0 else 'short')
        for action in ('enter_open','enter_unlock','exit_close','exit_hedge'):
            t[action+'_quantity']=route[action+'_quantity'];t[action+'_fills']=route[action+'_fills']
        assert t['enter_open_quantity']+t['enter_unlock_quantity']==t['opened_quantity']
        assert t['exit_close_quantity']+t['exit_hedge_quantity']==t['opened_quantity']-t['remaining_quantity']
        t['entry_execution']='mixed' if t['enter_open_quantity'] and t['enter_unlock_quantity'] else 'unlock' if t['enter_unlock_quantity'] else 'new_open'
        t['exit_execution']='mixed' if t['exit_close_quantity'] and t['exit_hedge_quantity'] else 'lock' if t['exit_hedge_quantity'] else 'direct_close' if t['exit_close_quantity'] else 'open_at_end'
        enriched.append(t)
    write(output/'trade_labels.csv',enriched)
    write(output/'exit_decisions.csv',[dict(trade_id=tid,**event) for tid,events in exit_decisions.items() for event in events])
    result=dict(status='passed',source=str(source),sealed_files_verified=len(manifest['files']),frames_replayed=frames,
                actual_fills_consumed=len(fills),all_trade_fields_matched=len(saved_trades),stop_events_matched=len(saved_stops),
                allocation_decisions_matched=len(saved_allocations),logical_trade_pnl=str(sum((t['net_pnl'] for t in enriched),D(0))),
                inventory_cleanup_adjustment=summary['inventory_cleanup_adjustment'],account_pnl=summary['net_pnl'])
    assert D(result['logical_trade_pnl'])+D(result['inventory_cleanup_adjustment'])==D(result['account_pnl'])
    (output/'classification_verification.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result,indent=2),flush=True)
    return enriched,summary


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,default=Path('outputs/rbreaker_signal_diagnostics_20261022'))
    args=p.parse_args();replay(args.output)
