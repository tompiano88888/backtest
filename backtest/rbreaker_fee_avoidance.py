"""External execution routing for the ATR/1% R-Breaker checkpoint.

Logical exposure observations come only from actual Engine fills. They are risk
bookkeeping, not a second matching engine or Ledger. All physical inventory,
fees, valuation and settlement remain in the one real Engine/Ledger.
"""
from collections import defaultdict
from types import SimpleNamespace

from .models import D,Fill,Instruction
from .rbreaker import RBreaker
from .rbreaker_portfolio import PortfolioRBreaker,PRODUCTS,FEE,margin_usage
from .rbreaker_stops import TradeStops
from .rbreaker_exposure import exposure_metrics,NetExposureRBreaker


class InventoryAwareSignals(RBreaker):
    def __init__(self,owner,product):
        super().__init__()
        self.owner,self.product=owner,product

    def __call__(self,ctx):
        # Preserve the original old-main-before-new-main rule even when the old
        # inventory is fully locked and hence absent from net exposure positions.
        old={p['instrument'] for p in self.owner.physical_positions
             if self.owner.instruments[p['instrument']].product==self.product and p['instrument']!=self.main}
        represented={p['instrument'] for p in ctx.positions}
        extra=tuple(dict(instrument=c,side=1,quantity=1) for c in old-represented)
        if extra:
            ctx=SimpleNamespace(**{**vars(ctx),'positions':ctx.positions+extra})
        super().__call__(ctx)


class FeeAvoidingRBreaker:
    def __init__(self,instruments,atr,rate=D('.15'),risk_fraction=D('.01'),atr_multiple=D(1)):
        self.instruments,self.rate=instruments,rate
        self.stops=TradeStops(instruments,atr,risk_fraction,atr_multiple)
        self.logic=PortfolioRBreaker(instruments,rate,self.stops)
        self.logic.strategies={p:InventoryAwareSignals(self,p) for p in PRODUCTS}
        self.audit,self.events=self.logic.audit,self.stops.events
        self.active=self.stops.active
        self.cursor=0
        self.logical_fills=[]
        self.logical_lots=defaultdict(list)
        self.today=defaultdict(int)
        self.desired={}
        self.route={}
        self.routing_fills,self.order_routing,self.guard_audit=[],[],[]
        self.submitted={}
        self.physical_positions=()
        self.cleanup_cash=D(0)
        self.cleanup_net=defaultdict(int)
        self.risk=[]
        self.observation_sink=self.breach_sink=None
        self.observations=[]
        self.funding=dict(observations=0,breaches=0,max_ratio=D(0),max_gap=D(0),first_breach=None,
                          status='passed',guard_shrunk=0,guard_deferred=0)

    def begin_day(self,mains,previous):
        self.mains=mains
        self.logic.begin_day(mains,previous)

    @staticmethod
    def group(code,side):
        return f'rbreaker-safe-{code}-'+('long' if side>0 else 'short')

    def observe(self,ctx):
        fills=ctx.fills_since(self.cursor)
        for offset,fill in enumerate(fills):
            leg=fill.instruction
            key=fill.trading_date,leg.instrument,leg.side
            before=self.today[key]
            if leg.effect=='open':self.today[key]+=leg.quantity
            else:
                # Applies to unlocks, direct exits and obsolete-main cleanup.
                assert before==0,('would charge close-today',fill,before)
            action,logical_side=self.route[leg.group,leg.effect]
            logical_effect='open' if action.startswith('enter_') else 'close'
            if action=='cleanup':
                delta=leg.side*leg.quantity*(1 if leg.effect=='open' else -1)
                self.cleanup_net[leg.instrument]+=delta
                self.cleanup_cash-=delta*fill.price*self.instruments[leg.instrument].multiplier+fill.fee
            else:
                event=Fill(fill.time,fill.trading_date,
                           Instruction(leg.instrument,logical_side,leg.quantity,logical_effect),fill.price,fill.fee)
                self.logical_fills.append(event)
                lots=self.logical_lots[leg.instrument,logical_side]
                if logical_effect=='open':lots.append([leg.quantity,fill.price,fill.trading_date])
                else:
                    left=leg.quantity
                    assert left<=sum(q for q,p,d in lots),(fill,action,logical_side,lots,self.routing_fills[-4:],self.order_routing[-6:])
                    while left:
                        used=min(left,lots[0][0]);left-=used;lots[0][0]-=used
                        if not lots[0][0]:lots.pop(0)
            self.routing_fills.append(dict(fill_index=self.cursor+offset,time=str(fill.time),trading_date=fill.trading_date,
                                           instrument=leg.instrument,group=leg.group,physical_side=leg.side,
                                           physical_effect=leg.effect,quantity=leg.quantity,price=fill.price,fee=fill.fee,
                                           action=action,logical_side=logical_side,
                                           logical_effect='' if action=='cleanup' else logical_effect,
                                           today_balance_before=before))
        self.cursor+=len(fills)

    def logical_positions(self):
        return tuple(dict(instrument=code,side=side,quantity=q,entry=p,basis=p,trading_date=day,group='standalone')
                     for (code,side),lots in self.logical_lots.items() for q,p,day in lots if q)

    def inventory_adjustment(self,marks):
        return self.cleanup_cash+sum((q*marks[c]*self.instruments[c].multiplier for c,q in self.cleanup_net.items() if q),D(0))

    def records(self,marks):
        return self.stops.records(marks)

    def _actual(self,ctx):
        return exposure_metrics(ctx.positions,ctx.marks,self.instruments,self.rate)

    observe_execution=NetExposureRBreaker.observe_execution

    def affordable(self,ctx,plan):
        # Pending closes give no advance release. Every pending open is reserved.
        held={(p['instrument'],p['side']):0 for p in ctx.positions}
        for p in ctx.positions:held[p['instrument'],p['side']]+=p['quantity']
        positions=[dict(p) for p in ctx.positions]
        fee=D(0)
        for (code,side),target in plan.items():
            current=held.get((code,side),0)
            extra=max(target-current,0)
            inst=self.instruments[code]
            if extra:positions.append(dict(instrument=code,side=side,quantity=extra))
            fee+=abs(target-current)*(abs(ctx.marks[code])+inst.tick)*inst.multiplier*FEE
        usage=margin_usage(positions,ctx.marks,self.instruments,self.rate)
        return ctx.account.equity>fee and sum(usage.values(),D(0))<=(ctx.account.equity-fee)*D('.9')

    def __call__(self,ctx):
        self.observe(ctx)
        self.physical_positions=ctx.positions
        positions=self.logical_positions()
        held=defaultdict(int)
        for p in ctx.positions:held[p['instrument'],p['side']]+=p['quantity']
        logical=defaultdict(int)
        for p in positions:logical[p['instrument']]+=p['side']*p['quantity']
        actual_net=defaultdict(int)
        for p in ctx.positions:actual_net[p['instrument']]+=p['side']*p['quantity']
        for code in set(actual_net)|set(logical)|set(self.cleanup_net):
            assert actual_net[code]==logical[code]+self.cleanup_net[code]
        local=SimpleNamespace(time=ctx.time,bars=ctx.bars,auctions=ctx.auctions,marks=ctx.marks,
                              account=ctx.account,positions=positions,
                              fills_since=lambda index:tuple(self.logical_fills[index:]),
                              target=lambda code,q:self.desired.__setitem__(code,q))
        self.logic(local)
        self.logic.risk.clear()
        day=ctx.time.date()
        codes=set(logical)|set(self.desired)|{c for c,s in held}
        # Zero inventory may still have a live opening remainder. Both sides
        # must receive targets, so a next-day direct close cancels yesterday's
        # unfilled hedge instead of accidentally executing both instructions.
        for code in codes:
            held[code,1]+=0;held[code,-1]+=0
        plan=dict(held)
        routes={}
        # Exits/old-main cleanup first; then one frozen risk-sized entry per code.
        entrants=[]
        for code in sorted(codes):
            current=logical[code];wanted=self.desired.get(code,current)
            obsolete=code!=self.mains.get(self.instruments[code].product)
            if obsolete:wanted=0
            if current and (not wanted or current*wanted<0 or abs(wanted)<abs(current)):
                side=1 if current>0 else -1
                qty=abs(current)-(abs(wanted) if current*wanted>0 else 0)
                if not self.today[day,code,side]:
                    plan[code,side]=held[code,side]-qty
                    routes[code,side,'close']=('exit_close',side)
                else:
                    plan[code,-side]=held[code,-side]+qty
                    routes[code,-side,'open']=('exit_hedge',side)
            elif not current and obsolete:
                for side in (1,-1):
                    if held[code,side] and not self.today[day,code,side]:
                        plan[code,side]=0;routes[code,side,'close']=('cleanup',0)
            elif wanted and abs(wanted)>abs(current):
                entrants.append((code,1 if wanted>0 else -1,abs(wanted)-abs(current)))
        actual_usage=sum(margin_usage(ctx.positions,ctx.marks,self.instruments,self.rate).values(),D(0))
        for code,side,requested in entrants:
            # The 90% funding gate applies equally to unlocking and new opening.
            if ctx.account.equity<=0 or actual_usage>=ctx.account.equity*D('.9'):continue
            eligible=0 if self.today[day,code,-side] else min(held[code,1],held[code,-1])
            def trial(q):
                result=dict(plan);unlock=min(eligible,q)
                result[code,-side]=held[code,-side]-unlock
                result[code,side]=held[code,side]+q-unlock
                return result,unlock
            low,high=0,requested
            while low<high:
                q=(low+high+1)//2
                if self.affordable(ctx,trial(q)[0]):low=q
                else:high=q-1
            if low:
                plan,unlock=trial(low)
                if unlock:routes[code,-side,'close']=('enter_unlock',side)
                if low>unlock:routes[code,side,'open']=('enter_open',side)
        for (code,side),target in sorted(plan.items()):
            group=self.group(code,side);signed=side*target
            effect='open' if target>held[code,side] else 'close'
            if target!=held[code,side]:
                action,logical_side=routes[code,side,effect]
                self.route[group,effect]=(action,logical_side)
            if self.submitted.get(group)!=signed:
                ctx.target_group(code,signed,group)
                self.submitted[group]=signed
                self.order_routing.append(dict(time=str(ctx.time),instrument=code,side=side,group=group,
                                               held=held[code,side],target=target,
                                               today_balance=self.today[day,code,side],
                                               action=routes.get((code,side,effect),('cancel_or_hold',0))[0]))
        usage=margin_usage(ctx.positions,ctx.marks,self.instruments,self.rate)
        equity=ctx.account.equity
        self.risk.append(dict(time=str(ctx.time),equity=equity,margin=sum(usage.values(),D(0)),
                              usage=sum(usage.values(),D(0))/equity if equity>0 else None,
                              **{p+'_usage':usage.get(p,D(0))/equity if equity>0 else None for p in PRODUCTS}))

    def execution_guard(self,ctx,candidate):
        leg=candidate.instruction;inst=self.instruments[leg.instrument]
        action,side=self.route[leg.group,leg.effect]
        increasing=action.startswith('enter_')
        _,exposure,before=self._actual(ctx)
        total_before=sum(before.values(),D(0))
        def evaluate(q):
            fill=ctx.fill_for_quantity(q)
            delta=leg.side*q*(1 if leg.effect=='open' else -1)
            equity=ctx.account.equity+delta*inst.multiplier*(ctx.marks[inst.id]-fill.price)-fill.fee
            positions=[dict(p) for p in ctx.positions]
            if leg.effect=='open':positions.append(dict(instrument=inst.id,side=leg.side,quantity=q,group=leg.group))
            else:
                left=q
                for p in positions:
                    if p['group']==leg.group and left:
                        used=min(left,p['quantity']);left-=used;p['quantity']-=used
            _,after_exposure,after=self._actual(SimpleNamespace(positions=positions,marks=ctx.marks))
            margin=sum(after.values(),D(0));reason='accepted'
            if leg.effect=='close' and any(p['instrument']==leg.instrument and p['side']==leg.side and p['trading_date']==ctx.trading_date for p in ctx.positions):
                reason='close_today_forbidden'
            elif increasing:
                if equity<=0:reason='nonpositive_equity'
                elif total_before>=ctx.account.equity*D('.9'):reason='current_margin_90'
                elif margin>equity*D('.9'):reason='projected_margin_90'
                elif after_exposure.get(inst.product,D(0))>equity*D('.5'):reason='net_exposure_50'
            elif leg.effect=='open' and margin>max(total_before,max(equity,D(0))*D('.9')):
                reason='protective_margin_increase'
            return reason,equity,margin,fill.fee
        low,high=0,leg.quantity
        while low<high:
            q=(low+high+1)//2
            if evaluate(q)[0]=='accepted':low=q
            else:high=q-1
        reason=evaluate(leg.quantity)[0]
        actual=evaluate(low) if low else ('deferred',ctx.account.equity,total_before,D(0))
        self.guard_audit.append(dict(time=str(ctx.time),phase=ctx.phase,instrument=inst.id,group=leg.group,
                                     effect=leg.effect,side=leg.side,action=action,requested=leg.quantity,allowed=low,
                                     reason=reason,equity_before=ctx.account.equity,margin_before=total_before,
                                     equity_after=actual[1],margin_after=actual[2],fee=actual[3]))
        if low<leg.quantity:self.funding['guard_deferred' if not low else 'guard_shrunk']+=1
        return low
