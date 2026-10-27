"""Keep minute-close stops without using pre-fill minute extremes."""
from collections import defaultdict
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from backtest.models import D
from backtest.rbreaker_stops import TradeStops


class ObservedStops(TradeStops):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.last_change={};self.ticks=defaultdict(list);self.evidence={}

    def quote(self,code,price,at,end):
        self.ticks[code].append((at,end,D(price)))
        # Only current/previous minutes are needed for delayed fill callbacks.
        self.ticks[code]=[t for t in self.ticks[code] if t[0]>=at-timedelta(minutes=2)]
        self._check(code,D(price),at,end)

    def _check(self,code,price,at,end):
        t=self.active.get(code)
        if not t or t['stopped'] or at<datetime.fromisoformat(t['entry_time']):return
        key=(code,t['id'],end)
        if key in self.evidence:return
        qty=sum(q for q,p in t['lots']);average=sum(q*p for q,p in t['lots'])/qty
        threshold=average-t['side']*t['distance'];inst=self.instruments[code]
        touch=price<=threshold if t['side']>0 else price>=threshold
        net=t['realized']-t['fees']+sum(q*t['side']*(price-p)*inst.multiplier for q,p in t['lots'])
        cost=qty*inst.multiplier*(abs(price)*D('.000023')+inst.tick)
        if touch or net-cost<=-t['budget']:
            self.evidence[key]=dict(reason='atr_price' if touch else 'trade_risk_budget',quantity=qty,
                entry_average=average,stop_price=threshold,bar_extreme=price,net_at_extreme=net,
                budget=t['budget'],distance=t['distance'],side=t['side'])

    def fills_now(self,fills,at):
        changed={f.instruction.instrument:f.time for f in fills[self.cursor:]}
        # Parent updates original-entry FIFO risk and cooling; there are no bars
        # here, so receiving a fill alone never triggers an intraminute stop.
        super().observe(SimpleNamespace(time=at,bars={},auctions={},fills_since=lambda n:tuple(fills[n:])))
        for code,stamp in changed.items():
            self.last_change[code]=stamp
            for tick_at,end,price in self.ticks[code]:
                if tick_at>=stamp:self._check(code,price,tick_at,end)

    def observe(self,ctx):
        for key,e in list(self.evidence.items()):
            code,trade_id,end=key
            if end>ctx.time:continue
            self.evidence.pop(key)
            t=self.active.get(code)
            if t and t['id']==trade_id and not t['stopped']:
                t.update(stopped=True,trigger_time=str(ctx.time),trigger_reason=e['reason'])
                self.locked.add((t['product'],ctx.time.date()))
                self.events.append(dict(trade_id=trade_id,instrument=code,product=t['product'],
                    trigger_time=str(ctx.time),bar_close=ctx.bars[code].close if code in ctx.bars else e['bar_extreme'],**e))
        bars=dict(ctx.bars)
        for code,b in list(bars.items()):
            if self.last_change.get(code,b.start)<=b.start:continue
            ticks=[p for at,end,p in self.ticks[code] if end==b.end and at>=self.last_change[code]]
            if ticks:
                bars[code]=replace(b,open=ticks[0],high=max(ticks),low=min(ticks),close=ticks[-1],volume=0)
            else:bars.pop(code)
        return super().observe(SimpleNamespace(**{**vars(ctx),'bars':bars}))
