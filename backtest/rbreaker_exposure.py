"""External net-exposure budgets and execution guard for the lock experiment."""
from collections import defaultdict
from datetime import timedelta

from .models import D
from .rbreaker_locks import LockedRBreaker
from .rbreaker_portfolio import PRODUCTS,FEE,margin_usage


def exposure_metrics(positions,marks,instruments,rate):
    net=defaultdict(int)
    for p in positions:
        net[p['instrument']]+=p['side']*p['quantity']
    exposure=defaultdict(lambda:D(0))
    for code,quantity in net.items():
        inst=instruments[code]
        exposure[inst.product]+=abs(quantity)*abs(marks[code])*inst.multiplier*rate
    margin=margin_usage(positions,marks,instruments,rate)
    return net,dict(exposure),margin


class NetExposureRBreaker(LockedRBreaker):
    def __init__(self,instruments,atr,rate=D('.15'),margin_fraction=D('.05'),direct_close=False):
        super().__init__(instruments,atr,rate,margin_fraction)
        self.direct_close=self.allow_close_today=direct_close
        self.intents={}
        self.intent_counter=0
        self.routing_audit=[]
        self.guard_audit=[]
        self.observations=[]
        self.observation_sink=self.breach_sink=self.stress_sink=None
        self.current_plan={}
        self.funding=dict(observations=0,breaches=0,max_ratio=D(0),max_gap=D(0),first_breach=None,
                          status='passed',guard_shrunk=0,guard_deferred=0)

    def select_inventory(self,batches,code,direction,size,day):
        # One candidate means one logical 5% batch; physical routing happens later.
        return size,None

    def targets(self,batch,day):
        if self.direct_close and batch['state']=='exiting':
            return {1:0,-1:0}
        return super().targets(batch,day)

    def _actual(self,ctx):
        return exposure_metrics(ctx.positions,ctx.marks,self.instruments,self.rate)

    def reservation(self,ctx,plan):
        usage,_=super().reservation(ctx,plan)
        quantities=defaultdict(int)
        for p in ctx.positions:
            quantities[p['group']]+=p['quantity']
        fees=D(0)
        for batch in self.active.values():
            inst=self.instruments[batch['instrument']]
            price=abs(ctx.marks[inst.id])+inst.tick
            for side,group in batch['groups'].items():
                change=plan.get(group,quantities[group])-quantities[group]
                fees+=abs(change)*price*inst.multiplier*FEE
                if self.direct_close and change<0:
                    left=-change;today=0
                    for p in ctx.positions:
                        if p['group']==group:
                            used=min(left,p['quantity'])
                            today+=used if p['trading_date']==ctx.time.date() else 0
                            left-=used
                            if not left:
                                break
                    fees+=today*price*inst.multiplier*(D('.00023')-FEE)
        return usage,fees

    def reserved_exposure(self,ctx,plan):
        net,_,_=self._actual(ctx)
        quantities=defaultdict(int)
        for p in ctx.positions:
            quantities[p['group']]+=p['quantity']
        for batch in self.active.values():
            if batch['state']!='entering':
                continue
            actual=quantities[batch['groups'][1]]-quantities[batch['groups'][-1]]
            target=plan.get(batch['groups'][1],quantities[batch['groups'][1]])-plan.get(batch['groups'][-1],quantities[batch['groups'][-1]])
            direction=batch['direction']
            extra=max(direction*(target-actual),0)
            net[batch['instrument']]+=direction*extra
        result=defaultdict(lambda:D(0))
        for code,q in net.items():
            inst=self.instruments[code]
            result[inst.product]+=abs(q)*(abs(ctx.marks[code])+inst.tick)*inst.multiplier*self.rate
        return dict(result)

    def affordable(self,ctx,plan,product,protective=False):
        margin,fees=self.reservation(ctx,plan)
        _,exposure,actual=self._actual(ctx)
        equity=ctx.account.equity-fees
        if equity<=0:
            return False
        total=sum(margin.values(),D(0));current=sum(actual.values(),D(0))
        if protective:
            return total<=max(current,equity*D('.9'))
        projected=self.reserved_exposure(ctx,plan)
        return (current<ctx.account.equity*D('.9') and exposure.get(product,D(0))<ctx.account.equity*D('.5')
                and total<=equity*D('.9') and projected.get(product,D(0))<=equity*D('.5'))

    def resize_pending(self,ctx,plan):
        quantities=defaultdict(int)
        for p in ctx.positions:
            quantities[p['group']]+=p['quantity']
        pending_ids={b.get('intent_id') for b in self.active.values() if b['state']=='entering'}
        for intent_id in pending_ids:
            intent=self.intents[intent_id]
            members=[self.active[k] for k in intent['components'] if k in self.active and self.active[k].get('intent_id')==intent['id']]
            if not any(b['state']=='entering' for b in members):
                continue
            inst=self.instruments[intent['instrument']]
            unit=(abs(ctx.marks[inst.id])+inst.tick)*inst.multiplier*self.rate
            acquired=sum(intent['side']*(quantities[b['groups'][1]]-quantities[b['groups'][-1]]) for b in members)
            remaining=max(min(intent['goal'],int(intent['margin_budget']/unit))-acquired,0)
            for batch in members:
                if batch['state']!='entering':
                    continue
                direction=batch['direction']
                held=direction*(quantities[batch['groups'][1]]-quantities[batch['groups'][-1]])
                wanted=min(max(batch['goal']-held,0),remaining)
                remaining-=wanted
                side=direction if batch['reason']=='new_entry' else -direction
                group=batch['groups'][side]
                current=quantities[group]
                plan[group]=current
                lower,upper=0,wanted
                while lower<upper:
                    count=(lower+upper+1)//2
                    plan[group]=current+count if side==direction else current-count
                    if self.affordable(ctx,plan,batch['product']):
                        lower=count
                    else:
                        upper=count-1
                plan[group]=current+lower if side==direction else current-lower

    def admit_candidates(self,ctx,candidates,plan):
        for product,code,direction,mode,atr,requested,_ in candidates:
            reusable=[] if self.direct_close else [b for b in self.active.values() if b['instrument']==code and b['state']=='locked'
                      and self.quantity(b,1)>0 and self.old_quantity(b,-direction,ctx.time.date())>0]
            fresh=self.new_batch(code,direction,mode,ctx)
            for g in fresh['groups'].values():
                plan[g]=0
            choices=[(b,min(self.quantity(b,direction),self.old_quantity(b,-direction,ctx.time.date())),'unlock') for b in reusable]
            choices.append((fresh,requested,'new_entry'))
            original={g:plan.get(g,self.quantity(b,s)) for b,_,_ in choices for s,g in b['groups'].items()}
            def route(count):
                plan.update(original)
                parts=[]
                for batch,available,action in choices:
                    take=min(count,available)
                    if take:
                        side=direction if action=='new_entry' else -direction
                        group=batch['groups'][side]
                        plan[group]=original[group]+take if action=='new_entry' else original[group]-take
                        parts.append((batch,take,action))
                        count-=take
                    if not count:
                        break
                return parts
            lower,upper=0,requested
            # Mark candidate members as risk-increasing for reservation only.
            saved={b['group']:(b['state'],b['direction']) for b,_,_ in choices}
            for b,_,_ in choices:
                b.update(state='entering',direction=direction)
            while lower<upper:
                count=(lower+upper+1)//2
                route(count)
                if self.affordable(ctx,plan,product):
                    lower=count
                else:
                    upper=count-1
            parts=route(lower)
            for b,_,_ in choices:
                b['state'],b['direction']=saved[b['group']]
            if not any(b is fresh for b,_,_ in parts):
                del self.active[fresh['group']]
                for g in fresh['groups'].values():
                    plan.pop(g,None)
                    self.group_owner.pop(g,None)
            if not lower:
                continue
            self.intent_counter+=1
            intent=dict(id=self.intent_counter,instrument=code,product=product,side=direction,mode=mode,
                        decision_time=str(ctx.time),reference_equity=self.reference_equity,
                        margin_budget=self.reference_equity*self.margin_fraction,goal=lower,requested=requested,
                        atr=atr,basis=D(0),acquired_quantity=0,components=[b['group'] for b,_,_ in parts])
            self.intents[intent['id']]=intent
            for batch,size,action in parts:
                batch.update(state='entering',direction=direction,mode=mode,goal=size,atr=atr,distance=atr,
                             decision_time=str(ctx.time),stop_basis=D(0),stopped=False,stop_exit_pending=False,
                             reason=action,intent_id=intent['id'],reference_equity=self.reference_equity,
                             margin_budget=intent['margin_budget'],
                             entry_targets={s:plan.get(g,self.quantity(batch,s)) for s,g in batch['groups'].items()})
                self.routing_audit.append(dict(time=str(ctx.time),intent_id=intent['id'],group=batch['group'],
                                               instrument=code,action=action,side=direction,quantity=size))
            self.modes[product]=mode;self.desired.pop(product,None)
            margin,fees=self.reservation(ctx,plan)
            projected=self.reserved_exposure(ctx,plan)
            _,actual_exposure,actual_margin=self._actual(ctx)
            action=parts[0][2] if all(a==parts[0][2] for b,q,a in parts) else 'mixed'
            self.audit.append(dict(time=str(ctx.time),instrument=code,group=f'exposure-{intent["id"]:06d}',intent_id=intent['id'],
                                   action=action,side=direction,new_lots=lower,requested_lots=requested,
                                   equity=ctx.account.equity,reference_equity=self.reference_equity,margin_budget=intent['margin_budget'],
                                   total_used=sum(actual_margin.values(),D(0)),product_used=actual_exposure.get(product,D(0)),
                                   projected_used=sum(margin.values(),D(0)),projected_product_used=projected.get(product,D(0)),
                                   fee_reserve=fees,atr=atr,stop_distance=atr,components=len(parts)))

    def observe(self,ctx):
        cursor=len(self.actions)
        super().observe(ctx)
        for row in self.actions[cursor:]:
            key=self.group_owner[row['group']]
            batch=self.active.get(key)
            if batch is None:
                # Container may have been fully closed in super().observe.
                batch=next(b for b in reversed(self.completed) if b['group']==key)
            intent_id=batch.get('intent_id')
            row['intent_id']=intent_id
            if intent_id and row['state']=='entering':
                added=abs(row['net_after'])-abs(row['net_before'])
                if added>0:
                    intent=self.intents[intent_id]
                    old=intent['acquired_quantity']
                    intent['basis']=(intent['basis']*old+row['price']*added)/(old+added)
                    intent['acquired_quantity']+=added
        for batch in self.active.values():
            intent=self.intents.get(batch.get('intent_id'))
            if intent:
                batch['stop_basis']=intent['basis']
                inst=self.instruments[intent['instrument']]
                capacity=int(intent['margin_budget']/((abs(ctx.marks[inst.id])+inst.tick)*inst.multiplier*self.rate))
                if batch['state']=='entering' and intent['acquired_quantity'] and intent['acquired_quantity']>=capacity:
                    intent['goal']=intent['acquired_quantity']
                    batch['goal']=abs(self.net(batch))
                    batch['state']='active' if self.net(batch) else 'locked'

    def request_exit(self,batch,reason):
        if reason=='atr_stop' and batch.get('intent_id'):
            intent=self.intents[batch['intent_id']]
            for key in intent['components']:
                member=self.active.get(key)
                if member and member.get('intent_id')==intent['id'] and member['state'] in ('active','entering'):
                    member.update(stopped=True,trigger_time=batch['trigger_time'])
                    super().request_exit(member,reason)
        else:
            super().request_exit(batch,reason)

    def __call__(self,ctx):
        event_start=len(self.events)
        super().__call__(ctx)
        for event in self.events[event_start:]:
            key=f'rbreaker-lock-{event["trade_id"]:06d}'
            batch=self.active.get(key)
            if batch and batch.get('intent_id'):
                intent=self.intents[batch['intent_id']]
                event['intent_id']=intent['id']
                event['quantity']=sum(abs(self.net(self.active[k])) for k in intent['components'] if k in self.active and self.active[k].get('intent_id')==intent['id'])
        if self.stress_sink and ctx.bars:
            net,_,_=self._actual(ctx)
            adverse=dict(ctx.marks);upper=dict(ctx.marks)
            equity=ctx.account.equity
            for code,bar in ctx.bars.items():
                q=net.get(code,0)
                adverse[code]=bar.low if q>0 else bar.high if q<0 else bar.close
                upper[code]=bar.high
                equity+=q*self.instruments[code].multiplier*(adverse[code]-ctx.marks[code])
            margin=sum(margin_usage(ctx.positions,upper,self.instruments,self.rate).values(),D(0))
            self.stress_sink(dict(time=str(ctx.time),equity_lower=equity,margin_upper=margin,
                                  ratio_upper=margin/equity if equity>0 else None,
                                  stressed=equity<=0 or margin>equity))

    def execution_guard(self,ctx,candidate):
        leg=candidate.instruction
        batch=self.active[self.group_owner[leg.group]]
        increasing=batch['state']=='entering'
        net,exposure,margin=self._actual(ctx)
        current_total=sum(margin.values(),D(0))
        inst=self.instruments[leg.instrument]
        product=inst.product
        intent=self.intents.get(batch.get('intent_id'))
        def evaluate(quantity):
            fill=ctx.fill_for_quantity(quantity)
            delta=leg.side*quantity*(1 if leg.effect=='open' else -1)
            equity=ctx.account.equity+delta*inst.multiplier*(ctx.marks[inst.id]-fill.price)-fill.fee
            positions=[dict(p) for p in ctx.positions]
            if leg.effect=='open':
                positions.append(dict(instrument=inst.id,side=leg.side,quantity=quantity,group=leg.group))
            else:
                left=quantity
                for p in positions:
                    if p['group']==leg.group and left:
                        used=min(p['quantity'],left);p['quantity']-=used;left-=used
            _,after_exposure,after_margin=exposure_metrics(positions,ctx.marks,self.instruments,self.rate)
            total=sum(after_margin.values(),D(0))
            reason='accepted'
            logical_quantity=0
            if increasing:
                owned={g for key in intent['components'] if key in self.active and self.active[key].get('intent_id')==intent['id'] for g in self.active[key]['groups'].values()}
                logical_quantity=sum(intent['side']*p['side']*p['quantity'] for p in positions if p['group'] in owned)
                unit=max(abs(ctx.marks[inst.id]),abs(fill.price))*inst.multiplier*self.rate
                if ctx.account.equity<=0 or equity<=0:reason='nonpositive_equity'
                elif current_total>=ctx.account.equity*D('.9'):reason='current_margin_90'
                elif exposure.get(product,D(0))>=ctx.account.equity*D('.5'):reason='current_exposure_50'
                elif logical_quantity*unit>intent['margin_budget']:reason='logical_batch_5'
                elif total>equity*D('.9'):reason='projected_margin_90'
                elif after_exposure.get(product,D(0))>equity*D('.5'):reason='projected_exposure_50'
            elif leg.effect=='open' and total>max(current_total,max(equity,D(0))*D('.9')):
                reason='protective_margin_increase'
            return reason,equity,total,after_exposure.get(product,D(0)),logical_quantity,fill.fee
        maximum=candidate.instruction.quantity
        lower,upper=0,maximum
        while lower<upper:
            q=(lower+upper+1)//2
            if evaluate(q)[0]=='accepted':lower=q
            else:upper=q-1
        allowed=lower
        result=evaluate(allowed) if allowed else evaluate(1)
        requested_result=evaluate(maximum)
        row=dict(time=str(ctx.time),frame_time=str(ctx.time if ctx.phase=='auction' else ctx.time+timedelta(minutes=1)),
                 phase=ctx.phase,instrument=inst.id,group=leg.group,intent_id=batch.get('intent_id'),
                 effect=leg.effect,side=leg.side,increasing=increasing,requested=maximum,allowed=allowed,
                 reason=requested_result[0] if allowed<maximum else 'accepted',equity_before=ctx.account.equity,
                 margin_before=current_total,exposure_before=exposure.get(product,D(0)),
                 equity_after=result[1] if allowed else ctx.account.equity,
                 margin_after=result[2] if allowed else current_total,
                 exposure_after=result[3] if allowed else exposure.get(product,D(0)),
                 logical_quantity_after=result[4] if allowed else 0,
                 batch_budget=intent['margin_budget'] if intent else D(0),mark=ctx.marks[inst.id],
                 price=candidate.price,fee=result[5] if allowed else D(0))
        self.guard_audit.append(row)
        if allowed<maximum:
            self.funding['guard_deferred' if not allowed else 'guard_shrunk']+=1
        return allowed

    def observe_execution(self,ctx,stage):
        _,exposure,margin=self._actual(ctx)
        total=sum(margin.values(),D(0));equity=ctx.account.equity
        assert total==ctx.account.margin
        ratio=total/equity if equity>0 else None
        breached=equity<=0 or total>equity
        row=dict(time=str(ctx.time),stage=stage,phase=ctx.phase,equity=equity,margin=total,usage=ratio,
                 breached=breached,**{p+'_exposure':exposure.get(p,D(0)) for p in PRODUCTS})
        self.funding['observations']+=1
        self.funding['max_ratio']=max(self.funding['max_ratio'],ratio or D(0))
        self.funding['max_gap']=max(self.funding['max_gap'],total-equity)
        if breached:
            self.funding['status']='failed'
            self.funding['breaches']+=1
            self.funding['first_breach']=self.funding['first_breach'] or row
            if self.breach_sink:self.breach_sink(row)
        if self.observation_sink:self.observation_sink(row)
        else:self.observations.append(row)
