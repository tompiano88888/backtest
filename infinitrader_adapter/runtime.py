"""Real-fill driven target execution. No Engine.step and no cash ledger."""
from collections import defaultdict
from dataclasses import asdict
from datetime import date, datetime, timedelta
import hashlib
from pathlib import Path
from threading import RLock
from types import SimpleNamespace
from uuid import uuid4

from backtest.models import D, Bar, Auction, Fill, Instruction
from backtest.rbreaker_fee_avoidance import FeeAvoidingRBreaker
from .domain import Contract, CounterAccount
from .journal import Journal
from .risk import DualAdmission
from .stops import ObservedStops


def contract_dict(c):
    return {k:str(v) if k in ('multiplier','tick','expiry') else v for k,v in asdict(c).items()}


def contract_load(c):
    return Contract(**{**c,'multiplier':D(c['multiplier']),'tick':D(c['tick']),'expiry':date.fromisoformat(c['expiry'])})


def market_load(row,auction=False):
    r=dict(row)
    for k in ('time','start','end'):
        if k in r:r[k]=datetime.fromisoformat(r[k])
    r['trading_date']=date.fromisoformat(r['trading_date'])
    for k in ('open','high','low','close','price','settlement_price','upper_limit','lower_limit','turnover','open_interest','previous_settlement_price'):
        if k in r and r[k] is not None:r[k]=D(r[k])
    return Auction(**r) if auction else Bar(**r)


class Runtime:
    def __init__(self,contracts,journal_path,identity,counter_cap=D('.9'),participation=D('.1'),refresh_delay=6):
        self.lock=RLock();self.contracts=dict(contracts)
        self.journal=Journal(journal_path,identity)
        for event in self.journal.events:
            for c in event.get('contracts',[]):self.contracts[c['id']]=contract_load(c)
        self.callback=FeeAvoidingRBreaker(self.contracts,{})
        stops=ObservedStops(self.contracts,{})
        self.callback.stops=stops;self.callback.logic.stop_policy=stops
        self.callback.active=stops.active;self.callback.events=stops.events
        self.gate=DualAdmission(counter_cap)
        self.participation=D(participation);self.refresh_delay=timedelta(seconds=refresh_delay)
        if not 0 < self.participation <= 1:raise ValueError('Invalid participation')
        self.fills=[];self.lots=[];self.targets={};self.target_routes={};self.orders={};self.seen=set();self.quotes={};self.rules={}
        self.marks={};self.last_frame=None;self.last_activity=None;self.frame_volume={};self.volume_used=defaultdict(int)
        self.account=None;self.counter_synced=False;self.last_counter_fee=None;self.pre_fill_commission=None
        self.fault=None;self.audit=[];self.current_day=None
        for event in self.journal.events:self._reduce(event)
        self.restored_unresolved=bool(self.live_orders())

    def record(self,event):
        self.journal.append(event);self._reduce(event)

    def live_orders(self):
        return [o for o in self.orders.values() if not o['terminal'] or o['filled']<o['reported_filled']]

    def physical_positions(self):
        return tuple(dict(p) for p in self.lots if p['quantity'])

    def _reduce(self,e):
        kind=e['kind']
        if kind=='day':
            for c in e['contracts']:self.contracts[c['id']]=contract_load(c)
            self.current_day=date.fromisoformat(e['day'])
            self.callback.stops.atr.update({(c,self.current_day):D(v) for c,v in e['atr'].items()})
            self.callback.begin_day(e['mains'],e['previous'])
        elif kind=='frame':
            now=datetime.fromisoformat(e['time']);self.last_frame=now
            self.marks={c:D(p) for c,p in e['marks'].items()}
            bars={c:market_load(v) for c,v in e['bars'].items()}
            auctions={c:market_load(v,True) for c,v in e['auctions'].items()}
            self.frame_volume.update({c:b.volume for c,b in bars.items()})
            self.volume_used.clear()
            for row in self.callback.logical_positions():
                assert row['instrument'] in self.contracts
            ctx=SimpleNamespace(time=now,bars=bars,auctions=auctions,marks=self.marks,
                account=SimpleNamespace(equity=D(e['equity'])),positions=self.physical_positions(),
                fills_since=lambda n:tuple(self.fills[n:]),target_group=lambda c,q,g:self.targets.__setitem__(g,(c,q)))
            self.callback(ctx)
            for g,(c,target) in self.targets.items():
                held=sum(p['quantity'] for p in self.lots if p['group']==g)
                side=1 if g.endswith('-long') else -1
                wanted=abs(target) if target*side>0 else 0
                effect=None if wanted==held else 'open' if wanted>held else 'close'
                self.target_routes[g]=(effect,self.callback.route.get((g,effect)))
            # Persisted journal is the complete diagnostic stream, not RAM lists.
            self.callback.logic.risk.clear();self.callback.risk.clear()
        elif kind=='intent':
            self.orders[e['client_id']]={**e,'broker_id':None,'filled':0,'reported_filled':0,'terminal':False,'cancel_requested':False}
        elif kind=='ack':self.orders[e['client_id']]['broker_id']=e['broker_id']
        elif kind=='cancel_request':self.orders[e['client_id']]['cancel_requested']=True
        elif kind=='status':
            o=self.orders[e['client_id']]
            if not o['filled'] <= max(o['filled'],e['reported_filled']) <= o['quantity']:
                raise ValueError('Invalid reported fill quantity')
            o['reported_filled']=max(o['reported_filled'],e['reported_filled'])
            o['terminal']=o['terminal'] or e['terminal']
        elif kind=='fill':
            key=tuple(e['key'])
            if key in self.seen:raise ValueError('Duplicate journal fill')
            o=self.orders[e['client_id']];q=e['quantity']
            if q<=0 or o['filled']+q>o['quantity']:raise ValueError('Overfill')
            self.seen.add(key);o['filled']+=q
            if o['filled']==o['quantity']:o['terminal']=True
            at=datetime.fromisoformat(e['time']);day=date.fromisoformat(e['day'])
            leg=Instruction(o['instrument'],o['side'],q,o['effect'],o['group'])
            fill=Fill(at,day,leg,D(e['price']),D(e['fee_estimate']))
            self.fills.append(fill)
            if o['effect']=='open':
                self.lots.append(dict(instrument=o['instrument'],side=o['side'],quantity=q,entry=fill.price,basis=fill.price,
                                      trading_date=day,group=o['group']))
            else:
                left=q
                for p in self.lots:
                    if p['instrument']==o['instrument'] and p['side']==o['side'] and p['group']==o['group']:
                        used=min(left,p['quantity']);left-=used;p['quantity']-=used
                if left:raise ValueError('Fill exceeds strategy-owned physical inventory')
                self.lots=[p for p in self.lots if p['quantity']]
            # Freeze interpretation to this order, not the mutable newest target.
            route_key=(o['group'],o['effect']);old=self.callback.route.get(route_key)
            self.callback.route[route_key]=(o['action'],o['logical_side'])
            self.callback.observe(SimpleNamespace(fills_since=lambda n:tuple(self.fills[n:])))
            self.callback.stops.fills_now(self.callback.logical_fills,at)
            if old is not None:self.callback.route[route_key]=old
            self.last_activity=datetime.fromisoformat(e.get('received_at',e['time']));self.counter_synced=False
        elif kind=='quote_risk':
            self.callback.stops.quote(e['instrument'],D(e['price']),datetime.fromisoformat(e['time']),datetime.fromisoformat(e['end']))
        elif kind=='halt':self.fault=e['reason']
        else:raise ValueError('Unknown journal event')

    def prepare_day(self,day,mains,previous,atr,contracts=None):
        with self.lock:
            if self.current_day==day:return
            if self.current_day and day<self.current_day:raise ValueError('Trading day moved backwards')
            if self.live_orders():raise ValueError('Unresolved orders at trading-day transition')
            self.record(dict(kind='day',day=str(day),mains=mains,previous=previous,
                             atr={c:str(a) for c,a in atr.items()},contracts=[contract_dict(c) for c in (contracts or self.contracts).values()]))

    def frame(self,at,bars,auctions=None):
        with self.lock:
            if self.fault or self.restored_unresolved:return False
            if self.account is None or at.date()!=self.current_day:return False
            if self.last_frame and at<=self.last_frame:return False
            if any(b.end!=at for b in bars.values()):raise ValueError('Frame end mismatch')
            marks=dict(self.marks)
            marks.update({c:b.close for c,b in bars.items()})
            marks.update({c:a.price for c,a in (auctions or {}).items()})
            if any(p['instrument'] not in marks for p in self.lots):return False
            e=dict(kind='frame',time=str(at),bars={c:asdict(b) for c,b in bars.items()},
                   auctions={c:asdict(a) for c,a in (auctions or {}).items()},marks=marks,equity=str(self.account.equity))
            # Roundtrip into the same JSON representation used by recovery.
            import json
            self.record(json.loads(json.dumps(e,default=str)))
            return True

    def synchronize(self,account,broker_positions,now):
        """Account observations, not a second cash book. Dedicated account required."""
        with self.lock:
            expected=defaultdict(int)
            for p in self.lots:expected[p['instrument'],p['side']]+=p['quantity']
            observed={k:v for k,v in broker_positions.items() if v}
            expected={k:v for k,v in expected.items() if v}
            self.account=account
            delayed=self.last_activity and now-self.last_activity<self.refresh_delay
            fee_delayed=(self.pre_fill_commission is not None and self.last_activity is not None and
                         account.commission<=self.pre_fill_commission)
            self.counter_synced=(not delayed and not fee_delayed and observed==expected and
                                 account.frozen_margin==0 and not self.restored_unresolved)
            if self.counter_synced:self.pre_fill_commission=None
            return self.counter_synced

    def receive_fill(self,client_id,trade_key,quantity,price,at,day,received_at=None):
        with self.lock:
            if tuple(trade_key) in self.seen:return False
            if client_id not in self.orders:
                self.halt('unattributed_account_trade');return False
            o=self.orders[client_id];c=self.contracts[o['instrument']]
            if day!=self.current_day or day!=self.rules[c.id].trading_day:
                self.halt('fill_trading_day_not_reconciled');return False
            fee=self.rules[c.id].fee(c,quantity,price,o['effect'])
            if fee>0 and self.pre_fill_commission is None:
                self.pre_fill_commission=D(o['counter_commission_before'])
            self.record(dict(kind='fill',client_id=client_id,key=list(trade_key),quantity=quantity,
                             price=str(price),fee_estimate=str(fee),time=str(at),day=str(day),received_at=str(received_at or at)))
            return True

    def order_status(self,client_id,reported_filled,terminal):
        with self.lock:
            if client_id not in self.orders:self.halt('unknown_order_status');return
            if type(reported_filled) is not int or not 0<=reported_filled<=self.orders[client_id]['quantity']:
                raise ValueError('Invalid cumulative order fill count')
            self.record(dict(kind='status',client_id=client_id,reported_filled=reported_filled,terminal=bool(terminal)))
            if self.restored_unresolved and not self.live_orders():self.restored_unresolved=False

    def halt(self,reason):
        if not self.fault:self.record(dict(kind='halt',reason=reason))

    def pump(self,gateway,now,allow_auction=False):
        with self.lock:
            if self.fault or self.restored_unresolved:return
            live=self.live_orders()
            for o in live:
                c,target=self.targets.get(o['group'],(o['instrument'],0))
                held=sum(p['quantity'] for p in self.lots if p['group']==o['group'])
                desired=abs(target) if target*o['side']>0 else 0
                outstanding=o['quantity']-o['filled']
                remaining=desired-held if o['effect']=='open' else held-desired
                obsolete=remaining!=outstanding or (now-datetime.fromisoformat(o['submitted'])>timedelta(seconds=20))
                if obsolete and o['broker_id'] is not None and not o['cancel_requested'] and not o['terminal']:
                    self.record(dict(kind='cancel_request',client_id=o['client_id']))
                    gateway.cancel(o['broker_id'])
            if live or self.last_frame is None or not self.counter_synced:return
            for group,(code,target) in sorted(self.targets.items(),key=lambda x:(
                abs(x[1][1])>=sum(p['quantity'] for p in self.lots if p['group']==x[0]),x[0])):
                held=sum(p['quantity'] for p in self.lots if p['group']==group)
                side=1 if group.endswith('-long') else -1
                desired=abs(target) if target*side>0 else 0
                if desired==held:continue
                effect='open' if desired>held else 'close';requested=abs(desired-held)
                expected_effect,frozen_route=self.target_routes.get(group,(None,None))
                if expected_effect!=effect or frozen_route is None:continue
                action,logical_side=frozen_route
                quote=self.quotes.get(code)
                if quote is None or not timedelta(0)<=now-quote.time<=timedelta(seconds=5):continue
                if quote.trading_day!=self.current_day or quote.time<self.last_frame:continue
                if not gateway.is_tradeable(code,allow_auction):continue
                cap=int(self.frame_volume.get(code,0)*self.participation)-self.volume_used[code]
                quantity=min(requested,max(cap,0))
                if not quantity:continue
                buy=(side==1)==(effect=='open')
                try:price=quote.limit_price(self.contracts[code],buy)
                except ValueError as exc:
                    self.audit.append(dict(time=str(now),code=code,reason=str(exc)));continue
                leg=Instruction(code,side,quantity,effect,group)
                result=self.gate.check(self.callback,leg,action,logical_side,price,now,self.account,
                                       self.physical_positions(),self.marks,self.rules)
                self.audit.append(dict(time=str(now),code=code,action=action,requested=quantity,**asdict(result)))
                if not result.allowed:continue
                client_id='rb'+uuid4().hex[:20]
                e=dict(kind='intent',client_id=client_id,instrument=code,side=side,effect=effect,group=group,
                       action=action,logical_side=logical_side,quantity=result.allowed,price=str(price),submitted=str(now),
                       counter_commission_before=str(self.account.commission))
                self.record(e)  # durable BEFORE external side effect
                try:broker_id=gateway.send(e)
                except Exception:
                    self.halt('ambiguous_send_result_requires_order_reconciliation');raise
                if broker_id is None or broker_id<0:
                    self.order_status(client_id,0,True)
                    self.audit.append(dict(time=str(now),code=code,reason='send_rejected'))
                else:self.record(dict(kind='ack',client_id=client_id,broker_id=broker_id))
                self.volume_used[code]+=result.allowed
                self.last_activity=now
                # Exactly one in-flight order; query and reconcile before another.
                self.counter_synced=False
                return
