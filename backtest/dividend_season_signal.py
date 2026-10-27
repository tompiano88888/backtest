"""Historical monthly dividend contribution forecast; no forecast cash booking."""
from calendar import monthrange
from collections import defaultdict
from datetime import date, timedelta

from .cash_futures_basis import PAIRS, load_spot
from .models import D

VERSION = '3f899b4d11604eef86a318e7496f9741'
CODES = {'IF': 'H00300.CSI', 'IH': 'H00016.CSI', 'IC': 'H00905.CSI', 'IM': 'H00852.CSI'}


def contributions(spot, total_rows):
    """Retain signed return differences, including precision noise; reject gaps."""
    totals = {}
    for row in total_rows:
        key = row['instrument'], row['trading_date']
        if key in totals:
            raise ValueError('Duplicate total return date')
        totals[key] = row['close']
    result = []
    for product, code in CODES.items():
        prices = {day: row['close'] for (inst, day), row in spot.items() if inst == PAIRS[product][0]}
        tr = {day: value for (inst, day), value in totals.items() if inst == code}
        if not prices or not tr:
            raise ValueError('Missing price or total return index')
        lower, upper = max(min(prices), min(tr)), min(max(prices), max(tr))
        pdates = {d for d in prices if lower <= d <= upper}
        tdates = {d for d in tr if lower <= d <= upper}
        if pdates != tdates or len(pdates) < 2:
            raise ValueError('Price and total return calendars differ within overlap')
        days = sorted(pdates)
        for previous, day in zip(days, days[1:]):
            values = prices[previous], prices[day], tr[previous], tr[day]
            if any(v is None or not v.is_finite() or v <= 0 for v in values):
                raise ValueError('Invalid index close')
            result.append(dict(product=product, day=day, previous_day=previous,
                               contribution=tr[day]/tr[previous]-prices[day]/prices[previous]))
    return result


class SeasonalForecast:
    """Three prior complete years, month totals spread over calendar days.

    Signed monthly differences are retained. Negative horizon forecasts are
    unavailable, never silently clipped to zero. This is a revised-history
    research estimate, not a claim of historical publication/PIT verification.
    """
    def __init__(self, rows, lookback=3, active_months=None):
        if type(lookback) is not int or lookback < 1:
            raise ValueError('Invalid lookback')
        self.lookback = lookback
        self.active_months = tuple(range(1,13)) if active_months is None else tuple(active_months)
        if (not self.active_months or len(set(self.active_months)) != len(self.active_months)
                or any(type(m) is not int or not 1 <= m <= 12 for m in self.active_months)):
            raise ValueError('Active months must be unique integers in 1..12')
        self.months = defaultdict(lambda: D(0))
        self.bounds = {}
        self.month_keys = set()
        self.cache = {}
        seen = set()
        for row in rows:
            p, day, value = row['product'], row['day'], D(row['contribution'])
            if (p, day) in seen or not value.is_finite():
                raise ValueError('Duplicate or invalid dividend contribution')
            seen.add((p, day))
            self.months[p, day.year, day.month] += value
            self.month_keys.add((p, day.year, day.month))
            key = p, day.year
            lo, hi = self.bounds.get(key, (day, day))
            self.bounds[key] = min(lo, day), max(hi, day)

    def profile(self, product, year):
        key = product, year
        if key not in self.cache:
            years = range(year-self.lookback, year)
            for y in years:
                bounds = self.bounds.get((product, y))
                if (not bounds or bounds[0] > date(y, 1, 10) or bounds[1] < date(y, 12, 20)
                        or any((product, y, m) not in self.month_keys for m in range(1, 13))):
                    self.cache[key] = None
                    return None
            self.cache[key] = {m: sum((self.months[product, y, m] for y in years), D(0))/D(self.lookback)
                               for m in range(1, 13)}
        return self.cache[key]

    def predict(self, product, day, expiry):
        if expiry <= day:
            return None
        cursor = day + timedelta(days=1)
        spans=[]
        while cursor <= expiry:
            n = monthrange(cursor.year, cursor.month)[1]
            finish = min(expiry, date(cursor.year, cursor.month, n))
            if cursor.month in self.active_months:
                spans.append((cursor.month,(finish-cursor).days+1,n))
            cursor = finish + timedelta(days=1)
        if not spans:
            return D(0)
        profile = self.profile(product, day.year)
        if profile is None:
            return None
        value = sum((profile[m]*D(days)/D(n) for m,days,n in spans),D(0))
        return value if value >= 0 else None


class DividendSeasonSignal:
    def __init__(self, prices, spot, instruments, total_rows, active_months=None):
        self.prices, self.spot, self.instruments = prices, spot, instruments
        self.contributions = contributions(spot, total_rows)
        self.forecast = SeasonalForecast(self.contributions,active_months=active_months)
        self.audit = {}

    def rate(self, product, code, day):
        key = product, code, day
        if key in self.audit:
            return self.audit[key]['adjusted_annualized_rate']
        expiry = self.instruments[code].expiry if code else day
        days = (expiry-day).days
        f = self.prices.get((code, day), {}).get('close')
        s = self.spot.get((PAIRS[product][0], day), {}).get('close')
        q = self.forecast.predict(product, day, expiry)
        raw = (f-s)/s*D(365)/D(days) if days > 0 and f is not None and s and s > 0 else None
        shift = q*D(365)/D(days) if q is not None and days > 0 else None
        rate = raw+shift if raw is not None and shift is not None else None
        self.audit[key] = dict(product=product, contract=code, day=day, days_to_expiry=days,
            forecast_year_start=day.year-3, forecast_year_end=day.year-1,
            predicted_dividend_ratio=q, original_annualized_rate=raw,
            annualized_dividend_adjustment=shift, adjusted_annualized_rate=rate,
            issue='' if rate is not None else 'missing_inputs_history_or_negative_forecast')
        return rate


def load_total_return(version=VERSION):
    return load_spot('data/index_total_return', version)[1]
