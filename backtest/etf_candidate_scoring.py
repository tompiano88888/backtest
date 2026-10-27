"""Fixed-threshold one-lot training accounts using the actual ETF Fund rules."""
from bisect import bisect_left
from datetime import timedelta
from .models import D
from .rolling_arbitrage_optimization import ProductMarket
from .arbitrage_repo_returns import repo_rate
from .cash_futures_basis import PAIRS


class ETFCandidateMarket(ProductMarket):
    def __init__(self,*args,dividend_events=(),margin='.15',**kwargs):
        super().__init__(*args,**kwargs)
        if self.rate_provider is None:
            from .cash_carry_signals import annualized_rate
            def original_rate(product,code,day):
                f=self.prices.get((code,day),{});s=self.spot.get((self.index,day),{})
                return annualized_rate(f.get('close'),s.get('close'),
                    (self.instruments[code].expiry-day).days if code else 0)
            self.rate_provider=original_rate
        self.dividend_events=[e for e in dividend_events if e['instrument']==PAIRS[self.product][1]]
        self.margin_rate=margin;self.evaluation_cache={};self.cache_date=None

    def eligible_open(self,code,i):
        if not super().eligible_open(code,i):return False
        r=self.spot.get((PAIRS[self.product][1],self.dates[i]))
        return bool(r and r.get('ohlcv_complete') and r.get('open') is not None and r['open']>0)

    def evaluate(self,begin,asof,entry_threshold,exit_threshold,capital,trace=False):
        # Identical binary signal dates produce identical one-position cashflow accounts.
        if self.cache_date!=(begin,asof,capital):
            self.cache_date=begin,asof,capital;self.evaluation_cache={}
        lower=bisect_left(self.dates,begin)
        key=(tuple(i for i in self.entry_indices[entry_threshold] if lower<=i<=asof),
             tuple(i for i in self.exit_indices[exit_threshold] if lower<=i<=asof))
        if not trace and key in self.evaluation_cache:return dict(self.evaluation_cache[key])
        if not trace:
            result=self.fast_account(begin,asof,entry_threshold,exit_threshold,capital)
            self.evaluation_cache[key]=dict(result)
            return result
        from .pooled_rolling_arbitrage import Portfolio
        provider=lambda product,code,day:self.rate_provider(product,code,day)
        fund=Portfolio(self.instruments,self.prices,self.spot,{self.product:self},capital,
                       self.margin_rate,True,True,True,self.dividend_events,provider)
        pending_entry=None;day=begin;limit=self.dates[asof];i=lower
        while day<=limit:
            fund.dividends.process(day,fund.ledger)
            observed=i<=asof and self.dates[i]==day
            if observed:
                next_trade=self.dates[i+1] if i+1<=asof else None
                fund.locks=[r for r in fund.locks if r['release_day'] is None or r['release_day']>day]
                before=len(fund.completed)
                for group in list(fund.pending_exits):fund.close_open(group,day,next_trade)
                if pending_entry and not fund.live:
                    fund.plans=[pending_entry]
                else:fund.plans=[]
                pending_entry=None
                fund.allocate_open(day,next_trade,max_new_openings=1)
                fund.resolve_funding(day);fund.settle(day,next_trade)
                exited=len(fund.completed)>before
                if fund.live:
                    for c in fund.live.values():
                        rate=fund.signal_rate(self.product,c['contract'],day)
                        if rate is not None and rate<exit_threshold:fund.pending_exits.setdefault(c['id'],day)
                elif not exited:
                    code=self.front[i];rate=self.rates[i]
                    if rate is not None and rate>entry_threshold and day<self.instruments[code].expiry:
                        pending_entry=dict(product=self.product,contract=code,signal_day=day,rate=rate,
                            entry_threshold=entry_threshold,exit_threshold=exit_threshold,expected_gap=rate-exit_threshold)
                for c in fund.live.values():
                    r=self.spot.get((c['etf'],day))
                    if not r or r.get('close') is None:raise ValueError('Missing training ETF valuation')
                    fund.ledger.marks[c['etf']]=r['close']
                i+=1
            fund.dividends.capture(day,fund.ledger);fund.dividends.process(day,fund.ledger)
            if trace:
                fund.record(day,observed)
            else:
                reserve=fund.margin(day,'close')
                locked=sum((r['amount'] for r in fund.locks),D(0))
                unused=max(fund.ledger.cash-reserve-locked,D(0))
                interest=(unused*repo_rate(day)/365).quantize(D('.00000001'))
                fund.ledger._cash(interest,'repo_interest',None,'REPO',day);fund.repo_total+=interest
            day+=timedelta(days=1)
        asset=sum((fund.ledger.marks[c['etf']]*c['etf_quantity'] for c in fund.live.values()),D(0))
        equity=fund.ledger.cash+asset+sum(fund.dividends.receivables().values(),D(0))
        result=dict(account_return=(equity-capital)/capital,
                    arbitrage_pnl=equity-capital-fund.repo_total,estimated_repo_interest=fund.repo_total,
                    cycles=len(fund.completed)+len(fund.live),open_at_end=bool(fund.live))
        if trace:return result,fund
        self.evaluation_cache[key]=dict(result)
        return result

    def fast_account(self,begin,asof,enter,exit_,capital):
        """Decimal one-position recurrence, independently checked against Ledger trace."""
        cash=capital;repo=D(0);claims=[];held=None;locks=[];pending=None;exit_pending=False
        completed=0;day=begin;limit=self.dates[asof];i=bisect_left(self.dates,begin)
        etf_code=PAIRS[self.product][1];margin_rate=D(self.margin_rate)
        events={e['record_date']:e for e in self.dividend_events}
        while day<=limit:
            for c in claims:
                if not c['paid'] and c['payment_date']<=day:cash+=c['amount'];c['paid']=True
            observed=i<=asof and self.dates[i]==day
            if observed:
                next_trade=self.dates[i+1] if i+1<=asof else None
                locks=[x for x in locks if x[1] is None or x[1]>day]
                locked=sum((x[0] for x in locks),D(0));exited=False
                e=self.spot.get((etf_code,day));code=self.front[i]
                if held and exit_pending and self.eligible_open(held['code'],i) and held['day']<day:
                    f=self.prices[held['code'],day];before=cash
                    cash+=(held['basis']-f['open']-held['tick'])*held['multiplier']+held['quantity']*e['open']
                    locks.append((max(cash-before+f['open']*held['multiplier']*margin_rate,D(0)),next_trade))
                    held=None;exit_pending=False;completed+=1;exited=True
                if pending and not held:
                    if code==pending and day<self.instruments[code].expiry and self.eligible_open(code,i):
                        f=self.prices[code,day];inst=self.instruments[code]
                        quantity=int(self.spot[self.index,day]['open']*inst.multiplier/e['open']/100)*100
                        locked=sum((x[0] for x in locks),D(0))
                        cost=quantity*e['open']
                        if quantity>0 and cash-locked>=cost+f['open']*inst.multiplier*margin_rate:
                            cash-=cost
                            held=dict(code=code,day=day,basis=f['open']-inst.tick,multiplier=inst.multiplier,
                                      tick=inst.tick,quantity=quantity,cost=cost)
                    pending=None
                if held:
                    f=self.prices.get((held['code'],day))
                    if not f or f.get('settlement_price') is None:raise ValueError('Missing candidate settlement')
                    settle=f['settlement_price'];mult=held['multiplier']
                    projected=cash+(held['basis']-settle)*mult-settle*mult*margin_rate-sum((x[0] for x in locks),D(0))
                    if projected<0:
                        if held['day']==day:
                            cash+=held['cost'];held=None
                        elif day<self.instruments[held['code']].expiry and e and e.get('ohlcv_complete') and e.get('close') is not None:
                            rate=self.rate_provider(self.product,held['code'],day)
                            if rate is not None:
                                cash+=(held['basis']-f['close']-held['tick'])*mult+held['quantity']*e['close']
                                held=None;exit_pending=False;completed+=1;exited=True
                if held:
                    f=self.prices[held['code'],day];settle=f['settlement_price'];mult=held['multiplier'];before=cash
                    cash+=(held['basis']-settle)*mult;held['basis']=settle
                    held['margin']=settle*mult*margin_rate
                    if day==self.instruments[held['code']].delivery:
                        if not e or not e.get('ohlcv_complete') or e.get('close') is None:raise ValueError('Missing candidate delivery ETF')
                        cash+=held['quantity']*e['close']
                        locks.append((max(cash-before+held['margin'],D(0)),next_trade))
                        held=None;exit_pending=False;completed+=1;exited=True
                if held:
                    rate=self.rate_provider(self.product,held['code'],day)
                    if rate is not None and rate<exit_:exit_pending=True
                    if not e or e.get('close') is None:raise ValueError('Missing candidate ETF mark')
                    held['value']=held['quantity']*e['close']
                elif not exited:
                    rate=self.rates[i]
                    if rate is not None and rate>enter and day<self.instruments[code].expiry:pending=code
                i+=1
            if held and day in events:
                ev=events[day]
                claims.append(dict(amount=held['quantity']*ev['cash_per_unit'],ex_date=ev['exchange_ex_date'],
                                   payment_date=ev['payment_date'],paid=False))
                if ev['payment_date']<=day:cash+=claims[-1]['amount'];claims[-1]['paid']=True
            unused=max(cash-(held['margin'] if held else D(0))-sum((x[0] for x in locks),D(0)),D(0))
            interest=(unused*repo_rate(day)/365).quantize(D('.00000001'));cash+=interest;repo+=interest
            day+=timedelta(days=1)
        receivable=sum((c['amount'] for c in claims if c['ex_date']<=limit and not c['paid']),D(0))
        equity=cash+(held['value'] if held else D(0))+receivable
        return dict(account_return=(equity-capital)/capital,arbitrage_pnl=equity-capital-repo,
                    estimated_repo_interest=repo,cycles=completed+int(held is not None),open_at_end=held is not None)
