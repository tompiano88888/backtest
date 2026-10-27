"""Causal ATR sizing and cross-settlement trade stop observations."""
from collections import defaultdict, deque
from datetime import date

from .models import D


def prior_atr(rows, window=20):
    if type(window) is not int or window < 1:
        raise ValueError('Positive ATR window required')
    by_code = defaultdict(list)
    for row in rows:
        by_code[row['instrument']].append(row)
    result = {}
    for code, records in by_code.items():
        history = deque(maxlen=window)
        previous_close = None
        for row in sorted(records,key=lambda r:r['trading_date']):
            if len(history) == window:
                result[code,row['trading_date']] = sum(history,D(0))/window
            h,l,c = (D(row[k]) for k in ('high','low','close'))
            tr = h-l if previous_close is None else max(h-l,abs(h-previous_close),abs(l-previous_close))
            history.append(tr)
            previous_close = c
    return result


class TradeStops:
    def __init__(self,instruments,atr,risk_fraction=D('.01'),atr_multiple=D(1)):
        if not 0 < risk_fraction < 1 or atr_multiple <= 0:
            raise ValueError('Invalid stop parameters')
        self.instruments,self.atr = instruments,atr
        self.risk_fraction,self.atr_multiple = risk_fraction,atr_multiple
        self.cursor = 0
        self.seeds,self.active = {},{}
        self.completed,self.events = [],[]
        self.locked = set()  # (product, trading date), includes actual stop exit day
        self.counter = 0

    def limit(self,code,quantity,ctx,fresh=False):
        if quantity == 0:
            return 0
        if fresh and code not in self.active:
            atr = self.atr.get((code,ctx.time.date()))
            if atr is None or atr <= 0:
                return 0
            inst = self.instruments[code]
            distance = atr*self.atr_multiple
            # Both entry/exit fees and two adverse ticks included in planned risk.
            fee_price = abs(ctx.marks[code])+distance+inst.tick
            per_lot = inst.multiplier*(distance+2*inst.tick+2*fee_price*D('.000023'))
            budget = ctx.account.equity*self.risk_fraction
            maximum = max(int(budget/per_lot)//inst.lot*inst.lot,0)
            self.seeds[code] = dict(atr=atr,distance=distance,budget=budget,equity=ctx.account.equity,
                                    maximum=maximum,planned_risk_per_lot=per_lot,decision_time=str(ctx.time))
        seed = self.seeds.get(code)
        if seed is None:
            return 0
        used = self.active.get(code,{}).get('opened_quantity',0)
        return min(quantity,max(seed['maximum']-used,0))

    def observe(self,ctx):
        # These are actual Engine fills; basis resets never reset the trade budget.
        fills = ctx.fills_since(self.cursor)
        self.cursor += len(fills)
        for fill in fills:
            leg = fill.instruction
            code = leg.instrument
            inst = self.instruments[code]
            if leg.effect == 'open':
                if code not in self.active:
                    self.counter += 1
                    seed = self.seeds[code]
                    self.active[code] = dict(id=self.counter,instrument=code,product=inst.product,side=leg.side,
                                            entry_time=str(fill.time),exit_time='',opened_quantity=0,
                                            realized=D(0),fees=D(0),lots=[],stopped=False,trigger_time='',
                                            trigger_reason='',**seed)
                trade = self.active[code]
                assert trade['side'] == leg.side
                trade['lots'].append([leg.quantity,fill.price])
                trade['opened_quantity'] += leg.quantity
            else:
                trade = self.active[code]
                remaining = leg.quantity
                while remaining:
                    lot = trade['lots'][0]
                    used = min(lot[0],remaining)
                    trade['realized'] += used*leg.side*inst.multiplier*(fill.price-lot[1])
                    lot[0] -= used
                    remaining -= used
                    if not lot[0]:
                        trade['lots'].pop(0)
                if trade['stopped']:
                    self.locked.add((inst.product,fill.trading_date))
            trade['fees'] += fill.fee
            if not trade['lots']:
                trade['exit_time'] = str(fill.time)
                self.completed.append(trade)
                self.active.pop(code)
                self.seeds.pop(code,None)
        blocked = {p for p,day in self.locked if day == ctx.time.date()}
        for code,trade in self.active.items():
            if trade['stopped']:
                blocked.add(trade['product'])
                continue
            bar = ctx.bars.get(code)
            auction = getattr(ctx,'auctions',{}).get(code)
            if bar is None and auction is None:
                continue
            side = trade['side']
            qty = sum(q for q,_ in trade['lots'])
            average = sum(q*p for q,p in trade['lots'])/qty
            threshold = average-side*trade['distance']
            extreme = (bar.low if side>0 else bar.high) if bar is not None else auction.price
            close = bar.close if bar is not None else auction.price
            trading_day = bar.trading_date if bar is not None else auction.trading_date
            price_touch = extreme<=threshold if side>0 else extreme>=threshold
            net_at_extreme = trade['realized']-trade['fees']+sum(q*side*(extreme-entry)*self.instruments[code].multiplier for q,entry in trade['lots'])
            exit_cost = qty*self.instruments[code].multiplier*(abs(extreme)*D('.000023')+self.instruments[code].tick)
            budget_touch = net_at_extreme-exit_cost<=-trade['budget']
            if price_touch or budget_touch:
                trade['stopped'] = True
                trade['trigger_time'] = str(ctx.time)
                trade['trigger_reason'] = 'atr_price' if price_touch else 'trade_risk_budget'
                self.locked.add((trade['product'],trading_day))
                blocked.add(trade['product'])
                self.events.append(dict(trade_id=trade['id'],instrument=code,product=trade['product'],
                                        trigger_time=str(ctx.time),reason=trade['trigger_reason'],side=side,
                                        quantity=qty,entry_average=average,stop_price=threshold,
                                        bar_extreme=extreme,bar_close=close,net_at_extreme=net_at_extreme,
                                        budget=trade['budget'],distance=trade['distance']))
        return blocked

    def records(self,marks):
        rows = []
        for trade in self.completed+list(self.active.values()):
            code = trade['instrument']
            remaining = sum(q for q,_ in trade['lots'])
            unrealized = sum((q*trade['side']*self.instruments[code].multiplier*(marks[code]-entry)
                              for q,entry in trade['lots']),D(0))
            rows.append({**{k:v for k,v in trade.items() if k!='lots'},'remaining_quantity':remaining,
                         'net_pnl':trade['realized']-trade['fees']+unrealized})
        return sorted(rows,key=lambda r:r['id'])
