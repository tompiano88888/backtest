"""External R-Breaker callback: repeated margin batches, independent cash stops."""
from collections import defaultdict
from datetime import time

from .models import D
from .rbreaker import levels
from .rbreaker_portfolio import PRODUCTS,FEE,allocate,margin_usage


class BatchRBreaker:
    def __init__(self,instruments,rate=D('.15'),margin_fraction=D('.05'),stop_fraction=D('.005')):
        if not 0<margin_fraction<=D('.5') or not 0<stop_fraction<1:
            raise ValueError('Invalid batch fractions')
        self.instruments,self.rate=instruments,rate
        self.margin_fraction,self.stop_fraction=margin_fraction,stop_fraction
        self.active,self.completed={},[]
        self.events,self.audit,self.risk=[],[],[]
        self.cursor=self.counter=0
        self.locked=set()
        self.desired={}
        self.modes={}

    def begin_day(self,mains,previous,reference_equity):
        self.mains,self.reference_equity=mains,D(reference_equity)
        self.levels={p:levels(previous[c]) for p,c in mains.items()}
        self.high,self.low={},{}

    def observe(self,ctx):
        fills=ctx.fills_since(self.cursor)
        self.cursor+=len(fills)
        for fill in fills:
            leg=fill.instruction
            batch=self.active[leg.group]
            inst=self.instruments[leg.instrument]
            if leg.effect=='open':
                batch['entry_time']=batch['entry_time'] or str(fill.time)
                batch['opened_quantity']+=leg.quantity
                batch['lots'].append([leg.quantity,fill.price])
            else:
                left=leg.quantity
                while left:
                    lot=batch['lots'][0]
                    used=min(lot[0],left)
                    batch['realized']+=used*leg.side*inst.multiplier*(fill.price-lot[1])
                    left-=used
                    lot[0]-=used
                    if not lot[0]:
                        batch['lots'].pop(0)
                if batch['stopped']:
                    self.locked.add((batch['product'],fill.trading_date))
            batch['fees']+=fill.fee
            if leg.effect=='close' and not batch['lots']:
                batch['exit_time']=str(fill.time)
                self.completed.append(batch)
                del self.active[leg.group]

    def quote(self,ctx,code):
        b=ctx.bars.get(code)
        if b is not None:
            return b.high,b.low,b.close
        a=ctx.auctions.get(code)
        return (a.price,a.price,a.price) if a is not None else None

    def __call__(self,ctx):
        self.observe(ctx)
        equity=ctx.account.equity
        usage=margin_usage(ctx.positions,ctx.marks,self.instruments,self.rate)
        quantities=defaultdict(int)
        for p in ctx.positions:
            quantities[p['group']]+=p['side']*p['quantity']
        for group,batch in list(self.active.items()):
            code=batch['instrument']
            quote=self.quote(ctx,code)
            if batch['lots'] and not batch['stopped'] and quote:
                high,low,close=quote
                extreme=low if batch['side']>0 else high
                qty=sum(q for q,_ in batch['lots'])
                unit=self.instruments[code].multiplier
                net=batch['realized']-batch['fees']+sum(q*batch['side']*unit*(extreme-entry) for q,entry in batch['lots'])
                exit_cost=qty*unit*(abs(extreme)*FEE+self.instruments[code].tick)
                if net-exit_cost<=-batch['budget']:
                    batch.update(stopped=True,closing=True,exit_reason='fixed_cash_stop',trigger_time=str(ctx.time))
                    self.locked.add((batch['product'],ctx.time.date()))
                    self.desired.pop(batch['product'],None)
                    average=sum(q*entry for q,entry in batch['lots'])/qty
                    distance=max(batch['budget']+batch['realized']-batch['fees']-exit_cost,D(0))/(qty*unit)
                    self.events.append(dict(trade_id=batch['id'],group=group,instrument=code,product=batch['product'],
                                            trigger_time=str(ctx.time),reason='fixed_cash_stop',side=batch['side'],quantity=qty,
                                            entry_average=average,stop_price=average-batch['side']*distance,
                                            bar_extreme=extreme,bar_close=close,net_at_extreme=net,budget=batch['budget']))
            if code!=self.mains.get(batch['product']):
                batch.update(closing=True,exit_reason=batch['exit_reason'] or 'main_change')
        # Derive strategy direction from current common observations, then add one batch.
        candidates={}
        for product,code in self.mains.items():
            quote=self.quote(ctx,code)
            if quote is None:
                continue
            high,low,price=quote
            self.high[product]=max(self.high.get(product,high),high)
            self.low[product]=min(self.low.get(product,low),low)
            k=self.levels[product]
            batches=[b for b in self.active.values() if b['product']==product]
            held=sum(quantities[b['group']] for b in batches)
            if ctx.time.time()>=time(14,59):
                self.desired.pop(product,None)
                if ctx.time.time()==time(14,59):
                    for batch in batches:
                        keep=batch['mode']=='breakout' and (batch['side']>0 and price>k['bbreak'] or batch['side']<0 and price<k['sbreak'])
                        if not keep:
                            batch.update(closing=True,exit_reason=batch['exit_reason'] or 'end_of_day_non_breakout')
                        else:
                            # No unfilled entry remainder may carry into the close/auction.
                            batch['target_quantity']=batch['opened_quantity']
                continue
            # Reverse the whole product, but never reopen before actual old exits.
            reverse=(held>0 and self.high[product]>k['ssetup'] and price<k['senter'] or
                     held<0 and self.low[product]<k['bsetup'] and price>k['benter'])
            if reverse:
                self.desired[product]=-1 if held>0 else 1
                self.modes[product]='reversal'
                for batch in batches:
                    batch.update(closing=True,exit_reason=batch['exit_reason'] or 'reversal')
            locked=(product,ctx.time.date()) in self.locked or any(b['stopped'] for b in batches)
            if locked:
                self.desired.pop(product,None)
                for batch in batches:
                    if not batch['closing'] and batch['opened_quantity']<batch['target_quantity']:
                        batch['target_quantity']=batch['opened_quantity']
                        ctx.target_group(batch['instrument'],quantities[batch['group']],batch['group'])
                        if not batch['lots']:
                            self.active.pop(batch['group'],None)
            if locked or any(b['closing'] for b in batches):
                continue
            pending=[b for b in batches if b['opened_quantity']<b['target_quantity']]
            if pending:
                batch=pending[0]
                cost=(abs(ctx.marks[code])+self.instruments[code].tick)*self.instruments[code].multiplier*self.rate
                requested=min(batch['target_quantity']-batch['opened_quantity'],
                              max(int(batch['margin_budget']/cost)-abs(quantities[batch['group']]),0))
                candidates[code]=dict(side=batch['side'],requested=requested,batch=batch)
                continue
            direction=1 if held>0 else -1 if held<0 else 0
            desired=self.desired.get(product)
            if desired and not held:
                direction=desired
                mode='reversal'
                eligible=True
            elif not held:
                direction=1 if price>k['bbreak'] else -1 if price<k['sbreak'] else 0
                mode='breakout'
                eligible=bool(direction)
            else:
                mode=self.modes.get(product,batches[-1]['mode'] if batches else 'breakout')
                eligible=(price>k['bbreak'] if direction>0 else price<k['sbreak']) if mode=='breakout' else (
                    self.low[product]<k['bsetup'] and price>k['benter'] if direction>0 else self.high[product]>k['ssetup'] and price<k['senter'])
            if not eligible or self.reference_equity<=0:
                continue
            inst=self.instruments[code]
            cost=(abs(ctx.marks[code])+inst.tick)*inst.multiplier*self.rate
            requested=int(self.reference_equity*self.margin_fraction/cost)//inst.lot*inst.lot
            if requested:
                candidates[code]=dict(side=direction,requested=requested,batch=None,mode=mode)
        # Close only the identified independent batch; preserve other batch ownership.
        for group,batch in list(self.active.items()):
            if batch['closing']:
                ctx.target_group(batch['instrument'],0,group)
                if not batch['lots']:
                    self.active.pop(group)
            elif ctx.time.time()>=time(14,59):
                ctx.target_group(batch['instrument'],quantities[group],group)
                if not batch['lots']:
                    self.active.pop(group)
        sizes=allocate(equity,usage,{c:(v['side'],v['requested']) for c,v in candidates.items()},
                       self.instruments,ctx.marks,self.rate)
        projected=sum(usage.values(),D(0))
        fees=D(0)
        for code,size in sizes.items():
            inst=self.instruments[code]
            nominal=size*(abs(ctx.marks[code])+inst.tick)*inst.multiplier
            projected+=nominal*self.rate
            fees+=nominal*FEE
        if any(sizes.values()):
            assert projected<=(equity-fees)*D('.9')
        for code,size in sizes.items():
            request=candidates[code]
            batch=request['batch']
            inst=self.instruments[code]
            if size:
                assert usage.get(inst.product,D(0))+size*(abs(ctx.marks[code])+inst.tick)*inst.multiplier*self.rate <= (equity-fees)*D('.5')
            if batch is None:
                if not size:
                    continue
                self.counter+=1
                group=f'rbreaker-batch-{self.counter:06d}'
                batch=dict(id=self.counter,group=group,instrument=code,product=inst.product,side=request['side'],
                           decision_time=str(ctx.time),entry_time='',exit_time='',opened_quantity=0,target_quantity=size,
                           maximum=request['requested'],reference_equity=self.reference_equity,equity=self.reference_equity,
                           margin_budget=self.reference_equity*self.margin_fraction,budget=self.reference_equity*self.stop_fraction,
                           mode=request['mode'],lots=[],realized=D(0),fees=D(0),stopped=False,closing=False,
                           exit_reason='',trigger_time='')
                self.active[group]=batch
                self.modes[inst.product]=request['mode']
                self.desired.pop(inst.product,None)
            held=quantities[batch['group']]
            ctx.target_group(code,held+batch['side']*size,batch['group'])
            self.audit.append(dict(time=str(ctx.time),instrument=code,group=batch['group'],held=held,
                                   target=held+batch['side']*size,new_lots=size,equity=equity,
                                   total_used=sum(usage.values(),D(0)),product_used=usage.get(inst.product,D(0)),
                                   projected_used=projected,fee_reserve=fees,reference_equity=batch['reference_equity'],
                                   margin_budget=batch['margin_budget'],stop_budget=batch['budget']))
        self.risk.append(dict(time=str(ctx.time),equity=equity,margin=sum(usage.values(),D(0)),
                              usage=sum(usage.values(),D(0))/equity if equity>0 else None,
                              **{p+'_usage':usage.get(p,D(0))/equity if equity>0 else None for p in PRODUCTS}))

    def records(self,marks):
        records=[]
        for b in self.completed+list(self.active.values()):
            if not b['opened_quantity']:
                continue
            unit=self.instruments[b['instrument']].multiplier
            pnl=b['realized']-b['fees']+sum((q*b['side']*unit*(marks[b['instrument']]-entry) for q,entry in b['lots']),D(0))
            records.append({**{k:v for k,v in b.items() if k!='lots'},
                            'remaining_quantity':sum(q for q,_ in b['lots']),'net_pnl':pnl})
        return sorted(records,key=lambda r:r['id'])
