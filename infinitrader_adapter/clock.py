"""CFFEX daytime clock; complete bars only, no fills or forward-filled bars."""
from datetime import datetime, time, timedelta
from backtest.models import Bar, D


class MinuteClock:
    def __init__(self):
        self.pending={};self.last_tick={};self.total_volume={};self.day=None
        self.closed_until=None

    def tick(self,code,stamp,day,price,total_volume):
        if self.day!=day:
            if self.day and day<self.day:raise ValueError('Trading day reversed')
            self.pending.clear();self.total_volume.clear();self.last_tick.clear();self.closed_until=None;self.day=day
        if stamp.date()!=day:raise ValueError('Unexpected non-daytime CFFEX timestamp')
        if code in self.last_tick and stamp<self.last_tick[code]:
            raise ValueError('Out-of-order tick')
        self.last_tick[code]=stamp
        old=self.total_volume.get(code,total_volume)
        if total_volume<old:raise ValueError('Cumulative volume moved backwards')
        self.total_volume[code]=total_volume
        if not (time(9,30)<=stamp.time()<=time(11,30) or time(13)<=stamp.time()<=time(15)):
            return
        end=stamp.replace(second=0,microsecond=0)+timedelta(minutes=1)
        if stamp.time() in (time(11,30),time(15)):end=stamp
        if self.closed_until and end<=self.closed_until:raise ValueError('Tick arrived for already completed minute')
        p=D(price)
        if p<=0:raise ValueError('Invalid trade price')
        key=end,code
        if key not in self.pending:self.pending[key]=[p,p,p,p,0]
        b=self.pending[key];b[1]=max(b[1],p);b[2]=min(b[2],p);b[3]=p;b[4]+=total_volume-old

    def pop(self,now,required,delay=2):
        ready=sorted({t for t,c in self.pending if t+timedelta(seconds=delay)<=now})
        result=[]
        for at in ready:
            codes={c for t,c in self.pending if t==at}
            if not set(required)<=codes:
                raise ValueError('Missing completed main-contract minute: '+str(at))
            bars={}
            for code in codes:
                o,h,l,c,v=self.pending.pop((at,code))
                bars[code]=Bar(code,at-timedelta(minutes=1),at,self.day,o,h,l,c,v)
            result.append((at,bars));self.closed_until=at
        return result
