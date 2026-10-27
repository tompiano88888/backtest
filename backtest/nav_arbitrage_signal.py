"""Same-date daily NAV adjustment, explicit ex-post publication assumption."""
from pathlib import Path
from types import SimpleNamespace
from .models import D
from .cash_futures_basis import PAIRS

VERSION='6feaff33a4ac4320832a06e488b1794a'


def adjusted_rate(future,index,etf,nav,days):
    if days<=0 or any(x is None for x in (future,index,etf,nav)):return None
    if min(index,etf,nav)<=0:return None
    return ((future-index)/index+(nav-etf)/nav)*D(365)/D(days)


def load_nav(version=VERSION):
    import duckdb
    from .ifind_index_etf import nav_from_snapshot
    db=duckdb.connect('data/index_etf/metadata.duckdb',read_only=True)
    try:
        store=SimpleNamespace(db=db,root=Path('data/index_etf'))
        return {(r['instrument'],r['trading_date']):r for _,code in PAIRS.values()
                for r in nav_from_snapshot(store,version,code)}
    finally:db.close()


class NAVSignal:
    def __init__(self,prices,spot,instruments,nav):
        self.prices,self.spot,self.instruments,self.nav=prices,spot,instruments,nav
        self.audit={}

    def rate(self,product,code,day):
        key=product,code,day
        if key in self.audit:return self.audit[key]['adjusted_annualized_rate']
        f=self.prices.get((code,day),{}).get('close')
        index_code,etf_code=PAIRS[product]
        index=self.spot.get((index_code,day),{}).get('close')
        etf=self.spot.get((etf_code,day),{}).get('close')
        nav_row=self.nav.get((etf_code,day),{})
        nav=nav_row.get('unit_nav')
        days=(self.instruments[code].expiry-day).days if code else 0
        rate=adjusted_rate(f,index,etf,nav,days)
        self.audit[key]=dict(product=product,contract=code,day=day,days_to_expiry=days,
            future_close=f,index_close=index,etf=etf_code,etf_close=etf,unit_nav=nav,
            unit_nav_known_at=nav_row.get('unit_nav_known_at'),
            futures_basis_ratio=(f-index)/index if f is not None and index and index>0 else None,
            etf_discount_ratio=(nav-etf)/nav if nav and etf is not None and nav>0 else None,
            adjusted_annualized_rate=rate,
            issue='expiry_or_after' if days<=0 else ('missing_or_invalid_inputs' if rate is None else ''))
        return rate
