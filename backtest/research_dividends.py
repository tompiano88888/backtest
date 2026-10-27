"""Pinned ex-post ETF cash dividends, separate entitlement/receivable/payment."""
from datetime import date
from types import SimpleNamespace
from .models import D

VERSION='fc5b0209d6f86935f26477238c1a50aceee8c1b1a43e73404ce0df7ecb947339'


def load_events(version=VERSION):
    import duckdb
    from .ifind_etf_dividends import read_events
    from .cash_futures_basis import PAIRS
    db=duckdb.connect('data/etf_dividends/metadata.duckdb',read_only=True)
    try:
        store=SimpleNamespace(db=db)
        return [e for _,code in PAIRS.values() for e in read_events(store,version,code)]
    finally:db.close()


class DividendBook:
    def __init__(self,events):
        self.events=events;self.claims=[];self.audit=[];self.processed=set();self.total=D(0)

    def capture(self,day,ledger):
        for e in self.events:
            key=(e['instrument'],e['announcement_date'])
            if e['record_date']!=day or key in self.processed:continue
            self.processed.add(key)
            for lot in ledger.lots:
                if lot.instrument!=e['instrument'] or lot.side!=1:continue
                amount=lot.quantity*e['cash_per_unit']
                claim=dict(instrument=lot.instrument,group=lot.group,quantity=lot.quantity,
                    cash_per_unit=e['cash_per_unit'],amount=amount,announcement_date=e['announcement_date'],
                    record_date=day,ex_date=e['exchange_ex_date'],payment_date=e['payment_date'],recognized=False,paid=False)
                self.claims.append(claim)
                self.audit.append(dict(day=day,action='record_entitlement',**claim))

    def process(self,day,ledger):
        for c in self.claims:
            if not c['recognized'] and c['ex_date']<=day:
                c['recognized']=True;self.total+=c['amount']
                self.audit.append(dict(day=day,action='recognize_receivable',**c))
            if c['recognized'] and not c['paid'] and c['payment_date']<=day:
                ledger._cash(c['amount'],'etf_dividend',c['instrument'],c['group'],day)
                c['paid']=True;self.audit.append(dict(day=day,action='pay_cash',**c))

    def receivables(self):
        result={}
        for c in self.claims:
            if c['recognized'] and not c['paid']:result[c['group']]=result.get(c['group'],D(0))+c['amount']
        return result
