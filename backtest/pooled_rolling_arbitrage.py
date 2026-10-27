"""One research Fund, ranked repeated one-lot hedges, T+1 released-budget lock."""
from bisect import bisect_left,bisect_right
from collections import defaultdict
from datetime import date,timedelta
from dataclasses import asdict
from pathlib import Path
import csv
import hashlib
import json
import shutil
from copy import copy

from .accounting import Ledger
from .arbitrage_capital_returns import annualized
from .arbitrage_repo_returns import repo_rate
from .cash_futures_basis import load_spot,PAIRS
from .factor_data import load_research_data
from .factor_research import csv_if,dump
from .factor_simulation import execute
from .front_contract_arbitrage import front_map
from .models import D,Instrument,Asset,Account
from .rolling_arbitrage_optimization import ProductMarket,PRODUCTS
from .research_dividends import DividendBook,load_events,VERSION as DIVIDEND_VERSION

TRIAL_FIELDS=['product','decision_day','window_start','window_end','common_capital','entry_threshold',
              'exit_threshold','score','account_return','arbitrage_pnl','estimated_repo_interest','cycles','open_at_end']


class Portfolio:
    """Real futures Ledger plus explicitly hypothetical, index-priced cash asset cashflows."""
    def __init__(self,instruments,prices,spot,markets,initial='10000000',margin='.15',funding_exit=False,etf=False,cancel_new_openings=False,dividend_events=None,rate_provider=None,min_expected_annual_return=None,minimum_return_basis='exit_target',min_expected_annual_return_by_product=None):
        instruments=dict(instruments)
        self.etf=etf
        if etf:
            for product,(_,code) in PAIRS.items():
                instruments[code]=Instrument(code,Asset.FUND,Account.SECURITY,'SSE',product,D(1),D('.001'),100,sell_delay_days=1)
        self.instruments,self.prices,self.spot,self.markets=instruments,prices,spot,markets
        self.initial,self.margin_rate=D(initial),D(margin)
        self.ledger=Ledger(instruments,self.initial);self.live={};self.completed=[];self.locks=[]
        self.serial=0;self.pending_exits={};self.plans=[];self.logs=[];self.selected=[];self.daily=[]
        self.spot_fills=[];self.repo_total=D(0);self.candidate_history=[];self.allocation_audit=[]
        self.funding_exit=funding_exit;self.funding_actions=[]
        self.cancel_new_openings=cancel_new_openings;self.cancelled_openings=[];self.proposed_openings=[]
        self.open_checkpoint=None
        self.dividends=DividendBook(dividend_events or [])
        self.rate_provider=rate_provider
        if minimum_return_basis not in ('exit_target','zero_convergence'):
            raise ValueError('Unknown minimum return basis')
        self.minimum_return_basis=minimum_return_basis
        self.min_expected_annual_return=None if min_expected_annual_return is None else D(min_expected_annual_return)
        if self.min_expected_annual_return is not None and not self.min_expected_annual_return.is_finite():
            raise ValueError('Minimum expected annual return must be finite')
        self.min_expected_annual_return_by_product={p:D(v) for p,v in (min_expected_annual_return_by_product or {}).items()}
        if min_expected_annual_return_by_product is not None:
            if self.min_expected_annual_return is not None:
                raise ValueError('Use either a common entry floor or per-product floors')
            if set(self.min_expected_annual_return_by_product)!=set(markets):
                raise ValueError('Per-product entry floors must exactly cover the active products')
            if any(not v.is_finite() or v<0 for v in self.min_expected_annual_return_by_product.values()):
                raise ValueError('Per-product entry floors must be finite and nonnegative')

    def signal_rate(self,product,code,day):
        if self.rate_provider:return self.rate_provider(product,code,day)
        raw=self.prices.get((code,day));s=self.spot.get((PAIRS[product][0],day))
        days=(self.instruments[code].expiry-day).days
        if not raw or not s or days<=0 or raw.get('close') is None or s.get('close') is None:return None
        return (raw['close']-s['close'])/s['close']*D(365)/D(days)

    @staticmethod
    def clone_ledger(ledger):
        """Independent research branch; original Ledger execution logic is unchanged."""
        cloned=copy(ledger)
        cloned.lots=[copy(lot) for lot in ledger.lots]
        cloned.entries=list(ledger.entries);cloned.fills=list(ledger.fills)
        cloned.marks=dict(ledger.marks);cloned.settled=set(ledger.settled);cloned.actions=set(ledger.actions)
        return cloned

    def cancel_lowest_new_opening(self,day):
        today=[c for c in self.live.values() if c['entry_day']==day]
        if not today:return False
        checkpoint=self.open_checkpoint
        if checkpoint is None or checkpoint['day']!=day:raise ValueError('Missing same-day research checkpoint')
        removed=min(today,key=lambda c:(c['expected_gap'],c['product'],c['id']))
        gap_before=max(-self.projected_available(day),D(0))
        kept=[dict(c) for c in today if c['id']!=removed['id']]
        # Re-run only retained plans from the state AFTER ordinary exits, BEFORE new entries.
        # Cancelled tentative trades never enter the published effective fills or accounting.
        self.ledger=self.clone_ledger(checkpoint['ledger'])
        self.live=dict(checkpoint['live'])
        self.spot_fills=self.spot_fills[:checkpoint['spot_count']]
        self.allocation_audit=self.allocation_audit[:checkpoint['audit_count']]
        self.logs=self.logs[:checkpoint['log_count']]
        for c in kept:
            free=self.available(day,'open')
            execute(self.ledger,c['contract'],-1,1,'open',c['id'],day,
                    self.prices[c['contract'],day]['open'],D(0),1)
            self.spot_trade(c,day,'open','open');self.live[c['id']]=c
            purchase=(c['entry_etf']*c['etf_quantity'] if self.etf else
                      c['entry_index']*self.instruments[c['contract']].multiplier)
            required=purchase+self.margin_for(c,day,'open')
            self.allocation_audit.append(dict(day=day,group=c['id'],product=c['product'],signal_day=c['signal_day'],
                expected_gap=c['expected_gap'],available_before=free,required_cash=required,stock_purchase_cash=purchase))
            self.logs.append(dict(day=day,group=c['id'],product=c['product'],reason='retained_opening_after_plan_review',
                                  release_day=None,amount=required))
        self.cancelled_openings.append(dict(day=day,group=removed['id'],product=removed['product'],
            contract=removed['contract'],signal_day=removed['signal_day'],expected_gap=removed['expected_gap'],
            exit_threshold=removed['exit_threshold'],etf_quantity=removed.get('etf_quantity'),
            gap_before=gap_before,gap_after=max(-self.projected_available(day),D(0)),
            action='retrospective_cancel_opening_plan',uses_same_day_close_information=True))
        return True

    def projected_available(self,day):
        variation=sum((lot.side*lot.quantity*self.instruments[lot.instrument].multiplier*
                       (self.prices[lot.instrument,day]['settlement_price']-lot.basis)
                       for lot in self.ledger.lots if self.instruments[lot.instrument].asset==Asset.FUTURE),D(0))
        return self.available(day,'close')+variation

    def resolve_funding(self,day):
        """Research-only same-close liquidation, before the Ledger settlement event."""
        while self.projected_available(day)<0 and self.live:
            if self.cancel_new_openings and self.cancel_lowest_new_opening(day):continue
            candidates=[]
            for c in self.live.values():
                if not self.can_spot_close(c,day,'close'):continue
                f=self.prices.get((c['contract'],day));s=self.spot.get((PAIRS[c['product']][0],day))
                if not f or not s or f.get('close') is None or s.get('close') is None:continue
                days=(self.instruments[c['contract']].expiry-day).days
                # Delivery-day annualization is undefined: leave to normal cash settlement.
                if days<=0:continue
                rate=self.signal_rate(c['product'],c['contract'],day)
                if rate is None:continue
                candidates.append((abs(rate-c['exit_threshold']),c['id'],rate))
            if not candidates:break
            distance,group,rate=min(candidates)
            c=self.live[group];gap_before=-self.projected_available(day)
            f=self.prices[c['contract'],day]
            execute(self.ledger,c['contract'],-1,1,'close',group,day,f['close'],D(0),1)
            price=self.spot_trade(c,day,'close','close')
            c.update(exit_day=day,exit_reason='funding_gap_same_close',exit_index=price,
                     exit_future=f['close']+self.instruments[c['contract']].tick)
            self.completed.append(c);del self.live[group];self.pending_exits.pop(group,None)
            # Emergency proceeds cover today's deficit immediately; no further entries today.
            self.funding_actions.append(dict(day=day,group=group,product=c['product'],
                annualized_rate=rate,exit_threshold=c['exit_threshold'],distance=distance,
                gap_before=gap_before,gap_after=max(-self.projected_available(day),D(0)),
                immediate_liquidity_exception=True))

    def margin_for(self,c,day,phase):
        raw=self.prices.get((c['contract'],day));inst=self.instruments[c['contract']]
        value=raw.get('open' if phase=='open' else 'settlement_price') if raw else None
        if value is None:value=self.ledger.marks.get(c['contract'])
        if value is None:raise ValueError('Missing margin mark')
        return abs(value)*inst.multiplier*self.margin_rate

    def margin(self,day,phase):
        return sum((self.margin_for(c,day,phase) for c in self.live.values()),D(0))

    def available(self,day,phase):
        return self.ledger.cash-self.margin(day,phase)-sum((r['amount'] for r in self.locks),D(0))

    def spot_trade(self,c,day,phase,effect):
        if self.etf:
            code=PAIRS[c['product']][1];raw=self.spot[code,day]
            price=raw[phase]
            execute(self.ledger,code,1,c['etf_quantity'],effect,c['id'],day,price,D(0),0)
            c['entry_etf' if effect=='open' else 'exit_etf']=price
            self.spot_fills.append(dict(day=day,group=c['id'],product=c['product'],instrument=code,
                effect=effect,quantity=c['etf_quantity'],price=price,cash_amount=price*c['etf_quantity'],hypothetical=False))
            return self.spot[PAIRS[c['product']][0],day][phase]
        raw=self.spot.get((PAIRS[c['product']][0],day))
        if raw is None or raw.get(phase) is None:raise ValueError('Missing ideal spot execution quote')
        price=raw[phase];amount=price*self.instruments[c['contract']].multiplier
        self.ledger._cash(-amount if effect=='open' else amount,'ideal_spot_'+effect,PAIRS[c['product']][0],c['id'],day)
        self.spot_fills.append(dict(day=day,group=c['id'],product=c['product'],index_reference=PAIRS[c['product']][0],
                                   effect=effect,index_units=self.instruments[c['contract']].multiplier,price=price,
                                   cash_amount=amount,hypothetical=True))
        return price

    def can_spot_close(self,c,day,phase):
        if not self.etf:return True
        raw=self.spot.get((PAIRS[c['product']][1],day))
        return bool(raw and raw.get(phase) is not None and raw.get('ohlcv_complete',False)
                    and c['entry_day']<day)

    def lock_release(self,c,day,next_trade,cash_change,margin):
        value=max(cash_change+margin,D(0))
        self.locks.append(dict(group=c['id'],closed_day=day,release_day=next_trade,amount=value))
        self.logs.append(dict(day=day,group=c['id'],product=c['product'],reason='released_budget_locked',
                              release_day=next_trade,amount=value))

    def close_open(self,group,day,next_trade):
        c=self.live[group];f=self.prices.get((c['contract'],day));s=self.spot.get((PAIRS[c['product']][0],day))
        if not f or not s or f.get('open') is None or s.get('open') is None or not self.can_spot_close(c,day,'open'):
            self.logs.append(dict(day=day,group=group,product=c['product'],reason='exit_missing_open',release_day=None,amount=None));return
        before=self.ledger.cash;reserve=self.margin_for(c,day,'open')
        execute(self.ledger,c['contract'],-1,1,'close',group,day,f['open'],D(0),1)
        price=self.spot_trade(c,day,'open','close')
        c.update(exit_day=day,exit_reason='frozen_exit_threshold',exit_index=price,exit_future=f['open']+self.instruments[c['contract']].tick)
        self.lock_release(c,day,next_trade,self.ledger.cash-before,reserve)
        self.completed.append(c);del self.live[group];del self.pending_exits[group]

    def allocate_open(self,day,next_trade,max_new_openings=None):
        if self.cancel_new_openings:
            self.open_checkpoint=dict(day=day,ledger=self.clone_ledger(self.ledger),live=dict(self.live),
                spot_count=len(self.spot_fills),audit_count=len(self.allocation_audit),log_count=len(self.logs))
        eligible=[]
        for plan in self.plans:
            m=self.markets[plan['product']];i=bisect_left(m.dates,day);code=plan['contract']
            if i>=len(m.dates) or m.dates[i]!=day or m.front[i]!=code or day>=self.instruments[code].expiry:continue
            if not m.eligible_open(code,i):continue
            eligible.append(plan)
        eligible.sort(key=lambda r:(-r['expected_gap'],r['product']))
        new_openings=0
        while eligible:
            if max_new_openings is not None and new_openings>=max_new_openings:break
            free=self.available(day,'open');chosen=None
            for p in eligible:
                inst=self.instruments[p['contract']]
                f=self.prices[p['contract'],day]['open'];s=self.spot[PAIRS[p['product']][0],day]['open']
                required=inst.multiplier*(s+f*self.margin_rate)
                if self.etf:
                    quote=self.spot.get((PAIRS[p['product']][1],day))
                    if not quote or not quote.get('ohlcv_complete') or quote.get('open') is None or quote['open']<=0:continue
                    quantity=int(s*inst.multiplier/quote['open']/100)*100
                    if quantity<=0:continue
                    required=quantity*quote['open']+inst.multiplier*f*self.margin_rate
                if free>=required:chosen=p;break
            if chosen is None:
                self.logs.append(dict(day=day,group='',product='',reason='no_affordable_candidate',release_day=None,amount=free));break
            self.serial+=1;group=f"{chosen['product']}_{self.serial}"
            c=dict(id=group,product=chosen['product'],contract=chosen['contract'],signal_day=chosen['signal_day'],entry_day=day,
                   entry_threshold=chosen['entry_threshold'],exit_threshold=chosen['exit_threshold'],entry_rate=chosen['rate'],
                   expected_gap=chosen['expected_gap'],entry_index=None,entry_future=None,exit_day=None,exit_reason=None,
                   exit_index=None,exit_future=None,pnl=None)
            raw=self.prices[c['contract'],day]
            if self.etf:
                c.update(etf=PAIRS[c['product']][1],etf_quantity=quantity,entry_etf=None,exit_etf=None)
            execute(self.ledger,c['contract'],-1,1,'open',group,day,raw['open'],D(0),1)
            c['entry_future']=raw['open']-self.instruments[c['contract']].tick
            c['entry_index']=self.spot_trade(c,day,'open','open')
            self.live[group]=c
            new_openings+=1
            if self.cancel_new_openings:self.proposed_openings.append(dict(c))
            self.allocation_audit.append(dict(day=day,group=group,product=c['product'],signal_day=c['signal_day'],
                                             expected_gap=c['expected_gap'],available_before=free,required_cash=required,
                                             stock_purchase_cash=(c['entry_etf']*c['etf_quantity'] if self.etf else c['entry_index']*self.instruments[c['contract']].multiplier)))
            self.logs.append(dict(day=day,group=group,product=c['product'],reason='allocated_highest_affordable_gap',release_day=None,amount=required))
        self.plans=[]

    def settle(self,day,next_trade):
        for code in sorted({p.instrument for p in self.ledger.lots if self.instruments[p.instrument].asset==Asset.FUTURE}):
            raw=self.prices.get((code,day));inst=self.instruments[code]
            if not raw or raw.get('settlement_price') is None:raise ValueError('Missing held futures settlement')
            due=day==inst.delivery
            relevant=[c for c in self.live.values() if c['contract']==code]
            if due and self.etf and any(not self.can_spot_close(c,day,'close') for c in relevant):
                raise ValueError('ETF delivery exit missing quote or not closeable; retain source data, do not assume fill')
            first=len(self.ledger.entries)
            self.ledger.settle(code,day,raw['settlement_price'])
            if due:
                new_entries=self.ledger.entries[first:]
                for c in relevant:
                    delta=sum((D(e['amount']) for e in new_entries if e['group']==c['id']),D(0))
                    before=self.ledger.cash;reserve=raw['settlement_price']*inst.multiplier*self.margin_rate
                    price=self.spot_trade(c,day,'close','close')
                    c.update(exit_day=day,exit_reason='delivery',exit_index=price,exit_future=raw['settlement_price'])
                    self.lock_release(c,day,next_trade,self.ledger.cash-before+delta,reserve)
                    self.completed.append(c);del self.live[c['id']];self.pending_exits.pop(c['id'],None)

    def decide(self,day,writer):
        for c in self.live.values():
            code=c['contract'];raw=self.prices.get((code,day));s=self.spot.get((PAIRS[c['product']][0],day))
            y=self.signal_rate(c['product'],code,day)
            if y is not None and y<c['exit_threshold'] and c['id'] not in self.pending_exits:self.pending_exits[c['id']]=day
        for product,m in self.markets.items():
            i=bisect_left(m.dates,day)
            if i>=len(m.dates) or m.dates[i]!=day:continue
            if day< m.dates[0].replace(year=m.dates[0].year+1):continue
            code=m.front[i]
            if day>=self.instruments[code].expiry:continue
            choice=m.select(i,writer);self.selected.append(choice)
            y=m.rates[i]
            if y is not None and y>choice['entry_threshold']:
                floor_return=y if self.minimum_return_basis=='zero_convergence' else y-choice['exit_threshold']
                minimum=self.min_expected_annual_return_by_product.get(product,self.min_expected_annual_return)
                if minimum is not None and floor_return<minimum:
                    self.logs.append(dict(day=day,group='',product=product,reason='expected_annual_return_below_minimum',release_day=None,amount=floor_return))
                    continue
                plan=dict(product=product,contract=code,signal_day=day,rate=y,
                                       entry_threshold=choice['entry_threshold'],exit_threshold=choice['exit_threshold'],
                                       expected_gap=y-choice['exit_threshold'])
                self.plans.append(plan);self.candidate_history.append(dict(plan))

    def record(self,day,is_market,single_product_reporting=False):
        reserve=self.margin(day,'close');locked=sum((r['amount'] for r in self.locks),D(0))
        unused=self.ledger.cash-reserve-locked
        interest=(max(unused,D(0))*repo_rate(day)/365).quantize(D('.00000001'))
        self.ledger._cash(interest,'repo_interest',None,'REPO',day);self.repo_total+=interest
        spot_value=sum((self.spot[PAIRS[c['product']][0],day]['close']*self.instruments[c['contract']].multiplier
                        if (PAIRS[c['product']][0],day) in self.spot else c['last_spot_value'] for c in self.live.values()),D(0))
        for c in self.live.values():
            raw=self.spot.get((PAIRS[c['product']][0],day))
            if raw:c['last_spot_value']=raw['close']*self.instruments[c['contract']].multiplier
        if self.etf:
            spot_value=D(0)
            for c in self.live.values():
                code=c['etf'];raw=self.spot.get((code,day))
                if raw and raw.get('close') is not None:self.ledger.marks[code]=raw['close']
                elif is_market:raise ValueError(f'Missing held ETF close: {code} {day}')
                c['last_spot_value']=self.ledger.marks[code]*c['etf_quantity']
                spot_value+=c['last_spot_value']
        receivables=self.dividends.receivables()
        equity=self.ledger.cash+spot_value+sum(receivables.values(),D(0))
        product_pnl={p:D(0) for p in PRODUCTS}
        if single_product_reporting:
            # Read-only report shortcut: a single product owns all non-repo PnL.
            # Actual cash, fills, marks, settlements and dividend books are unchanged.
            if len(self.markets)!=1 or not self.etf:
                raise ValueError('Single-product reporting requires one actual ETF product')
            product_pnl[next(iter(self.markets))]=equity-self.initial-self.repo_total
        else:
            attr=self.ledger.group_pnl()
            for k,v in attr.items():
                if k!='REPO':product_pnl[k.split('_')[0]]+=v
            for group,amount in receivables.items():product_pnl[group.split('_')[0]]+=amount
            if not self.etf:
                for c in self.live.values():product_pnl[c['product']]+=c['last_spot_value']
        assert sum(product_pnl.values(),D(0))+self.repo_total==equity-self.initial
        self.daily.append(dict(day=day,is_market_day=is_market,cash=self.ledger.cash,ETF_proxy_value=spot_value,margin=reserve,
                               locked_T1=locked,repo_base=max(unused,D(0)),funding_gap=max(-unused,D(0)),repo_interest=interest,
                               cash_deficit=max(-self.ledger.cash,D(0)),
                               cumulative_repo=self.repo_total,equity=equity,positions=len(self.live),
                               dividend_receivable=sum(receivables.values(),D(0)),cumulative_dividends=self.dividends.total,
                               **{f'pnl_{p}':v for p,v in product_pnl.items()}))


def replay_history(portfolio,dates,begin,end,writer,progress=None,single_product_reporting=False):
    """Shared scheduler for the existing legacy daily research and its parameter scans."""
    day=begin;market_set=set(dates)
    while day<=end:
        observed=day in market_set
        portfolio.dividends.process(day,portfolio.ledger)
        if observed:
            j=bisect_right(dates,day);next_trade=dates[j] if j<len(dates) else None
            portfolio.locks=[r for r in portfolio.locks if r['release_day'] is None or r['release_day']>day]
            for group in list(portfolio.pending_exits):portfolio.close_open(group,day,next_trade)
            portfolio.allocate_open(day,next_trade)
            if portfolio.funding_exit:portfolio.resolve_funding(day)
            portfolio.settle(day,next_trade)
            portfolio.decide(day,writer)
            if progress and day.month==1 and day.day<6:progress(day,'cash',portfolio.ledger.cash,'positions',len(portfolio.live))
        portfolio.dividends.capture(day,portfolio.ledger)
        portfolio.dividends.process(day,portfolio.ledger)
        portfolio.record(day,observed,single_product_reporting=single_product_reporting)
        day+=timedelta(days=1)


def load_source_snapshots(rolling_source=None):
    """A frozen replay carries its own provenance, independent of retired outputs."""
    if rolling_source is not None:
        config=json.loads((Path(rolling_source)/'config.json').read_text(encoding='utf-8'))
        parent=config['source_snapshots']
    else:
        parent=json.loads(Path('outputs/front_contract_cash_arbitrage/config.json').read_text(encoding='utf-8'))
    for key in ('daily_version','metadata_version','spot_version'):
        if not isinstance(parent.get(key),str) or not parent[key]:
            raise ValueError(f'Missing fixed source snapshot: {key}')
    return parent


def run(output='outputs/pooled_rolling_arbitrage',initial='10000000',margin='.15',funding_exit=False,etf=False,cancel_new_openings=False,dividends=False,dividend_version=DIVIDEND_VERSION,products=None,nav_adjustment=False,etf_scoring=False,min_expected_annual_return=None,minimum_return_basis='exit_target',min_expected_annual_return_by_product=None,rolling_source=None,dividend_season_adjustment=False,total_return_version=None,dividend_season_months=None):
    if rolling_source is not None and Path(output).resolve()==Path(rolling_source).resolve():
        raise ValueError('Output must not overwrite the frozen rolling source')
    products=tuple(PRODUCTS if products is None else products)
    if not products or len(set(products))!=len(products) or any(p not in PRODUCTS for p in products):
        raise ValueError('Products must be a nonempty unique subset of IF/IH/IC/IM')
    out=Path(output);out.mkdir(parents=True,exist_ok=True)
    parent=load_source_snapshots(rolling_source)
    _,_,raw,facts,instruments=load_research_data('data/index_futures',parent['daily_version'],parent['metadata_version'])
    _,spot_rows=load_spot(version=parent['spot_version'])
    prices={(r['instrument'],r['trading_date']):r for r in raw};spot={(r['instrument'],r['trading_date']):r for r in spot_rows}
    calendars={p:sorted({r['trading_date'] for r in raw if r['product']==p}) for p in PRODUCTS}
    first={c:date.fromisoformat(f['first_trade_date']) for c,f in facts.items()};mapping=front_map(instruments,first,calendars)
    if dividend_season_months is not None and not dividend_season_adjustment:
        raise ValueError('Dividend forecast months require dividend season adjustment')
    if dividend_season_adjustment and (nav_adjustment or not etf):
        raise ValueError('Dividend season signal requires ETF and excludes NAV adjustment')
    season_signal=None
    if dividend_season_adjustment:
        from .dividend_season_signal import DividendSeasonSignal,load_total_return,VERSION
        total_return_version=total_return_version or VERSION
        dividend_season_months=None if dividend_season_months is None else sorted(dividend_season_months)
        season_signal=DividendSeasonSignal(prices,spot,instruments,load_total_return(total_return_version),active_months=dividend_season_months)
    nav_signal=None
    if nav_adjustment:
        if not etf:raise ValueError('NAV signal requires actual ETF leg')
        from .nav_arbitrage_signal import NAVSignal,load_nav
        nav_signal=NAVSignal(prices,spot,instruments,load_nav())
    provider=season_signal.rate if season_signal else nav_signal.rate if nav_signal else None
    events=load_events(dividend_version) if dividends else []
    if etf_scoring:
        if not etf:raise ValueError('ETF scoring requires actual ETF leg')
        from .etf_candidate_scoring import ETFCandidateMarket
        markets={p:ETFCandidateMarket(p,calendars[p],mapping,prices,spot,instruments,provider,
                    dividend_events=events,margin=margin) for p in products}
    else:markets={p:ProductMarket(p,calendars[p],mapping,prices,spot,instruments,provider) for p in products}
    rolling_manifest=None
    if rolling_source is not None:
        source=Path(rolling_source)
        prior=json.loads((source/'config.json').read_text(encoding='utf-8'))
        if (prior.get('dividend_season_adjustment',False)!=dividend_season_adjustment
            or prior.get('dividend_season_months')!=dividend_season_months
            or (dividend_season_adjustment and prior.get('total_return_version')!=total_return_version)
            or prior['source_snapshots']!=parent or prior['nav_adjustment']!=nav_adjustment
            or prior.get('etf_scoring',False)!=etf_scoring or prior['dividends']!=dividends
            or prior['dividend_version']!=(dividend_version if dividends else None)
            or set(prior['products'])!=set(products) or D(prior['margin_rate'])!=D(margin)
            or prior['actual_etf_leg']!=etf or prior['funding_exit']!=funding_exit
            or prior['cancel_new_openings']!=cancel_new_openings
            or (nav_adjustment and prior['nav_version']!='6feaff33a4ac4320832a06e488b1794a')):
            raise ValueError('Frozen rolling source settings or snapshots do not match')
        choices={}
        with (source/'selections.csv').open(encoding='utf-8-sig',newline='') as handle:
            for row in csv.DictReader(handle):
                key=row['product'],date.fromisoformat(row['decision_day'])
                if key in choices:raise ValueError('Duplicate frozen rolling choice')
                choices[key]=dict(row,entry_threshold=D(row['entry_threshold']),exit_threshold=D(row['exit_threshold']))
        def cached_select(product,market):
            def select(i,writer):
                return dict(choices[product,market.dates[i]])
            return select
        for product,market in markets.items():market.select=cached_select(product,market)
        rolling_manifest=dict(directory=source.as_posix(),files={name:hashlib.sha256((source/name).read_bytes()).hexdigest()
            for name in ('config.json','selections.csv','all_trials.csv')})
    dates=sorted({d for ds in calendars.values() for d in ds});begin=date(2020,1,1);end=dates[-1]
    if cancel_new_openings and not funding_exit:raise ValueError('Opening cancellation requires funding_exit')
    if dividends and not etf:raise ValueError('Dividends require actual ETF leg')
    events=load_events(dividend_version) if dividends else []
    portfolio=Portfolio(instruments,prices,spot,markets,initial,margin,funding_exit,etf,cancel_new_openings,events,provider,min_expected_annual_return,minimum_return_basis,min_expected_annual_return_by_product)
    with (out/'all_trials.csv').open('w',newline='',encoding='utf-8-sig') as handle:
        writer=csv.DictWriter(handle,fieldnames=TRIAL_FIELDS);writer.writeheader()
        replay_history(portfolio,dates,begin,end,writer,lambda *args:print(*args,flush=True))
    if rolling_manifest:shutil.copyfile(Path(rolling_source)/'all_trials.csv',out/'all_trials.csv')
    attr=portfolio.ledger.group_pnl();all_cycles=[]
    for c in portfolio.completed+list(portfolio.live.values()):
        row={k:v for k,v in c.items() if k!='last_spot_value'}
        row['pnl']=attr[c['id']]+portfolio.dividends.receivables().get(c['id'],D(0))+(c.get('last_spot_value',D(0)) if c['id'] in portfolio.live and not etf else D(0))
        row['dividend_pnl']=sum((x['amount'] for x in portfolio.dividends.claims if x['group']==c['id'] and x['recognized']),D(0))
        if c['id'] in portfolio.live:row['exit_reason']='open_at_end'
        all_cycles.append(row)
    csv_if(out/'daily.csv',portfolio.daily);csv_if(out/'cycles.csv',all_cycles);csv_if(out/'allocations.csv',portfolio.logs)
    csv_if(out/'selections.csv',portfolio.selected)
    csv_if(out/'future_fills.csv',[dict(day=f.trading_date,**asdict(f.instruction),price=f.price,fee=f.fee) for f in portfolio.ledger.fills if portfolio.instruments[f.instruction.instrument].asset==Asset.FUTURE])
    if etf:csv_if(out/'etf_fills.csv',[dict(day=f.trading_date,**asdict(f.instruction),price=f.price,fee=f.fee) for f in portfolio.ledger.fills if portfolio.instruments[f.instruction.instrument].asset==Asset.FUND])
    csv_if(out/'candidate_plans.csv',portfolio.candidate_history);csv_if(out/'allocation_audit.csv',portfolio.allocation_audit)
    csv_if(out/'funding_actions.csv',portfolio.funding_actions)
    if nav_signal:csv_if(out/'nav_signals.csv',list(nav_signal.audit.values()))
    if dividends:
        csv_if(out/'dividend_events.csv',events);csv_if(out/'dividend_audit.csv',portfolio.dividends.audit)
        csv_if(out/'dividend_claims.csv',portfolio.dividends.claims)
    if cancel_new_openings:
        csv_if(out/'cancelled_openings.csv',portfolio.cancelled_openings)
        csv_if(out/'proposed_openings.csv',portfolio.proposed_openings)
    csv_if(out/('etf_cashflows.csv' if etf else 'ideal_spot_cashflows.csv'),portfolio.spot_fills);csv_if(out/'ledger_entries.csv',portfolio.ledger.entries)
    dump(out/'terminal_locks.json',portfolio.locks);dump(out/'pending_plans.json',portfolio.plans)
    ending=portfolio.daily[-1];yearly=[]
    for year in sorted({r['day'].year for r in portfolio.daily}):
        group=[r for r in portfolio.daily if r['day'].year==year]
        previous=next((r['equity'] for r in reversed(portfolio.daily) if r['day']<group[0]['day']),D(initial))
        repo=sum((r['repo_interest'] for r in group),D(0));profit=group[-1]['equity']-previous
        before_dividend=next((r['cumulative_dividends'] for r in reversed(portfolio.daily) if r['day']<group[0]['day']),D(0))
        yearly.append(dict(year=year,start=group[0]['day'],end=group[-1]['day'],opening_equity=previous,closing_equity=group[-1]['equity'],
                           total_pnl=profit,arbitrage_pnl=profit-repo,repo_interest=repo,dividend_pnl=group[-1]['cumulative_dividends']-before_dividend,actual_return=profit/previous))
    summary=dict(start=begin,end=end,initial_capital=D(initial),final_equity=ending['equity'],total_pnl=ending['equity']-D(initial),
                 repo_interest=portfolio.repo_total,arbitrage_pnl=ending['equity']-D(initial)-portfolio.repo_total,
                 total_return=ending['equity']/D(initial)-1,cagr=annualized(D(initial),ending['equity'],len(portfolio.daily)),
                 entries=len(all_cycles),closed_cycles=len(portfolio.completed),open_at_end=len(portfolio.live),
                 max_simultaneous_positions=max(r['positions'] for r in portfolio.daily),
                 max_funding_gap=max(r['funding_gap'] for r in portfolio.daily),
                 min_cash=min(r['cash'] for r in portfolio.daily),max_cash_deficit=max(r['cash_deficit'] for r in portfolio.daily),
                 budget_gap_calendar_days=sum(r['funding_gap']>0 for r in portfolio.daily),
                 funding_exit_count=len(portfolio.funding_actions),
                 cancelled_opening_count=len(portfolio.cancelled_openings),
                 dividend_pnl=portfolio.dividends.total,dividend_receivable=ending['dividend_receivable'],
                 budget_gap_trading_days=sum(r['is_market_day'] and r['funding_gap']>0 for r in portfolio.daily),
                 **{f'arbitrage_pnl_{p}':ending[f'pnl_{p}'] for p in PRODUCTS})
    csv_if(out/'summary.csv',[summary]);csv_if(out/'yearly.csv',yearly)
    dump(out/'config.json',dict(source_snapshots=parent,products=products,initial=initial,margin_rate=margin,margin_is_research_assumption=True,
         priority='previous completed close actual annualized premium minus selected frozen exit threshold; ties product code',
         allocation='next open: repeat highest affordable valid candidate, multiple independent lots allowed',
         release='cash-leg sale and freed margin plus closing future cashflow locked until next observed trading date; emergency immediate liquidity exception',
         repo='calendar daily positive Ledger cash minus current margin minus T1 locks; ETF purchase already debited from cash',
         selection=('daily independent 35-pair trailing-year ACTUAL ETF Fund return including dividends, residual-cash repo and budget/exit rules; one lot maximum per candidate' if etf_scoring else 'daily independent 35-pair trailing-year model inherited; includes prior fully-flat repo model; actual portfolio residual cash repo is separate'),
         costs=('actual raw ETF execution; future adverse1tick; zero ETF fee/slippage; cash dividends controlled by dividends flag; no share adjustments or complete fees' if etf else 'ideal index cash proxy; future adverse1tick; no complete fees/real ETF tracking/dividends or finance'),
         funding_exit=funding_exit,
         cancel_new_openings=cancel_new_openings,
         dividends=dividends,dividend_version=dividend_version if dividends else None,
         nav_adjustment=nav_adjustment,nav_version='6feaff33a4ac4320832a06e488b1794a' if nav_adjustment else None,
         etf_scoring=etf_scoring,
         dividend_season_adjustment=dividend_season_adjustment,total_return_version=total_return_version,
         dividend_season_months=dividend_season_months,
         dividend_season_model='prior 3 complete years monthly signed TR-minus-price returns, uniform calendar-day allocation; negative forecast unavailable' if dividend_season_adjustment else None,
         min_expected_annual_return=portfolio.min_expected_annual_return,
         frozen_rolling_source=rolling_manifest,
         min_expected_annual_return_by_product=portfolio.min_expected_annual_return_by_product,
         minimum_return_basis=minimum_return_basis,
         minimum_return_rule=('signal close annualized premium converging to zero' if minimum_return_basis=='zero_convergence' else 'signal close annualized premium minus selected exit threshold')+'; inclusive floor; live entry filter only, historical parameter scoring unchanged',
         signal_formula='((F-S)/S+predicted_dividend_ratio)*365/days' if dividend_season_adjustment else '((F-S)/S+(NAV-ETF_close)/NAV)*365/days' if nav_adjustment else '(F-S)/S*365/days',
         candidate_objective='actual ETF one-lot standalone Fund total return with dividends, remaining-cash repo, margin budget, T1 locks and emergency cancellation/exit' if etf_scoring else 'inherited ideal index one-lot return',
         nav_formula='((F-S)/S+(NAV-ETF_close)/NAV)*365/natural_days_to_expiry',
         nav_availability='same-date daily NAV used ex-post; historical publication unverified, not intraday IOPV; missing NAV no signal, no filling',
         dividend_accounting='record-close holdings; ex-date receivable; payment-date gross cash available at day start; sold holdings retain claims; no reinvestment or withholding tax; ex-post metadata known_at not historical PIT',
         cancellation_rule='retrospective same-day plan review using closing/settlement data; remove lowest original expected_gap first; replay retained openings from pre-entry Ledger checkpoint; if no new entries remain, close existing positions by original rule; not a tradable cancellation of already filled ETF',
         actual_etf_leg=etf,etf_quantity='floor(entry index open * futures multiplier / ETF open /100)*100; frozen per group',
         etf_execution='raw ETF open for ordinary trades, close for emergency/delivery; zero ETF fee/slippage unchanged cost baseline; T1 sell constraint; cash dividends controlled by dividends flag; no share adjustments',
         funding_exit_rule='project end-of-day settlement cash; closest absolute annualized-rate distance to own frozen exit; repeat until budget nonnegative; close +/-tick before settlement; emergency cash immediately covers deficit, no further entries today; ordinary exits retain T1 lock',
         source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()))
    if season_signal:
        csv_if(out/'dividend_season_signals.csv',list(season_signal.audit.values()))
        csv_if(out/'index_dividend_contributions.csv',season_signal.contributions)
    plot(out,portfolio.daily,all_cycles,initial=initial)
    lines=['# 四品种统一资金池滚动套利','',
        f'2020-01-01起{D(initial)/10000:g}万元统一现金。只做当月，独立35组滚动一年参数；IM需要自上市后满一年才可参与选择。当前收盘触发条件满足后，候选按当前年化率−选定退出阈值排名，次交易日开盘按当时价格选择最高且可负担的一份，重复至无可负担候选。同品种可多份，每份冻结退出阈值，数量整数。',
        '研究层资金计划限制：现货代理购买全额现金＋期货名义15%保证金。15%为用户估计，不是历史真实规则；不修改Ledger现金准入或扣保证金账务，保证金仅用于研究预算和回购基数。',
        '现货代理为理想指数对冲现金资产，指数本身不注册为交易资产，非真实ETF成交。期货由原Ledger逐日结算；现金代理购销独立现金流和市场价值与统一权益核对。',
        '均值参数改为逐组合冻结阈值退出，收盘判断最早次日开盘，交割按实际最终结算并关闭现货代理。关闭产生回款＋释放保证金＋当次期货平仓现金流统一锁到下一观察交易日，不能当天重用或计回购。周末/休市锁不释放。',
        '每自然日回购本金=max(账户现金−当前15%保证金−T1锁定额,0)，现货购买已从现金扣除，不重复扣其资金；在有持仓/开平仓日期也计真正未使用现金。2020年2%、2021起1.5%，ACT/365，日息1e−8元留存。资金缺口不强平，照实报告，缺口期不计回购或新预算。',
        '选参沿用此前单品种35组、过去一年含完全空仓回购账户收益最大模型；没有用组合未来收益重新选择参数。组合回购使用新增残余现金口径。期货1tick已计，完整手续费、ETF跟踪/股息、融资成本未计，仍是理想研究诊断。', '',
        f"期末权益：{summary['final_equity']:.2f}元；总PnL：{summary['total_pnl']:.2f}元；套利：{summary['arbitrage_pnl']:.2f}元；回购：{summary['repo_interest']:.2f}元；CAGR：{summary['cagr']:.2%}。",'',
        f"现金流限制：最低现金{summary['min_cash']:.2f}元；最大现金缺口{summary['max_cash_deficit']:.2f}元；含保证金及T1锁的最大预算缺口{summary['max_funding_gap']:.2f}元，{summary['budget_gap_calendar_days']}个自然日预算不足。出现缺口不自动借款/缩量/强平；模型仍支付实际期货结算，现金为负如实记录。未计融资成本，因此不能将此CAGR视为{D(initial)/10000:g}万无追加资金可执行收益。",'',
        '|年份|套利PnL（万元）|回购（万元）|总PnL（万元）|实际收益率|','|---|---:|---:|---:|---:|']
    for r in yearly:lines.append(f"|{r['year']}|{r['arbitrage_pnl']/10000:.2f}|{r['repo_interest']/10000:.2f}|{r['total_pnl']/10000:.2f}|{r['actual_return']:.2%}|")
    lines[0]='# '+ '/'.join(products)+'统一资金池滚动套利'
    if funding_exit:
        lines[11]=f"资金检查：最低现金{summary['min_cash']:.2f}元，最大现金缺口{summary['max_cash_deficit']:.2f}元，最大含保证金/T1锁预算缺口{summary['max_funding_gap']:.2f}元。若没有可成交组合仍须照实保留缺口，不能改账或假设融资。"
        lines[5]='普通退出仍次日开盘并锁到下一交易日。资金缺口退出采用用户新增的同收盘研究例外：预估当日结算后的现金减保证金及既有T1锁，缺口时按当前年化率与自身冻结退出阈值的绝对差从小到大退出，平到无缺口或没有可成交组合。成交为收盘±1tick，在当日结算之前入账，不重复结算已平组合。'
        lines[6]='紧急退出回款及释放保证金立即用于补足当日缺口，不新增当日开仓；普通退出保留T1锁。回购按最终正可用现金逐自然日估算。日线收盘信息与当日结算价用于同日流动性预估，不能证明盘中可知或可成交；这是用户指定理想化流动性处理，不是已验证的盘中策略。'
        lines += ['',f"资金退出共{len(portfolio.funding_actions)}份；最终日终缺口天数{summary['budget_gap_calendar_days']}。详情funding_actions.csv。未计完整成本与ETF公司行为，不宣称真实盘中资金始终充足。"]
    lines+=['','2026截至行情末日；每天资金/锁定/保证金/回购明细、每份组合、资金分配、全部候选试验、滚动选择和两腿现金流均输出。首次2020收盘生成计划，次交易日最早入场；月底/末日未平仅估值。']
    if etf:
        lines[3]='研究预算为实际ETF购买金额＋期货名义15%保证金；保证金仅统计预算，不重复扣权益。'
        lines[4]='现货腿替换为IF/510300、IH/510050、IC/510500、IM/512100。数量=向下取整(开仓指数开盘点位×期货乘数÷ETF开盘价÷100)×100，逐组合冻结。ETF以fund注册并通过同一Ledger买卖和估值；指数只生成信号及数量参考，不交易。'
        lines[7]='保持原指数信号、滚动一年选参和35组优化模型不变（选参目标仍为此前理想指数模型收益，不按ETF收益重选）。实际账户PnL改为原始ETF价格盈亏＋期货PnL＋回购。保持成本基线：期货1tick，ETF费用/滑点为0，完整费用和分红/份额调整尚未计入，因此是实际ETF原价诊断，不是完整总回报。ETF实施T1可卖限制，缺报价不假设成交。'
    if cancel_new_openings:
        lines[5]='资金缺口先撤当日预期收益最低的原开仓计划，必要时连续撤多份；当日无剩余新仓时才按原收盘退出规则处理老仓。普通退出仍次日开盘并T1锁；紧急老仓退出回款当天补缺口。'
        lines += ['',f"本轮先撤当日开仓计划：共撤销{len(portfolio.cancelled_openings)}份，实际老仓紧急退出{len(portfolio.funding_actions)}份。预期收益采用原开仓计划的信号日年化升水−冻结退出阈值，最低者先撤；不足继续撤，直到当天无剩余新仓才使用原老仓退出规则。",
            '撤销是事后重排研究计划：使用当日收盘和结算信息，回到普通退出之后、开仓之前的账本检查点，仅重新执行保留计划。撤销计划没有实际成交、费用、T1锁或盈亏，也不在cycles/fills里冒充卖出；原提议及撤销原因分别保存在proposed_openings/cancelled_openings。它不能证明可在真实开盘前决定撤单，含同日信息回看偏差，不是可直接交易的策略。']
    if dividends:
        lines[7]='指数信号、理想指数滚动选参及原执行成本保持不变。实际ETF原价＋现金分红纳入账务，登记日收盘确定权益、除息确认应收、派息才入现金。未到账应收仅计权益，不作保证金/开仓/回购资金，卖出后应收保留。分红金额为元数据每份现金，无红利再投资或税扣假设；未计份额调整和完整费用。'
        lines+=['',f"分红固定快照{dividend_version}，认列分红{portfolio.dividends.total:.2f}元，期末应收{ending['dividend_receivable']:.2f}元；套利PnL已包含分红，不能重复相加。元数据事后用于账务，known_at为查询时间，不冒充历史PIT可知；信号未读取未来分红。"]
    if nav_adjustment:
        lines+=['','本轮重新寻参：信号=((期货收盘−指数收盘)/指数收盘+(ETF单位净值−ETF收盘)/ETF单位净值)×365/剩余自然日。新信号同时用于训练开平、实际入退及老仓紧急退出排序；不只给已选参数事后加一项。原35组、滚动一年、训练最大含回购账户收益及其理想指数PnL评价口径保持，实际账户仍ETF加现金分红。',
            '净值固定快照6feaff33a4ac4320832a06e488b1794a，缺净值不信号、不前填。NAV只决定信号，ETF撮合和估值仍原始市场价。历史净值发布时间未核实，使用同日NAV为事后研究假设，不能宣称开盘执行时已知或PIT验证；缺口撤计划亦保留同日回看假设。nav_signals.csv列出基差、折溢价、日期和合成率。']
    if etf_scoring:
        lines=[line.replace('原35组、滚动一年、训练最大含回购账户收益及其理想指数PnL评价口径保持，实际账户仍ETF加现金分红。',
                            '原35组和滚动一年窗口保持，评分改为实际ETF含分红账户收益。')
                    .replace('保持原指数信号、滚动一年选参和35组优化模型不变（选参目标仍为此前理想指数模型收益，不按ETF收益重选）。',
                             '使用NAV调整信号，滚动一年35组按实际ETF含分红账户收益重新评分。')
                    .replace('指数信号、理想指数滚动选参及原执行成本保持不变。',
                             'NAV调整信号同时用于训练和执行，评分采用实际ETF账户，原执行成本保持。')
               for line in lines]
        lines+=['','本轮评分已改：每个参数候选用新NAV信号，在过去一年独立单品种最多一份组合Fund计算实际ETF100份取整买卖、期货结算与tick、现金分红、未用现金回购、15%预算/T1释放及缺口撤新/平老仓规则。按窗口末实际权益收益最大选择，不再用理想指数PnL。窗口末只估值不强制平仓，未付分红仅计应收，不提前计现金。四品种最终资金竞争仍在独立选参之后进行；单品种评分不等于完整组合全局优化。']
        if not nav_adjustment:
            lines=[line.replace('NAV调整信号','原期货/指数基差信号').replace('新NAV信号','原期货/指数基差信号') for line in lines]
    if season_signal:
        lines=[line.replace('保持原指数信号、滚动一年选参和35组优化模型不变（选参目标仍为此前理想指数模型收益，不按ETF收益重选）。',
                            '分红季调整信号重新进入滚动一年35组选参，评分目标保留原理想指数模型收益。')
                    .replace('指数信号、理想指数滚动选参及原执行成本保持不变。',
                             '分红季调整信号进入历史及实际开平仓，原代理评分目标及执行成本保持。') for line in lines]
        lines+=['','## 分红季信号调整','',
            '预测分红累计月份：'+(','.join(map(str,dividend_season_months)) if dividend_season_months else '全年12个月')+'；其余月份分红贡献设为0，仅累计决策日后至到期区间内的部分。',
            '信号=[(F-S)/S+预测分红收益比例]×365/剩余自然日。此前3个完整年度的月度TR/前TR−P/前P贡献均值，按预测区间各月覆盖自然日分配；不使用本年度实际分红或未来年份。每个训练历史信号按自身日期预测。',
            '月度贡献保留带符号精度噪声，负区间预测或历史不足无信号；原始行情不填充。预测不进入现金/PnL，实际ETF分红账务保持。全收益历史包含回溯及修订，历史发布时间和历史版本PIT未核实。',
            f"全收益版本{total_return_version}。日终预算缺口最大{summary['max_funding_gap']}元、{summary['budget_gap_calendar_days']}个自然日；不隐藏或以补现金修正。"]
    (out/'report.md').write_text('\n'.join(lines),encoding='utf-8');print(summary,flush=True)
    from .performance import export_performance
    export_performance(portfolio.daily,initial,begin,out,title='期现套利',
                       subtitle='滚动参数 · 统一资金池 · 原日频研究执行口径',
                       caveat='全样本选参、原日频回看执行、费用/税/PIT等既有限制保留，不等同统一分钟验收。')
    return summary


def plot(out,daily,cycles,initial='10000000'):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig,axes=plt.subplots(3,1,figsize=(14,10),constrained_layout=True)
    values={r['day']:float(r['equity'])/float(initial) for r in daily}
    axes[0].plot(list(values),list(values.values()),label='Unified account NAV incl estimated repo')
    entries=[r['entry_day'] for r in cycles];axes[0].scatter(entries,[values[d] for d in entries],s=9,marker='^',color='red',label='All entries');axes[0].legend()
    for p in PRODUCTS:axes[1].plot([r['day'] for r in daily],[float(r[f'pnl_{p}'])/10000 for r in daily],label=p)
    axes[1].plot([r['day'] for r in daily],[float(r['cumulative_repo'])/10000 for r in daily],label='Repo');axes[1].set_ylabel('PnL CNY10,000');axes[1].legend()
    for k in ('ETF_proxy_value','margin','locked_T1','repo_base'):
        axes[2].plot([r['day'] for r in daily],[float(r[k])/10000 for r in daily],label=k)
    axes[2].set_ylabel('CNY10,000');axes[2].legend();fig.savefig(out/'portfolio.png',dpi=150);plt.close(fig)
