"""Original research guard AND counter guard; unlocks have no exemption."""
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import timedelta
from types import SimpleNamespace
from backtest.models import D, Fill


def actual_margin(positions, marks, contracts, rules):
    sides=defaultdict(lambda: {1:D(0),-1:D(0)})
    for p in positions:
        if not p['quantity']: continue
        c=contracts[p['instrument']]
        sides[c.exchange,c.product][p['side']] += rules[c.id].margin(c,p['quantity'],marks[c.id],p['side'])
    return sum((max(s.values()) for s in sides.values()),D(0))


@dataclass(frozen=True)
class Admission:
    allowed: int
    reason: str
    research_allowed: int
    counter_risk: object = None


class DualAdmission:
    def __init__(self, cap=D('.9'), max_snapshot_age=10):
        if not 0 < D(cap) <= 1: raise ValueError('Invalid counter risk cap')
        self.cap=D(cap); self.max_age=timedelta(seconds=max_snapshot_age)

    def check(self, callback, leg, action, logical_side, price, now, account, positions, marks, rules):
        """Exact fills are unknown: limit-price budget only, rechecked after fills."""
        c=callback.instruments[leg.instrument]
        rule=rules.get(c.id)
        if rule is None or rule.trading_day != now.date() or rule.valid_until < now.date():
            return Admission(0,'missing_or_stale_contract_rules',0)
        if account is None or not timedelta(0) <= now-account.queried_at <= self.max_age:
            return Admission(0,'missing_or_stale_counter_snapshot',0)
        if leg.effect == 'close' and callback.today[now.date(),c.id,leg.side]:
            return Admission(0,'close_today_forbidden',0,account.risk)
        callback.route[leg.group,leg.effect]=(action,logical_side)
        context=SimpleNamespace(time=now,trading_date=now.date(),phase='live_limit',positions=tuple(positions),
            marks=marks,account=SimpleNamespace(equity=account.equity),
            fill_for_quantity=lambda q:Fill(now,now.date(),replace(leg,quantity=q),
                                          price,rule.fee(c,q,price,leg.effect)))
        research=callback.execution_guard(context,Fill(now,now.date(),leg,price,D(0)))
        if not research: return Admission(0,'research_guard',0,account.risk)
        needs_counter_gate = leg.effect == 'open' or action.startswith('enter_')
        if not needs_counter_gate:
            return Admission(research,'risk_reducing_close',research,account.risk)
        if account.risk >= self.cap:
            return Admission(0,'counter_current_risk',research,account.risk)
        before=actual_margin(positions,marks,callback.instruments,rules)
        def fits(q):
            after=[dict(p) for p in positions]; remaining=q
            if leg.effect=='open':after.append(dict(instrument=c.id,side=leg.side,quantity=q,group=leg.group))
            else:
                for p in after:
                    if p['group']==leg.group and p['instrument']==c.id and p['side']==leg.side:
                        used=min(remaining,p['quantity']);p['quantity']-=used;remaining-=used
                if remaining: return False
            # Do not use a pending unlock's projected margin reduction as cash.
            delta=max(actual_margin(after,marks,callback.instruments,rules)-before,D(0))
            if leg.effect=='open':
                # Counter may freeze a full new leg before applying larger-side
                # relief. Do not finance a hedge with relief not yet granted.
                delta=max(delta,rule.margin(c,q,price,leg.side))
            fee=rule.fee(c,q,price,leg.effect)
            conservative_equity=account.equity-fee-q*c.multiplier*abs(price-marks[c.id])
            return (conservative_equity>0 and account.available >= delta+fee and
                account.margin+account.frozen_margin+delta <= conservative_equity*self.cap)
        low,high=0,research
        while low<high:
            mid=(low+high+1)//2
            if fits(mid):low=mid
            else:high=mid-1
        return Admission(low,'accepted' if low==leg.quantity else 'counter_budget_shrunk_or_deferred',research,account.risk)
