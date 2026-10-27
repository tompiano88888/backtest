"""External, lagged allocation research over a frozen ATR reference portfolio."""
import calendar
import csv
from datetime import date
import hashlib
import json
from pathlib import Path
import statistics

from .models import D
from .rbreaker_stops import TradeStops


def three_months_before(day):
    month = day.year*12+day.month-1-3
    year, zero_month = divmod(month,12)
    return date(year,zero_month+1,min(day.day,calendar.monthrange(year,zero_month+1)[1]))


class RollingAllocation:
    def __init__(self,source):
        source = Path(source)
        config = json.loads((source/'config.json').read_text(encoding='utf-8'))
        self.source_config = config
        if not config.get('include_auction') or config.get('stop',{}).get('risk_fraction') != '0.01':
            raise ValueError('Expected frozen auction ATR 1% reference')
        self.config = dict(source=str(source.resolve()),months=3,min_observations=20,
                           score='positive annualized sample Sharpe of daily product net PnL / prior portfolio equity',
                           mix='80% positive score + 20% equal; equal if insufficient history or all scores zero',
                           sizing='risk_fraction * active_product_count * weight; margin cap min(50%,50% * multiplier)',
                           rebalance='daily using strictly earlier completed dates; existing positions not resized',
                           sha256={name:hashlib.sha256((source/name).read_bytes()).hexdigest()
                                   for name in ('config.json','daily.csv','product_daily.csv')})
        def read(name):
            with (source/name).open(encoding='utf-8-sig',newline='') as handle:
                return list(csv.DictReader(handle))
        previous_equity = {}
        previous = D(config['initial_cash'])
        for row in read('daily.csv'):
            previous_equity[row['day']] = previous
            previous = D(row['equity'])
        self.returns = {}
        previous_pnl = {}
        for row in read('product_daily.csv'):
            product, day = row['product'],date.fromisoformat(row['day'])
            pnl = D(row['pnl'])
            change = pnl-previous_pnl.get(product,D(0))
            previous_pnl[product] = pnl
            if row['main']:
                equity = previous_equity[row['day']]
                if equity <= 0:
                    raise ValueError('Nonpositive reference equity')
                self.returns.setdefault(product,[]).append((day,float(change/equity)))
        self.audit = []

    def weights(self,day,products):
        lower = three_months_before(day)
        samples = {p:[r for d,r in self.returns.get(p,[]) if lower<=d<day] for p in products}
        scores = {}
        for p,values in samples.items():
            sigma = statistics.stdev(values) if len(values)>1 else 0
            scores[p] = max(statistics.mean(values)/sigma*252**.5,0) if sigma else 0
        fallback = any(len(v)<20 for v in samples.values()) or sum(scores.values())==0
        equal = D(1)/len(products)
        denominator = sum((D(str(v)) for v in scores.values()),D(0))
        weights = {p:equal if fallback else D('.2')*equal+D('.8')*D(str(scores[p]))/denominator for p in products}
        for p in products:
            self.audit.append(dict(day=day,product=p,window_start=lower,
                                   information_day=max((d for d,r in self.returns.get(p,[]) if lower<=d<day),default=''),
                                   observations=len(samples[p]),positive_sharpe=scores[p],fallback=fallback,
                                   weight=weights[p],risk_multiplier=weights[p]*len(products),
                                   margin_cap=min(D('.5'),D('.5')*weights[p]*len(products))))
        return weights


class WeightedTradeStops(TradeStops):
    def limit(self,code,quantity,ctx,fresh=False):
        fraction = self.risk_fraction
        multiplier = self.multipliers[self.instruments[code].product]
        self.risk_fraction = fraction*multiplier
        try:
            result = super().limit(code,quantity,ctx,fresh)
            if fresh and code not in self.active and code in self.seeds:
                self.seeds[code]['risk_multiplier'] = multiplier
            return result
        finally:
            self.risk_fraction = fraction
