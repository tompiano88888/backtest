"""Minute R-Breaker with a shared cash pool and strategy-side margin budgets."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict
from datetime import date, time
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from contextlib import ExitStack

from .accounting import Ledger
from .engine import Engine, ExecutionConfig, Frame
from .factor_data import load_research_data
from .models import D, MarginRule
from .rbreaker import (DAILY, METADATA, MINUTE, MinuteCache, RBreaker, minute_ends,
                       reconcile_fills, select_main, write_csv)
from .storage import json_default

PRODUCTS = ('IF', 'IH', 'IC', 'IM')
FEE = D('.000023')


def margin_usage(positions, marks, instruments, rate):
    """Match Ledger's larger-side futures margin, aggregated across months."""
    sides = defaultdict(lambda: {1:D(0), -1:D(0)})
    for p in positions:
        inst = instruments[p['instrument']]
        sides[inst.product][p['side']] += p['quantity']*abs(marks[inst.id])*inst.multiplier*rate
    return {product:max(amounts.values()) for product,amounts in sides.items()}


def allocate(equity, usage, candidates, instruments, marks, rate=D('.15'), total_cap=D('.9'), product_cap=D('.5'), product_caps=None):
    """Equal budgets for simultaneous requests; pending closes release no funds.

    Candidate values are (direction, requested remaining lots or None for sizing).
    Use only currently visible prices plus adverse tick and fee reservations.
    """
    result = {code:0 for code in candidates}
    occupied = sum(usage.values(), D(0))
    if equity <= 0 or occupied >= equity*total_cap:
        return result
    # Reserve an upper bound on ALL new-entry fees before calculating either cap.
    # This protects a nearly full product even when another product pays fees.
    fee_bound = (equity*total_cap-occupied)*FEE/rate
    budget_equity = equity-fee_bound
    remaining = max(budget_equity*total_cap-occupied,D(0))
    active = set(candidates)
    # Water filling redistributes a product's unused cap without code-order bias.
    budgets = {code:D(0) for code in active}
    while active and remaining > 0:
        share = remaining/len(active)
        spent = D(0)
        exhausted = set()
        for code in sorted(active):
            product = instruments[code].product
            cap = product_cap if product_caps is None else product_caps[product]
            headroom = max(budget_equity*cap-usage.get(product,D(0))-budgets[code],D(0))
            amount = min(share, headroom)
            budgets[code] += amount
            spent += amount
            if amount == headroom:
                exhausted.add(code)
        remaining -= spent
        if not exhausted:
            break
        active -= exhausted
    for code,budget in budgets.items():
        inst = instruments[code]
        # Higher absolute price reserves conservatively for either direction.
        nominal = (abs(marks[code])+inst.tick)*inst.multiplier
        cost = nominal*rate
        requested = candidates[code][1]
        lots = int(budget/cost)//inst.lot*inst.lot
        result[code] = lots if requested is None else min(lots, requested)
    return result


class PortfolioRBreaker:
    def __init__(self, instruments, rate=D('.15'), stop_policy=None):
        self.instruments, self.rate = instruments, rate
        self.strategies = {p:RBreaker() for p in PRODUCTS}
        self.intents = {}  # actual contract -> frozen direction/entry size, awaiting fills
        self.reverse_after_close = {}
        self.audit = []
        self.risk = []
        self.stop_policy = stop_policy

    def begin_day(self, mains, previous):
        self.mains = mains
        for product, code in mains.items():
            self.strategies[product].begin_day(code, previous[code])

    def __call__(self, ctx):
        quantities = defaultdict(int)
        for p in ctx.positions:
            quantities[p['instrument']] += p['side']*p['quantity']
        usage = margin_usage(ctx.positions,ctx.marks,self.instruments,self.rate)
        equity = ctx.account.equity
        directives = {}
        blocked = self.stop_policy.observe(ctx) if self.stop_policy else set()
        for product,code in self.mains.items():
            if product in blocked:
                continue
            strategy = self.strategies[product]
            positions = tuple(p for p in ctx.positions if self.instruments[p['instrument']].product == product)
            local = SimpleNamespace(time=ctx.time,bars=ctx.bars,marks=ctx.marks,account=ctx.account,
                                    auctions=getattr(ctx,'auctions',{}),positions=positions,target=lambda i,q:directives.__setitem__(i,q))
            strategy(local)
        for code in set(quantities)|set(self.intents)|set(self.reverse_after_close):
            if self.instruments[code].product in blocked:
                directives[code] = 0
        # Main changes, reversals and non-breakout day exits all close first.
        closing = set()
        for code,target in directives.items():
            held = quantities[code]
            if target == 0 or held*target < 0:
                self.intents.pop(code,None)
                closing.add(code)
                if target and held*target < 0:
                    self.reverse_after_close[code] = 1 if target>0 else -1
                else:
                    self.reverse_after_close.pop(code,None)
            elif target and held == 0 and code not in self.intents:
                self.intents[code] = dict(side=1 if target>0 else -1, quantity=None)
        for code,side in list(self.reverse_after_close.items()):
            if ctx.time.time() >= time(14,59) or self.mains.get(self.instruments[code].product) != code:
                self.reverse_after_close.pop(code)
            elif not quantities[code]:
                self.intents[code] = dict(side=side, quantity=None)
                self.strategies[self.instruments[code].product].mode[code] = 'reversal'
                self.reverse_after_close.pop(code)
            else:
                closing.add(code)
        candidates = {}
        for code,intent in list(self.intents.items()):
            held = quantities[code]
            if self.mains.get(self.instruments[code].product) != code or code in closing:
                self.intents.pop(code,None)
                continue
            if ctx.time.time() >= time(14,59):
                # Cancel all unfinished entry quantities; retain filled overnight lots.
                ctx.target(code,held)
                self.intents.pop(code,None)
                continue
            requested = None if intent['quantity'] is None else max(intent['quantity']-abs(held),0)
            if requested == 0:
                self.intents.pop(code,None)
                continue
            candidates[code] = (intent['side'],requested)
        sizes = allocate(equity,usage,candidates,self.instruments,ctx.marks,self.rate,
                         product_caps=getattr(self,'product_caps',None))
        if self.stop_policy:
            sizes = {code:self.stop_policy.limit(code,size,ctx,fresh=self.intents[code]['quantity'] is None)
                     for code,size in sizes.items()}
        projected = dict(usage)
        reserve_fees = D(0)
        for code,size in sizes.items():
            product = self.instruments[code].product
            inst = self.instruments[code]
            projected[product] = projected.get(product,D(0))+size*(abs(ctx.marks[code])+inst.tick)*inst.multiplier*self.rate
            reserve_fees += size*(abs(ctx.marks[code])+inst.tick)*inst.multiplier*FEE
        if any(sizes.values()):
            # No future prices used; this validates decision-time reservations.
            assert sum(projected.values(),D(0)) <= (equity-reserve_fees)*D('.9')
            for code,size in sizes.items():
                if size:
                    assert projected[self.instruments[code].product] <= (equity-reserve_fees)*D('.5')
        for code in sorted(closing):
            ctx.target(code,0)
        for code,size in sizes.items():
            intent = self.intents[code]
            held = quantities[code]
            if intent['quantity'] is None:
                if not size:
                    # Entry remains a live signal, retried at the next minute.
                    self.intents.pop(code)
                    continue
                intent['quantity'] = size
            # Replace any pending remainder with the currently affordable amount.
            target = held + intent['side']*size
            ctx.target(code,target)
            self.audit.append(dict(time=str(ctx.time),instrument=code,held=held,target=target,
                                   new_lots=size,equity=equity,total_used=sum(usage.values(),D(0)),
                                   product_used=usage.get(self.instruments[code].product,D(0)),
                                   projected_used=sum(projected.values(),D(0)),fee_reserve=reserve_fees))
            if hasattr(self,'product_caps'):
                self.audit[-1].update(product_cap=self.product_caps[self.instruments[code].product],
                                      projected_product_used=projected[self.instruments[code].product])
        self.risk.append(dict(time=str(ctx.time),equity=equity,margin=sum(usage.values(),D(0)),
                              usage=sum(usage.values(),D(0))/equity if equity>0 else None,
                              **{p+'_usage':usage.get(p,D(0))/equity if equity>0 else None for p in PRODUCTS}))


def run(output='outputs/rbreaker_portfolio',start=date(2020,1,1),end=date(2026,10,8),rate=D('.15'),
        stop_risk=None,atr_window=20,atr_multiple=D(1),include_auction=False,
        batch_margin_fraction=None,batch_stop_fraction=None,rolling_source=None,lock_today=False,
        net_exposure=False,direct_close_control=False,historical_fees=None,avoid_close_today=False):
    import duckdb
    output = Path(output)
    if avoid_close_today and (historical_fees is None or stop_risk is None or not include_auction or
                             batch_margin_fraction is not None or rolling_source is not None or lock_today or net_exposure):
        raise ValueError('Fee-safe routing requires historical fees + ATR risk sizing + auction, without batch/rolling/old locks')
    output.mkdir(parents=True,exist_ok=True)
    daily_version,metadata,raw,facts,instruments = load_research_data('data/index_futures',DAILY,METADATA)
    with duckdb.connect('data/index_futures_minute/metadata.duckdb',read_only=True) as db:
        report = json.loads(db.execute('SELECT report FROM futures_minute_snapshots WHERE version=?',[MINUTE]).fetchone()[0])
    if report['source_daily_version'] != daily_version:
        raise ValueError('Minute/daily version mismatch')
    fee_provider = None
    if historical_fees is not None:
        from .fees import HistoricalFees
        fee_provider = HistoricalFees(**historical_fees)
    config = dict(initial_cash='10000000',start=str(start),end=str(end),margin_rate=str(rate),
                  total_margin_cap='.9',product_margin_cap='.5',fee_rate=str(FEE),slippage_ticks=1,
                  participation='.1',minute_version=MINUTE,daily_version=daily_version,metadata_version=metadata,
                  margin_policy='strategy decision-time caps; no forced deleveraging; pending opens reserved; closes release after fill',
                  allocation='simultaneous entrants share free margin equally, each capped at 50%; entry quantity fixed; no holding pyramids',
                  reversal='close completely, then size new opposite entry at following callback',
                  margin_assumption='constant 15% research assumption, not verified historical schedule',
                  clock_assumption='same as original one-lot experiment; exclude 09:30, vendor label=end')
    config['include_auction'] = include_auction
    if avoid_close_today:
        config['fee_avoidance'] = True
        config['execution_routing'] = 'all closes including unlocks require zero same-contract same-side today fee balance; otherwise opposite open; eligible locked inventory first'
        config['funding_basis'] = 'net exposure 50%, actual physical larger-side margin 90%; pending closes release nothing; execution guard for this trial only'
        config['inventory_cleanup'] = 'obsolete-main inventory exits submitted before next auction; current-main locks retained for future signals'
    if historical_fees is not None:
        config['fee_mode'] = 'historical'
        config['historical_fees'] = historical_fees
        config['strategy_cost_estimate'] = 'unchanged .000023 for sizing and estimated exit costs; actual fills use historical FeeService'
        config['commission_rate'] = '0'
        config['commission_per_unit'] = '0'
    if include_auction:
        config['clock_assumption'] = '09:30 discrete opening auction; 09:31-15:00 continuous end labels'
        config['auction_policy'] = 'pre-existing instructions fill at auction price without slippage; post-result signals next continuous open with slippage'
    config['stop'] = None if stop_risk is None else dict(risk_fraction=str(stop_risk),atr_window=atr_window,
                                                       atr_multiple=str(atr_multiple),cooldown='trigger and stop-fill days',
                                                       trigger='completed minute high/low touches ATR or trade loss budget; next allowed open',
                                                       accounting='FIFO original entries and actual fees, no reset on daily settlement')
    batch_mode=batch_margin_fraction is not None and not lock_today
    if lock_today:
        if not include_auction or batch_margin_fraction is None or batch_stop_fraction is not None or stop_risk is not None or rolling_source is not None:
            raise ValueError('Locks require auction + batch-margin + ATR, without cash/risk/rolling sizing')
        if atr_multiple!=1:
            raise ValueError('Lock trial uses ATR multiple 1')
        config['lock_today']=True
        config['batch_margin_fraction']=str(batch_margin_fraction)
        config['allocation']='5% prior-settled-equity margin batches; reuse yesterday locked inventory before new opens; larger-side reservations include pending opens, no pending-close release'
        config['lock_policy']='only today close replaced by opposite same-contract open; yesterday close normal; main locks persist until directional batch unlock; obsolete main both sides submitted before next auction'
        config['stop']=dict(mode='lock_atr',atr_window=atr_window,atr_multiple=str(atr_multiple),risk_fraction='0',
                            trigger='completed adverse minute/auction touches frozen ATR from net-exposure acquisition price',
                            sizing='5% prior settled equity margin, no 1% risk sizing; stop attempts next permitted event')
    if net_exposure:
        if not lock_today:
            raise ValueError('Net-exposure trial requires the lock-trial scheduler')
        config.update(net_exposure=True,direct_close_control=direct_close_control,
                      allocation='one logical batch per product/observation; total 5% prior settled equity net-margin budget across unlocks and new opens',
                      product_cap_basis='sum abs(net lots per actual contract) times current price, multiplier and margin rate; no cross-month netting',
                      total_cap_basis='actual larger-side margin including locked inventory; 90% decision and execution entry gate',
                      execution_guard=True,funding_policy='no liquidation; retain all passive breaches and continue; any actual >100% fails feasibility')
        if direct_close_control:
            config['close_today_fee_rate']='0.00023'
            config['lock_policy']='matched direct-close control; close today normally at .00023, all other fills .000023'
    elif direct_close_control:
        raise ValueError('Direct control requires net-exposure mode')
    if batch_mode:
        if stop_risk is not None or batch_stop_fraction is None:
            raise ValueError('Batch stop mode requires both fractions, without ATR stop-risk')
        config['batch_margin_fraction']=str(batch_margin_fraction)
        config['allocation']='one batch per product per completed observation while signal persists; no next batch before previous remainder finishes; prior settled equity budget'
        config['stop']=dict(mode='batch_cash',risk_fraction=str(batch_stop_fraction),
                            budget_basis='previous trading-day settled total equity, frozen at batch decision',
                            cooldown='trigger and stop-fill days; stop exits only its own batch',
                            trigger='completed minute adverse extreme/auction price, net loss plus estimated exit costs')
    if rolling_source is not None:
        if batch_mode or stop_risk is None:
            raise ValueError('Rolling allocation requires ATR risk sizing, without batches')
        from .rbreaker_rolling import RollingAllocation
        rolling = RollingAllocation(rolling_source)
        config['rolling_allocation'] = rolling.config
        config['allocation'] = 'daily lagged 3-month reference Sharpe weights scale 1% ATR risk and product margin caps; no holding pyramids or forced resizing'
        for key in ('minute_version','daily_version','metadata_version','margin_rate','fee_rate','slippage_ticks','participation','include_auction'):
            if rolling.source_config[key] != config[key]:
                raise ValueError(f'Rolling reference configuration mismatch: {key}')
    (output/'config.json').write_text(json.dumps(config,indent=2),encoding='utf-8')
    rules = {code:MarginRule('future',rate) for code in instruments}
    engine = Engine(Ledger(instruments,'10000000'),rules,ExecutionConfig(participation=D('.1'),fee_rate=FEE,slippage_ticks=1,
                    close_today_fee_rate=D('.00023') if direct_close_control else None,
                    fee_mode='historical' if historical_fees is not None else 'fixed'),fee_provider=fee_provider)
    stop_policy = None
    if stop_risk is not None:
        from .rbreaker_stops import TradeStops,prior_atr
        stop_policy = TradeStops(instruments,prior_atr(raw,atr_window),stop_risk,atr_multiple)
    callback = PortfolioRBreaker(instruments,rate,stop_policy)
    tracker=stop_policy
    if lock_today:
        from .rbreaker_locks import LockedRBreaker
        from .rbreaker_stops import prior_atr
        callback=LockedRBreaker(instruments,prior_atr(raw,atr_window),rate,batch_margin_fraction)
        tracker=callback
    if avoid_close_today:
        from .rbreaker_fee_avoidance import FeeAvoidingRBreaker
        from .rbreaker_stops import prior_atr
        callback=FeeAvoidingRBreaker(instruments,prior_atr(raw,atr_window),rate,stop_risk,atr_multiple)
        tracker=callback
        stop_policy=callback.stops
        engine.execution_guard=callback.execution_guard
        engine.execution_observer=callback.observe_execution
    if net_exposure:
        from .rbreaker_exposure import NetExposureRBreaker
        callback=NetExposureRBreaker(instruments,prior_atr(raw,atr_window),rate,batch_margin_fraction,direct_close_control)
        tracker=callback
        engine.execution_guard=callback.execution_guard
        engine.execution_observer=callback.observe_execution
    if rolling_source is not None:
        from .rbreaker_rolling import WeightedTradeStops
        stop_policy = WeightedTradeStops(instruments,prior_atr(raw,atr_window),stop_risk,atr_multiple)
        callback = PortfolioRBreaker(instruments,rate,stop_policy)
        tracker = stop_policy
    if batch_mode:
        from .rbreaker_batches import BatchRBreaker
        callback=BatchRBreaker(instruments,rate,batch_margin_fraction,batch_stop_fraction)
        tracker=callback
    caches = {p:MinuteCache('data/index_futures_minute',report) for p in PRODUCTS}
    by_product_day = defaultdict(lambda:defaultdict(list))
    indexed = {}
    for row in raw:
        by_product_day[row['product']][row['trading_date']].append(row)
        indexed[row['instrument'],row['trading_date']] = row
    previous = {}
    for p in PRODUCTS:
        days = sorted(by_product_day[p])
        for a,b in zip(days,days[1:]):
            previous[p,b] = a
    days = sorted({r['trading_date'] for r in raw if start<=r['trading_date']<=end})
    daily,product_daily,mapping,operations = [],[],[],[]
    pnl_by_product = {p:D(0) for p in PRODUCTS}
    frames = fills_seen = 0
    fill_phases = []
    max_usage, max_product_usage = D(0),D(0)
    breach_minutes = 0
    peak,drawdown = D('10000000'),D(0)
    with ExitStack() as report_stack, (output/'minute_risk.csv').open('w',encoding='utf-8',newline='') as risk_handle:
        import csv
        if net_exposure or avoid_close_today:
            def stream(name,fields):
                handle=report_stack.enter_context((output/name).open('w',encoding='utf-8-sig',newline=''))
                writer=csv.DictWriter(handle,fieldnames=fields)
                writer.writeheader()
                return writer.writerow
            observation_fields=['time','stage','phase','equity','margin','usage','breached']+[p+'_exposure' for p in PRODUCTS]
            callback.observation_sink=stream('funding_observations.csv',observation_fields)
            callback.breach_sink=stream('funding_breaches.csv',observation_fields)
            callback.stress_sink=stream('intrabar_stress.csv',['time','equity_lower','margin_upper','ratio_upper','stressed'])
        risk_writer = csv.DictWriter(risk_handle,fieldnames=['time','equity','margin','usage']+[p+'_usage' for p in PRODUCTS])
        risk_writer.writeheader()
        for day in days:
            mains = {p:select_main(by_product_day[p][previous[p,day]],instruments,day) for p in PRODUCTS if (p,day) in previous}
            mains = {p:c for p,c in mains.items() if c}
            if not mains:
                continue
            prior_rows={c:indexed[c,previous[p,day]] for p,c in mains.items()}
            if rolling_source is not None:
                weights = rolling.weights(day,tuple(mains))
                multipliers = {p:weights.get(p,D(0))*len(mains) for p in PRODUCTS}
                stop_policy.multipliers = multipliers
                callback.product_caps = {p:min(D('.5'),D('.5')*multipliers[p]) for p in PRODUCTS}
            if batch_mode or lock_today:
                callback.begin_day(mains,prior_rows,engine.ledger.last_equity)
            else:
                callback.begin_day(mains,prior_rows)
            mapping.extend(dict(day=day,product=p,instrument=c,information_day=previous[p,day]) for p,c in mains.items())
            codes = set(mains.values()) | {lot.instrument for lot in engine.ledger.lots}
            prices = {c:caches[instruments[c].product].day(c,day,daily_open=indexed[c,day]['open'] if include_auction else None) for c in codes}
            previous_pnl = engine.ledger.snapshot(rules).pnl
            continuous_stamps = list(minute_ends(day))
            stamps = ([continuous_stamps[0].replace(hour=9,minute=30)] if include_auction else [])+continuous_stamps
            if lock_today or avoid_close_today:
                # Prior-day OI is known before today's auction: submit obsolete-main
                # exits here, rather than backfilling from the 09:30 auction result.
                stamps.insert(0,continuous_stamps[0].replace(hour=9,minute=25))
            for stamp in stamps:
                auction_event = include_auction and stamp.time()==time(9,30)
                preparing=(lock_today or avoid_close_today) and stamp.time()==time(9,25)
                bars = {} if auction_event or preparing else {c:prices[c][stamp] for c in codes}
                auctions = {c:caches[instruments[c].product].auctions[c] for c in codes} if auction_event else {}
                final = stamp.time()==time(15)
                settlements = tuple((c,day,D(indexed[c,day]['settlement_price'])) for c in codes) if final else ()
                snapshot = engine.step(Frame(stamp,bars,frozenset(codes),frozenset(codes) if final else frozenset(),settlements,auctions=auctions),on_bar=callback)
                fill_phases.extend(['auction' if auction_event else 'continuous']*(len(engine.ledger.fills)-len(fill_phases)))
                frames += 1
                risk = callback.risk.pop()
                risk_writer.writerow(risk)
                if risk['usage'] is not None:
                    max_usage = max(max_usage,risk['usage'])
                    max_product_usage = max(max_product_usage,*(risk[p+'_usage'] for p in PRODUCTS))
                    breach_minutes += risk['usage']>D('.9') or any(risk[p+'_usage']>D('.5') for p in PRODUCTS)
                peak = max(peak,snapshot.equity)
                drawdown = max(drawdown,(peak-snapshot.equity)/peak)
            check = reconcile_fills(engine)
            assert check == snapshot.pnl, (day,check,snapshot.pnl)
            assert snapshot.cash == snapshot.equity
            assert all(lot.basis == D(indexed[lot.instrument,day]['settlement_price']) for lot in engine.ledger.lots)
            # Decimal cash attribution includes every actual contract, obsolete mains too.
            for entry in engine.ledger.entries[fills_seen:]:
                pnl_by_product[instruments[entry['instrument']].product] += D(entry['amount'])
            fills_seen = len(engine.ledger.entries)
            assert sum(pnl_by_product.values(),D(0)) == snapshot.pnl
            for p in PRODUCTS:
                product_daily.append(dict(product=p,day=day,main=mains.get(p,''),pnl=pnl_by_product[p]))
            daily.append(dict(day=day,cash=snapshot.cash,equity=snapshot.equity,pnl=snapshot.pnl,
                              daily_pnl=snapshot.pnl-previous_pnl,margin=snapshot.margin,
                              usage=snapshot.risk_ratio,reconciliation_difference=check-snapshot.pnl,
                              **{p+'_quantity':sum(lot.side*lot.quantity for lot in engine.ledger.lots if instruments[lot.instrument].product==p) for p in PRODUCTS}))
            operations.extend(asdict(op) for op in engine.operations if op.state not in ('pending','unwinding'))
            engine.operations[:] = [op for op in engine.operations if op.state in ('pending','unwinding')]
            engine.snapshots.clear()
            if len(daily)%100 == 0:
                print(f'{day} days={len(daily)} net_pnl={snapshot.pnl} margin_ratio={snapshot.risk_ratio}',flush=True)
    if not daily:
        raise ValueError('No eligible days')
    fees = sum((fill.fee for fill in engine.ledger.fills),D(0))
    slippage = sum((fill.instruction.quantity*instruments[fill.instruction.instrument].tick*instruments[fill.instruction.instrument].multiplier
                    for fill,phase in zip(engine.ledger.fills,fill_phases) if phase=='continuous'),D(0))
    summary = dict(config=config,net_pnl=snapshot.pnl,equity=snapshot.equity,fees=fees,
                   slippage_cost_vs_same_fill_open=slippage,gross_before_fees=snapshot.pnl+fees,
                   product_pnl=pnl_by_product,frames=frames,days=len(daily),fills=len(engine.ledger.fills),
                   max_minute_margin_ratio=max_usage,max_minute_product_margin_ratio=max_product_usage,
                   mark_to_market_cap_breach_minutes=breach_minutes,max_drawdown=drawdown,
                   final_positions=[asdict(lot) for lot in engine.ledger.lots],reconciliation_difference=check-snapshot.pnl,
                   code_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    summary['auction_fills'] = fill_phases.count('auction')
    if historical_fees is not None:
        assert len(engine.fees.details) == len(engine.ledger.fills)
        assert sum((r['total'] for r in engine.fees.details),D(0)) == fees
        summary['fee_provenance'] = historical_fees
        summary['close_today_quantity'] = sum(r['close_today_quantity'] for r in engine.fees.details)
        write_csv(output/'fee_details.csv',[
            {**{k:v for k,v in row.items() if k!='instruction'},**row['instruction']}
            for row in engine.fees.details])
    if tracker:
        trades = tracker.records(engine.ledger.marks)
        logical_pnl=sum((r['net_pnl'] for r in trades),D(0))
        adjustment=callback.inventory_adjustment(engine.ledger.marks) if avoid_close_today else D(0)
        assert logical_pnl+adjustment == snapshot.pnl, (logical_pnl,adjustment,snapshot.pnl)
        write_csv(output/'trades.csv',trades)
        write_csv(output/'stop_events.csv',tracker.events)
        closed = [r for r in trades if r['remaining_quantity']==0]
        summary['stop_statistics'] = dict(trades=len(trades),closed_trades=len(closed),triggered=len(tracker.events),
                                           pending_stops=sum(t['stopped'] for t in tracker.active.values()),
                                           worst_closed_trade=min((r['net_pnl'] for r in closed),default=D(0)),
                                           closed_losses_beyond_budget=sum(r['net_pnl'] < -r['budget'] for r in closed))
        if lock_today:
            summary['stop_statistics'].pop('closed_losses_beyond_budget')
            summary['stop_statistics']['pending_stops']=sum(b['stopped'] and b['state']=='exiting' for b in tracker.active.values())
            summary['lock_statistics']=dict(locked_lots=sum(r['locked_quantity'] for r in trades),
                                            long_lots=sum(r['long_quantity'] for r in trades),short_lots=sum(r['short_quantity'] for r in trades),
                                            acquisition_batches=len(callback.audit),unlocks=sum(r['action']=='unlock' for r in callback.audit),
                                            actual_same_day_closes=0)
            write_csv(output/'lock_actions.csv',callback.actions)
        if net_exposure:
            summary['funding_feasibility']=callback.funding
            summary['net_exposure_statistics']=dict(logical_batches=len(callback.intents),routing_components=len(callback.routing_audit),
                                                    unlock_components=sum(r['action']=='unlock' for r in callback.routing_audit),
                                                    mixed_batches=sum(r['action']=='mixed' for r in callback.audit))
            summary.pop('mark_to_market_cap_breach_minutes',None)
            summary['lock_statistics'].pop('actual_same_day_closes',None)
            write_csv(output/'logical_batches.csv',[{k:v for k,v in i.items() if k!='components'} for i in callback.intents.values()])
            write_csv(output/'routing_audit.csv',callback.routing_audit)
            write_csv(output/'execution_guard.csv',callback.guard_audit)
        if avoid_close_today:
            assert summary['close_today_quantity']==0
            summary['logical_trade_pnl']=logical_pnl
            summary['inventory_cleanup_adjustment']=adjustment
            summary['funding_feasibility']=callback.funding
            summary['routing_statistics']={action:sum(int(r['quantity']) for r in callback.routing_fills if r['action']==action)
                                            for action in ('enter_open','enter_unlock','exit_close','exit_hedge','cleanup')}
            summary['strategy_source_sha256']=hashlib.sha256(Path('backtest/rbreaker_fee_avoidance.py').read_bytes()).hexdigest()
            write_csv(output/'routing_fills.csv',callback.routing_fills)
            write_csv(output/'order_routing.csv',callback.order_routing)
            write_csv(output/'execution_guard.csv',callback.guard_audit)
            write_csv(output/'logical_fills.csv',[dict(time=f.time,trading_date=f.trading_date,**asdict(f.instruction),price=f.price,fee=f.fee) for f in callback.logical_fills])
    write_csv(output/'daily.csv',daily)
    if rolling_source is not None:
        write_csv(output/'rolling_weights.csv',rolling.audit)
    write_csv(output/'product_daily.csv',product_daily)
    write_csv(output/'allocation_audit.csv',callback.audit)
    write_csv(output/'main_mapping.csv',mapping)
    write_csv(output/'cash_entries.csv',engine.ledger.entries)
    write_csv(output/'fills.csv',[dict(time=f.time,trading_date=f.trading_date,**asdict(f.instruction),price=f.price,fee=f.fee,execution_phase=phase)
                                   for f,phase in zip(engine.ledger.fills,fill_phases)])
    (output/'operations.json').write_text(json.dumps(operations+[asdict(op) for op in engine.operations],default=json_default),encoding='utf-8')
    (output/'warnings.json').write_text(json.dumps(engine.warnings,default=json_default),encoding='utf-8')
    (output/'summary.json').write_text(json.dumps(summary,indent=2,default=json_default),encoding='utf-8')
    text = '# R-Breaker 1000万元资金池\n\n'
    text += f'净PnL：{snapshot.pnl:,.2f}元；期末权益：{snapshot.equity:,.2f}元。\n\n'
    text += '|品种|净PnL（元）|\n|---|---:|\n'
    for p in PRODUCTS:
        text += f'|{p}|{pnl_by_product[p]:,.2f}|\n'
    text += f'\n手续费：{fees:,.2f}元；已成交数量对应1tick滑点成本：{slippage:,.2f}元（不能作为重新无滑点回测结果）。\n'
    text += f'\n最大逐分钟回撤：{drawdown:.4%}；{frames}个Frame/{len(daily)}个交易日，独立原始成交价PnL与每日结算账本差异为零。\n'
    text += ('\n按动态权益限制总90%/单品种50%保证金；批次额度按前日结算权益固定，可逐分钟分批加仓；未成交开仓逐分钟重估并预留预算，平仓未成交不释放预算。\n' if batch_mode else
             '\n按动态权益计算保证金预算：总90%、单品种50%；同时信号均分剩余预算，入场不加仓，未成交开仓逐分钟重估并计入预算。反转先平后开，未成交平仓不释放预算。\n')
    text += f'\n实际最高盘中保证金占比：{max_usage:.4%}；单品种最高：{max_product_usage:.4%}。行情变动产生的存量超限分钟数：{breach_minutes}，停止开仓而不强平。\n'
    text += '\n保证金15%沿用显式研究假设，非已验收历史保证金；分钟标签、量额差异和交易状态限制延续原实验。\n'
    text += ('\n实际成交按固定供应商历史费用版本、today_first平今匹配收费，佣金为零；策略定仓及止损预计退出成本仍沿用原万0.23估算，策略设计不变。逐笔拆分见fee_details.csv，费用版本及知识截止见config.json。\n' if historical_fees is not None else
             '\n费用是用户指定的统一万0.23，不冒充历史交易所费率。\n')
    if include_auction:
        text += f"\n本次包含09:30集合竞价，竞价前已有指令可零滑点成交（共{summary['auction_fills']}笔）；竞价公布后新信号只能进入后续连续Bar，仍加不利1tick。成交文件execution_phase区分竞价与连续交易，不能仅凭09:30成交时间判断免滑点。\n"
    if stop_policy:
        text += f'\n止损试验：每笔风险预算{stop_risk:.2%}开仓决策权益，止损距离此前{atr_window}日简单均值ATR×{atr_multiple}；手数同时受风险预算和保证金预算约束。按完整分钟高低价触发，最早下一允许Bar开盘实际撮合，失败持续退出，触发及退出日停止该品种新开仓。\n'
        text += f"\n止损触发{summary['stop_statistics']['triggered']}次，最差已结束交易PnL为{summary['stop_statistics']['worst_closed_trade']:,.2f}元。风险预算不是实际亏损保证。交易按原始成本跨结算跟踪，trades.csv之和与账户PnL精确相等。\n"
    if rolling_source is not None:
        text += '\n本试验每日按前三个自然月固定ATR参考组合分品种日净PnL/前日组合权益评分：80%正夏普＋20%均分，不足20日或全非正则均分。风险预算实际为1%×活跃品种数×权重，品种保证金开仓上限为min(50%,50%×同一倍数)，已有持仓不因调权强平。风险预算和ATR在开仓时冻结。详见rolling_weights.csv及docs/RBREAKER_ROLLING.md。\n'
    if lock_today:
        text += '\n对锁试验：每次信号5%前日结算权益保证金批次，ATR20×1止损，仅平今改反向开仓、平昨正常；锁仓仍计单向大边占用。主力锁仓按信号逐批平反向释放，旧主力09:25提交双边平仓、09:30竞价最早撮合；未成交持续退出。每笔真实成交手续费万0.23。新增开仓和未成交余量按实际双边及在途较大侧核算90%/50%预算；不采用1%风险定仓或滚动分配。详见docs/RBREAKER_LOCKS.md。\n'
    if batch_mode:
        text += f'\n分批试验：每批保证金预算为前一交易日结算后权益×{batch_margin_fraction:.2%}，该批固定止损预算为同一权益×{batch_stop_fraction:.2%}；每个观测点持续信号可加一批，上一批有未成交余量时先完成/重估，不重复新建批次。每批独立group与FIFO原始成本，止损只退出触发批次，品种当天禁开；其他批次按正常信号/日终规则管理。\n'
        text += '\n本模式替代ATR定仓/ATR止损，不用止损距离反推手数。整数手向下取整、末批可受90%/50%限制缩量。每批实际盈亏包含手续费，触发检查含估计退出费用与滑点，实际亏损可能超预算。\n'
    if net_exposure:
        text=f'# R-Breaker净敞口统一预算：{"直接平仓控制组" if direct_close_control else "对锁版"}\n\n'
        text+=f'净PnL {snapshot.pnl:,.2f}元，期末权益 {snapshot.equity:,.2f}元，分钟回撤 {drawdown:.4%}。\n\n'
        text+='每次信号仅一个前日结算权益5%的逻辑净敞口保证金预算，跨容器解锁和新开共享；品种净敞口50%，实际大边资金90%限制新增，决策与真实成交开盘价处双重复核。锁仓计入实际资金，不将解锁豁免。\n\n'
        text+=f'资金可行性：{callback.funding["status"]}；实际观测最高占比 {callback.funding["max_ratio"]:.4%}，超100%或非正权益观测 {callback.funding["breaches"]} 个；不主动减仓，超限照实记录继续。压力上界另报，不视为真实Tick路径。\n\n'
        text+='完整逐笔/日终/归因见CSV，成交复核见execution_guard.csv，资金观测及超限见funding_observations.csv/funding_breaches.csv。正常成交万0.23；直接平仓控制组平今万2.3。数据与历史规则限制保留，原节点不改。\n'
    if avoid_close_today:
        text+='\n本轮在ATR20/1%风险定仓节点之上仅改执行路由：所有平仓（含解锁）若会计平今则改反向开仓；次日优先释放可普通计费的反向锁仓。实际费率由历史FeeService决定，未改引擎费用。当前主力锁仓保留，旧主力次日竞价前安排退出，成交受原约束。\n'
        text+=f'\n实际计平今量为{summary["close_today_quantity"]}；逻辑方向交易PnL {logical_pnl}元，库存清理额外成交及费用调整 {adjustment}元，两者合计与账户PnL相等。资金观测状态{callback.funding["status"]}，最高{callback.funding["max_ratio"]:.4%}，超100%观测{callback.funding["breaches"]}次。\n'
    (output/'report.md').write_text(text,encoding='utf-8')
    from .performance import export_performance
    export_performance(daily,config['initial_cash'],start,output,title='R-Breaker',
                       subtitle='按本次运行配置执行 · 统一资金池',minute_drawdown=drawdown,
                       caveat='费用、滑点与期末估值按本次配置入账；历史PIT及非Tick等研究边界保持。')
    print(f'COMPLETE net_pnl={snapshot.pnl} fees={fees} fills={len(engine.ledger.fills)}',flush=True)
    return summary
