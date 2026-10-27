"""External R-Breaker batches with physical same-contract long/short locks.

All fills, margin, fees, settlement and cash remain in the unified Engine/Ledger.
The callback owns only signal state, order targets and original-fill observations.
"""
from collections import defaultdict
from datetime import time

from .models import D
from .rbreaker import levels
from .rbreaker_portfolio import PRODUCTS,FEE,margin_usage


class LockedRBreaker:
    allow_close_today=False

    def __init__(self,instruments,atr,rate=D('.15'),margin_fraction=D('.05')):
        self.instruments,self.atr,self.rate=instruments,atr,rate
        self.margin_fraction=margin_fraction
        self.active,self.completed={},[]
        self.group_owner={}
        self.submitted={}
        self.cursor=self.counter=0
        self.events,self.audit,self.risk,self.actions=[],[],[],[]
        self.locked=set()
        self.desired={}
        self.modes={}

    def begin_day(self,mains,previous,reference_equity):
        self.mains,self.reference_equity=mains,D(reference_equity)
        self.levels={p:levels(previous[c]) for p,c in mains.items()}
        self.high,self.low={},{}

    @staticmethod
    def quantity(batch,side):
        return sum(lot[0] for lot in batch['lots'][side])

    def net(self,batch):
        return self.quantity(batch,1)-self.quantity(batch,-1)

    def old_quantity(self,batch,side,day):
        return sum(q for q,p,d in batch['lots'][side] if d<day)

    def observe(self,ctx):
        fills=ctx.fills_since(self.cursor)
        self.cursor+=len(fills)
        for fill in fills:
            leg=fill.instruction
            batch=self.active[self.group_owner[leg.group]]
            before=self.net(batch)
            inst=self.instruments[leg.instrument]
            if leg.effect=='open':
                batch['lots'][leg.side].append([leg.quantity,fill.price,fill.trading_date])
                batch['opened_quantity']+=leg.quantity
                batch['entry_time']=batch['entry_time'] or str(fill.time)
            else:
                left=leg.quantity
                while left:
                    lot=batch['lots'][leg.side][0]
                    used=min(left,lot[0])
                    # Hard assertion: every executed close is yesterday's inventory.
                    assert self.allow_close_today or lot[2]<fill.trading_date, (leg,fill.trading_date,lot[2])
                    batch['realized']+=used*leg.side*inst.multiplier*(fill.price-lot[1])
                    lot[0]-=used
                    left-=used
                    if not lot[0]:
                        batch['lots'][leg.side].pop(0)
            after=self.net(batch)
            if batch['state']=='entering' and abs(after)>abs(before):
                assert after*batch['direction']>0
                added=abs(after)-abs(before)
                batch['stop_basis']=(batch['stop_basis']*abs(before)+fill.price*added)/abs(after)
            batch['fees']+=fill.fee
            self.actions.append(dict(time=str(fill.time),day=fill.trading_date,batch_id=batch['id'],
                                     instrument=leg.instrument,group=leg.group,effect=leg.effect,side=leg.side,
                                     quantity=leg.quantity,state=batch['state'],reason=batch['reason'],
                                     net_before=before,net_after=after,price=fill.price,fee=fill.fee))
            if batch['stop_exit_pending']:
                self.locked.add((batch['product'],fill.trading_date))
        for key,batch in list(self.active.items()):
            net=self.net(batch)
            if batch['state']=='entering' and abs(net)>=batch['goal']:
                batch['state']='active'
            elif batch['state']=='exiting' and not net:
                batch['state']='locked'
                batch['stop_exit_pending']=False
            elif batch['state']=='closing_all' and not self.quantity(batch,1) and not self.quantity(batch,-1):
                batch['state']='locked'
            if batch['state']=='locked' and not self.quantity(batch,1) and not self.quantity(batch,-1):
                batch['exit_time']=str(ctx.time)
                if batch['opened_quantity']:
                    self.completed.append(batch)
                del self.active[key]

    @staticmethod
    def quote(ctx,code):
        bar=ctx.bars.get(code)
        if bar is not None:
            return bar.high,bar.low,bar.close
        auction=ctx.auctions.get(code)
        return (auction.price,auction.price,auction.price) if auction is not None else None

    def request_exit(self,batch,reason):
        batch['state']='exiting'
        batch['reason']=reason
        if reason=='atr_stop':
            batch['stop_exit_pending']=True

    def targets(self,batch,day):
        q={s:self.quantity(batch,s) for s in (1,-1)}
        if batch['state']=='closing_all':
            assert all(self.old_quantity(batch,s,day)==q[s] for s in (1,-1))
            return {s:0 for s in (1,-1)}
        if batch['state']=='entering':
            return dict(batch['entry_targets'])
        if batch['state']=='exiting':
            net=q[1]-q[-1]
            if net:
                side=1 if net>0 else -1
                close=min(abs(net),self.old_quantity(batch,side,day))
                q[side]-=close
                q[-side]+=abs(net)-close
        return q

    def reservation(self,ctx,plan):
        """Reserve larger sides, retaining unfilled close inventory and open targets."""
        held=defaultdict(int)
        for lot in ctx.positions:
            held[lot['group']]+=lot['quantity']
        sides=defaultdict(lambda:{1:D(0),-1:D(0)})
        fees=D(0)
        for batch in self.active.values():
            inst=self.instruments[batch['instrument']]
            mark=abs(ctx.marks[inst.id])
            for side,group in batch['groups'].items():
                current=held[group]
                target=plan.get(group,current)
                extra=max(target-current,0)
                sides[inst.product][side]+=(current*mark+extra*(mark+inst.tick))*inst.multiplier*self.rate
                fees+=abs(target-current)*(mark+inst.tick)*inst.multiplier*FEE
        return {p:max(v.values()) for p,v in sides.items()},fees

    def affordable(self,ctx,plan,product,protective=False):
        usage,fees=self.reservation(ctx,plan)
        base=margin_usage(ctx.positions,ctx.marks,self.instruments,self.rate)
        equity=ctx.account.equity-fees
        if equity<=0:
            return False
        total_limit=equity*D('.9')
        product_limit=equity*D('.5')
        if protective:
            # A zero-increment hedge can neutralize existing inventory above caps.
            total_limit=max(total_limit,sum(base.values(),D(0)))
            product_limit=max(product_limit,base.get(product,D(0)))
        return sum(usage.values(),D(0))<=total_limit and usage.get(product,D(0))<=product_limit

    def new_batch(self,code,direction,mode,ctx):
        self.counter+=1
        key=f'rbreaker-lock-{self.counter:06d}'
        groups={1:key+'-long',-1:key+'-short'}
        batch=dict(id=self.counter,group=key,groups=groups,instrument=code,product=self.instruments[code].product,
                   lots={1:[],-1:[]},state='locked',direction=direction,side=direction,mode=mode,
                   entry_time='',exit_time='',decision_time=str(ctx.time),opened_quantity=0,fees=D(0),realized=D(0),
                   stopped=False,trigger_time='',reason='new_entry',stop_basis=D(0),atr=D(0),distance=D(0),
                   stop_exit_pending=False,
                   goal=0,entry_targets={},reference_equity=self.reference_equity,
                   margin_budget=self.reference_equity*self.margin_fraction,budget=D(0),equity=self.reference_equity)
        self.active[key]=batch
        self.group_owner.update({g:key for g in groups.values()})
        return batch

    def __call__(self,ctx):
        self.observe(ctx)
        day=ctx.time.date()
        quantities={key:self.net(b) for key,b in self.active.items()}
        # Stops refer to the price at which net exposure was acquired, including unlocks.
        for batch in self.active.values():
            net=self.net(batch)
            quote=self.quote(ctx,batch['instrument'])
            if net and quote and batch['state'] in ('active','entering'):
                high,low,close=quote
                side=1 if net>0 else -1
                threshold=batch['stop_basis']-side*batch['distance']
                if (low<=threshold if side>0 else high>=threshold):
                    batch.update(stopped=True,trigger_time=str(ctx.time))
                    self.request_exit(batch,'atr_stop')
                    self.locked.add((batch['product'],day))
                    self.desired.pop(batch['product'],None)
                    self.events.append(dict(trade_id=batch['id'],instrument=batch['instrument'],product=batch['product'],
                                            trigger_time=str(ctx.time),reason='atr_price',quantity=abs(net),side=side,
                                            entry_average=batch['stop_basis'],stop_price=threshold,distance=batch['distance'],
                                            bar_extreme=low if side>0 else high,bar_close=close))
            if batch['instrument']!=self.mains.get(batch['product']):
                batch.update(state='closing_all',reason='old_main_auction_exit')
        candidates=[]
        for product,code in self.mains.items():
            quote=self.quote(ctx,code)
            if quote is None:
                continue
            high,low,price=quote
            self.high[product]=max(self.high.get(product,high),high)
            self.low[product]=min(self.low.get(product,low),low)
            k=self.levels[product]
            batches=[b for b in self.active.values() if b['product']==product]
            net=sum(self.net(b) for b in batches)
            if ctx.time.time()>=time(14,59):
                self.desired.pop(product,None)
                for batch in batches:
                    if batch['state']=='entering':
                        batch['state']='active' if self.net(batch) else 'locked'
                    if ctx.time.time()==time(14,59) and self.net(batch) and batch['state']=='active':
                        keep=batch['mode']=='breakout' and (self.net(batch)>0 and price>k['bbreak'] or self.net(batch)<0 and price<k['sbreak'])
                        if not keep:
                            self.request_exit(batch,'end_of_day_non_breakout')
                continue
            reverse=(net>0 and self.high[product]>k['ssetup'] and price<k['senter'] or
                     net<0 and self.low[product]<k['bsetup'] and price>k['benter'])
            if reverse:
                self.desired[product]=-1 if net>0 else 1
                self.modes[product]='reversal'
                for batch in batches:
                    if self.net(batch) and batch['state'] not in ('closing_all','exiting'):
                        self.request_exit(batch,'reversal')
            blocked=(product,day) in self.locked or any(b['state']=='exiting' and b['stopped'] for b in batches)
            if blocked:
                self.desired.pop(product,None)
                for batch in batches:
                    if batch['state']=='entering':
                        batch['state']='active' if self.net(batch) else 'locked'
            if blocked or any(b['state'] in ('entering','exiting','closing_all') for b in batches):
                continue
            direction=self.desired.get(product) if not net else (1 if net>0 else -1)
            if direction:
                mode=self.modes.get(product,'breakout')
                eligible=True if not net else ((price>k['bbreak'] if direction>0 else price<k['sbreak']) if mode=='breakout' else
                         (self.low[product]<k['bsetup'] and price>k['benter'] if direction>0 else self.high[product]>k['ssetup'] and price<k['senter']))
            else:
                direction=1 if price>k['bbreak'] else -1 if price<k['sbreak'] else 0
                mode='breakout'
                eligible=bool(direction)
            atr=self.atr.get((code,day))
            if not eligible or atr is None or atr<=0 or self.reference_equity<=0:
                continue
            inst=self.instruments[code]
            size=int(self.reference_equity*self.margin_fraction/((abs(ctx.marks[code])+inst.tick)*inst.multiplier*self.rate))//inst.lot*inst.lot
            size,batch=self.select_inventory(batches,code,direction,size,day)
            if size:
                candidates.append((product,code,direction,mode,atr,size,batch))
        plan={}
        for batch in self.active.values():
            plan.update({batch['groups'][s]:q for s,q in self.targets(batch,day).items()})
        self.resize_pending(ctx,plan)
        # Protective lock orders reserve actual larger-side margin, never the sum.
        for batch in self.active.values():
            if batch['state']!='exiting':
                continue
            for side,group in batch['groups'].items():
                current=self.quantity(batch,side)
                if plan[group]>current and not self.affordable(ctx,plan,batch['product'],protective=True):
                    wanted=plan[group]
                    while wanted>current and not self.affordable(ctx,plan,batch['product'],protective=True):
                        wanted=(current+wanted)//2
                        plan[group]=wanted
        self.admit_candidates(ctx,candidates,plan)
        self.current_plan=dict(plan)
        # Each group has a fixed physical side; both sides are real Engine positions.
        for batch in self.active.values():
            for side,group in batch['groups'].items():
                target=side*plan.get(group,self.quantity(batch,side))
                if self.submitted.get(group)!=target:
                    ctx.target_group(batch['instrument'],target,group)
                    self.submitted[group]=target
        usage=margin_usage(ctx.positions,ctx.marks,self.instruments,self.rate)
        self.risk.append(dict(time=str(ctx.time),equity=ctx.account.equity,margin=sum(usage.values(),D(0)),
                              usage=sum(usage.values(),D(0))/ctx.account.equity if ctx.account.equity>0 else None,
                              **{p+'_usage':usage.get(p,D(0))/ctx.account.equity if ctx.account.equity>0 else None for p in PRODUCTS}))

    def select_inventory(self,batches,code,direction,size,day):
        reusable=[b for b in batches if b['instrument']==code and b['state']=='locked' and self.quantity(b,1) and
                  self.old_quantity(b,-direction,day)>0]
        batch=reusable[0] if reusable else None
        if batch:
            size=min(size,self.quantity(batch,direction),self.old_quantity(batch,-direction,day))
        return size,batch

    def resize_pending(self,ctx,plan):
        for batch in self.active.values():
            if batch['state']!='entering' or batch['reason']!='new_entry':
                continue
            side=batch['direction'];group=batch['groups'][side]
            current=self.quantity(batch,side)
            wanted=plan[group]
            inst=self.instruments[batch['instrument']]
            unit=(abs(ctx.marks[inst.id])+inst.tick)*inst.multiplier*self.rate
            wanted=min(wanted,max(current,int(batch['margin_budget']/unit)))
            plan[group]=current
            while wanted>current:
                plan[group]=wanted
                if self.affordable(ctx,plan,batch['product']):
                    break
                wanted=(current+wanted)//2
            plan[group]=wanted

    def admit_candidates(self,ctx,candidates,plan):
        day=ctx.time.date()
        for product,code,direction,mode,atr,requested,batch in candidates:
            new=batch is None
            if new:
                batch=self.new_batch(code,direction,mode,ctx)
                for group in batch['groups'].values():
                    plan[group]=0
            side=direction if new else -direction
            group=batch['groups'][side]
            current=self.quantity(batch,side)
            lower,upper=0,requested
            while lower<upper:
                trial=(lower+upper+1)//2
                plan[group]=current+trial if new else current-trial
                if self.affordable(ctx,plan,product,protective=not new):
                    lower=trial
                else:
                    upper=trial-1
            size=lower
            plan[group]=current+size if new else current-size
            if not size:
                if new:
                    del self.active[batch['group']]
                    for g in batch['groups'].values():
                        plan.pop(g,None)
                continue
            batch.update(state='entering',direction=direction,mode=mode,goal=size,atr=atr,distance=atr,
                         decision_time=str(ctx.time),stop_basis=D(0),stopped=False,
                         stop_exit_pending=False,
                         reason='new_entry' if new else 'unlock',reference_equity=self.reference_equity,
                         margin_budget=self.reference_equity*self.margin_fraction,
                         entry_targets={s:plan.get(g,self.quantity(batch,s)) for s,g in batch['groups'].items()})
            self.modes[product]=mode
            self.desired.pop(product,None)
            usage,fees=self.reservation(ctx,plan)
            actual=margin_usage(ctx.positions,ctx.marks,self.instruments,self.rate)
            self.audit.append(dict(time=str(ctx.time),instrument=code,group=batch['group'],action=batch['reason'],
                                   side=direction,new_lots=size,requested_lots=requested,equity=ctx.account.equity,
                                   reference_equity=self.reference_equity,margin_budget=batch['margin_budget'],
                                   projected_used=sum(usage.values(),D(0)),projected_product_used=usage.get(product,D(0)),fee_reserve=fees,
                                   total_used=sum(actual.values(),D(0)),product_used=actual.get(product,D(0)),
                                   long_quantity=self.quantity(batch,1),short_quantity=self.quantity(batch,-1),
                                   old_opposite_quantity=self.old_quantity(batch,-direction,day),
                                   planned_long=plan.get(batch['groups'][1],0),planned_short=plan.get(batch['groups'][-1],0),
                                   atr=atr,stop_distance=atr))

    def records(self,marks):
        result=[]
        for batch in self.completed+list(self.active.values()):
            if not batch['opened_quantity']:
                continue
            inst=self.instruments[batch['instrument']]
            pnl=batch['realized']-batch['fees']+sum((q*s*inst.multiplier*(marks[inst.id]-entry)
                       for s,lots in batch['lots'].items() for q,entry,day in lots),D(0))
            result.append({**{k:v for k,v in batch.items() if k not in ('groups','lots','entry_targets')},
                           'maximum':batch['opened_quantity'],'remaining_quantity':sum(self.quantity(batch,s) for s in (1,-1)),
                           'long_quantity':self.quantity(batch,1),'short_quantity':self.quantity(batch,-1),
                           'locked_quantity':min(self.quantity(batch,1),self.quantity(batch,-1)),
                           'net_quantity':self.net(batch),'net_pnl':pnl})
        return sorted(result,key=lambda r:r['id'])
