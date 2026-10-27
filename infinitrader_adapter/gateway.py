"""Bindings to the locally inspected PythonGO API; never guesses fee APIs."""
from dataclasses import replace
from datetime import date, datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo
from backtest.models import D
from .domain import Contract, ContractRules, CounterAccount

TZ=ZoneInfo('Asia/Shanghai')


def aware(value):
    value=datetime.fromisoformat(str(value)) if not isinstance(value,datetime) else value
    return value.replace(tzinfo=TZ) if value.tzinfo is None else value.astimezone(TZ)


class ConfirmedRules:
    """User-authorized fallback for SDKs without per-contract fee/margin queries."""
    def __init__(self,path):self.path=Path(path)

    def query(self,code,day,now):
        rows=json.loads(self.path.read_text(encoding='utf-8'))['contracts']
        r=rows[code]  # missing is deliberately fatal, never fills research defaults
        required=('long_margin_rate','short_margin_rate','open_rate','open_per_lot','close_rate',
                  'close_per_lot','close_today_rate','close_today_per_lot')
        if not date.fromisoformat(r['valid_from'])<=day<=date.fromisoformat(r['valid_until']):
            raise ValueError('Counter-confirmed rules do not cover trading day')
        confirmed=aware(r['confirmed_at'])
        if confirmed>now:raise ValueError('Future confirmation time')
        return ContractRules(code,day,**{k:D(r[k]) for k in required},source=r['source'],
            confirmed_at=confirmed,valid_until=date.fromisoformat(r['valid_until']))


class CheckpointEstimates:
    """User authorized research estimates; never labelled queried live rates."""
    def __init__(self,path):self.snapshot=json.loads(Path(path).read_text(encoding='utf-8'))

    def query(self,code,day,now):
        r=self.snapshot['contracts'].get(code)
        if r is None:r=self.snapshot['products'][code[:2]]
        margin=D(self.snapshot['research_margin_rate'])
        return ContractRules(code,day,margin,margin,*(D(r[k]) for k in
            ('open_rate','open_per_unit','close_rate','close_per_unit','close_today_rate','close_today_per_unit')),
            source='ESTIMATE: research margin; historical exchange fees '+self.snapshot['fee_version']+' source='+r['source_contract'],
            confirmed_at=aware(self.snapshot['as_of']),valid_until=day)


class PythonGoGateway:
    def __init__(self,strategy,investor,rules_path,rules_mode='counter_confirmed'):
        if not investor.strip():raise ValueError('Explicit simulation investor required')
        if rules_mode not in ('counter_confirmed','checkpoint_estimates'):raise ValueError('Unknown rule source')
        self.strategy=strategy;self.investor=investor
        self.rules_provider=(ConfirmedRules if rules_mode=='counter_confirmed' else CheckpointEstimates)(rules_path)
        self.status={};self.rules={};self.codes={}

    def account(self,now):
        a=self.strategy.get_account_fund_data(self.investor)
        if a.investor != self.investor:raise ValueError('Counter investor mismatch')
        return CounterAccount(*(D(getattr(a,k)) for k in ('dynamic_rights','margin','frozen_margin','available','risk','commission')),
                              queried_at=now,previous_balance=D(a.pre_balance),deposit=D(a.deposit),withdrawal=D(a.withdraw))

    def positions(self):
        from pythongo import infini
        result={}
        for p in infini.get_investor_position(self.investor,simple=True):
            if p['Position']:
                if p.get('HedgeFlag')!='1':raise ValueError('Dedicated speculative simulation account required')
                if p['Direction'] not in ('多','空'):raise ValueError('Unknown position direction')
                key=p['InstrumentID'],1 if p['Direction']=='多' else -1
                result[key]=result.get(key,0)+int(p['Position'])
        return result

    def discover(self,day):
        contracts={}
        for product in ('IF','IH','IC','IM'):
            # Installed SDK returns {ProductClass: [codes]}, unlike some examples.
            result=self.strategy.get_instruments_by_product('CFFEX',product,raw_data=False)
            if not isinstance(result,dict) or '1' not in result:
                raise ValueError('Unexpected PythonGO instrument-list schema')
            codes=result['1']
            for code in codes:
                r=self.strategy.get_instrument_data('CFFEX',code)
                expiry=datetime.strptime(r.expire_date.replace('-',''),'%Y%m%d').date()
                if expiry<day:continue
                if r.product_id!=product or r.exchange!='CFFEX':raise ValueError('Instrument product mismatch')
                contracts[code]=Contract(code,product,'CFFEX',D(r.size),D(r.price_tick),expiry)
        if {c.product for c in contracts.values()} != {'IF','IH','IC','IM'}:
            raise ValueError('Incomplete four-product contract universe')
        self.codes=contracts
        return contracts

    def query_rules(self,codes,day,now):
        self.rules={code:self.rules_provider.query(code,day,now) for code in codes}
        return self.rules

    def is_tradeable(self,code,auction=False):
        return self.status.get(code)=='连续交易' or (auction and self.status.get(code)=='集合竞价报单')

    def send(self,intent):
        buy=(intent['side']==1)==(intent['effect']=='open')
        return self.strategy.make_order_req(exchange='CFFEX',instrument_id=intent['instrument'],
            volume=intent['quantity'],price=float(D(intent['price'])),order_direction='BUY' if buy else 'SELL',
            offset='0' if intent['effect']=='open' else '1',order_type='GFD',investor=self.investor,
            hedgeflag='1',market=False,memo=intent['client_id'])

    def cancel(self,order_id):
        result=self.strategy.cancel_order(order_id)
        if result!=0:raise ValueError('Cancel request was not accepted; terminal confirmation still required')


def prepare_history(market_center,contracts,day):
    """Use actual-contract D1 observations, never platform dominant codes/ATR."""
    from backtest.rbreaker import select_main
    from backtest.rbreaker_stops import prior_atr
    rows=[]
    for code in sorted(contracts):
        data=market_center.get_kline_data(exchange='CFFEX',instrument_id=code,style='D1',count=-64,simply=False)
        used=set()
        for r in data:
            stamp=aware(r['datetime']);d=stamp.date()
            if d>=day:continue
            if d in used:raise ValueError('Duplicate daily observation')
            used.add(d)
            h,l,c=(D(r[k]) for k in ('high','low','close'))
            if not 0<l<=c<=h:raise ValueError('Invalid daily OHLC')
            rows.append(dict(instrument=code,trading_date=d,high=h,low=l,close=c,
                             open_interest=D(r['open_interest']),volume=D(r['volume'])))
    mains={};previous={};atr={}
    for product in ('IF','IH','IC','IM'):
        selected=[r for r in rows if contracts[r['instrument']].product==product]
        if not selected:raise ValueError(f'Missing history for {product}')
        last=max(r['trading_date'] for r in selected)
        code=select_main([r for r in selected if r['trading_date']==last],contracts,day)
        mains[product]=code
        previous[code]=next({k:str(v) for k,v in r.items() if k in ('high','low','close')}
                            for r in selected if r['instrument']==code and r['trading_date']==last)
        # Append an inert current-day row to obtain the ATR known before today.
        last_row=next(r for r in selected if r['instrument']==code and r['trading_date']==last)
        observations=[r for r in selected if r['instrument']==code]
        values=prior_atr(observations+[dict(last_row,trading_date=day)])
        atr[code]=values[code,day]  # insufficient history stops startup
    return mains,previous,atr
