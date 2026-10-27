"""One serialized four-product simulation controller for PythonGO callbacks."""
from datetime import date, datetime, time, timedelta
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
from threading import RLock

from backtest.models import Auction, D
from .clock import MinuteClock
from .domain import Quote
from .gateway import PythonGoGateway, TZ, aware, prepare_history
from .runtime import Runtime


class Controller:
    def __init__(self,strategy,config_path,market_center):
        self.lock=RLock();self.strategy=strategy;self.market=market_center
        self.config_path=Path(config_path)
        self.config=json.loads(self.config_path.read_text(encoding='utf-8'))
        if self.config['environment']!='simulation':raise ValueError('Simulation-only adapter')
        if self.config['mode'] not in ('observe','simulation'):raise ValueError('Unsupported execution mode')
        self.gateway=PythonGoGateway(strategy,self.config['investor'],self.config_path.parent/self.config['rules_file'],
                                     self.config.get('rules_mode','counter_confirmed'))
        now=datetime.now(TZ)
        contracts=self.gateway.discover(now.date())
        source_root=Path(__file__).resolve().parents[1]
        names=['backtest/rbreaker.py','backtest/rbreaker_portfolio.py','backtest/rbreaker_stops.py',
               'backtest/rbreaker_fee_avoidance.py','infinitrader_adapter/runtime.py','infinitrader_adapter/risk.py']
        names=sorted(set(names)|{str(p.relative_to(source_root)).replace('\\','/') for p in (source_root/'infinitrader_adapter').glob('*.py')})
        hashes={n:hashlib.sha256((source_root/n).read_bytes()).hexdigest() for n in names}
        identity=dict(config={k:v for k,v in self.config.items() if k!='mode'},source_hashes=hashes)
        state=self.config_path.parent/self.config['state_directory'];state.mkdir(parents=True,exist_ok=True)
        # Cross-process lock prevents two strategy instances sharing the same journal.
        import msvcrt
        self.process_lock=(state/'process.lock').open('a+b');self.process_lock.seek(0);self.process_lock.write(b'0');self.process_lock.flush()
        self.process_lock.seek(0)
        try:msvcrt.locking(self.process_lock.fileno(),msvcrt.LK_NBLCK,1)
        except OSError:
            self.process_lock.close();raise ValueError('Another instance owns this simulation state')
        self.runtime=Runtime(contracts,state/'events.jsonl',identity,
            counter_cap=D(self.config['counter_risk_cap']),participation=D('.1'))
        self.clock=MinuteClock();self.opens={};self.prepared=None;self.auction_observed=False;self.pre_open_done=False
        self.last_query=None;self.last_error=None
        self.history_cache={};self.next_prepare=None
        self.diagnostics=(state/'diagnostics.jsonl')
        self.account_log=state/'counter_account.jsonl'
        self.subscriptions=set(contracts)|{p['instrument'] for p in self.runtime.lots}
        for code in sorted(self.subscriptions):strategy.sub_market_data(exchange='CFFEX',instrument_id=code)
        if self.runtime.restored_unresolved:
            self.log('blocked','Unresolved pre-restart orders; reconcile in client before resuming')
        if self.runtime.fault:
            self.log('blocked','Persistent execution halt: '+self.runtime.fault)

    def log(self,event,message):
        row=dict(time=str(datetime.now(TZ)),event=event,message=message)
        with self.diagnostics.open('a',encoding='utf-8') as h:h.write(json.dumps(row,ensure_ascii=False,default=str)+'\n')
        self.strategy.output(event,message)

    def prepare(self,day,now):
        if self.prepared==day:return True
        if self.next_prepare and now<self.next_prepare:return False
        rt=self.runtime
        if rt.current_day==day and rt.last_frame:
            # Restored state is exact only up to its last recorded input. Do not
            # invent missing bars/orders or quietly skip an unknown intraday gap.
            if (now.time()>time(9,30) and now-rt.last_frame>timedelta(seconds=75)):
                raise ValueError('Intraday recovery gap requires market/order reconciliation')
            mains=rt.callback.mains
        else:
            if day not in self.history_cache:
                contracts=self.gateway.discover(day)
                mains,previous,atr=prepare_history(self.market,contracts,day)
                self.history_cache[day]=(contracts,mains,previous,atr)
            contracts,mains,previous,atr=self.history_cache[day]
        codes=set(mains.values())|{p['instrument'] for p in rt.lots}
        try:queried_rules=self.gateway.query_rules(codes,day,now)
        except (KeyError,ValueError,FileNotFoundError,json.JSONDecodeError) as exc:
            self.log('waiting_for_confirmed_rules',str(exc))
            self.next_prepare=now+timedelta(seconds=30)
            return False
        if rt.current_day!=day:rt.prepare_day(day,mains,previous,atr,contracts)
        rt.rules=queried_rules
        self.prepared=day;self.auction_observed=False;self.pre_open_done=False;self.opens={}
        for code in sorted(codes-self.subscriptions):
            self.strategy.sub_market_data(exchange='CFFEX',instrument_id=code);self.subscriptions.add(code)
        self.log('day_prepared',dict(day=str(day),mains=mains,contract_rules={c:asdict(r) for c,r in rt.rules.items()}))
        return True

    def refresh(self,now):
        if self.last_query and now-self.last_query<timedelta(seconds=1):return
        rt=self.runtime
        account=self.gateway.account(now);positions=self.gateway.positions()
        rt.synchronize(account,positions,now);self.last_query=now
        row=dict(trading_day=str(self.prepared or rt.current_day),**asdict(account),
                 synchronized=rt.counter_synced,positions=[dict(instrument=c,side=s,quantity=q) for (c,s),q in positions.items()])
        from backtest.rbreaker_portfolio import margin_usage
        marks={**rt.marks,**{c:q.price for c,q in rt.quotes.items()}}
        complete=all(c in rt.contracts and c in marks for c,s in positions)
        if complete:
            estimate=sum(margin_usage(row['positions'],marks,rt.contracts,D('.15')).values(),D(0))
            row.update(estimated_margin=estimate,margin_difference=account.margin-estimate,
                       margin_actual_to_estimated=account.margin/estimate if estimate>0 else None,
                       implied_uniform_margin_rate=account.margin/estimate*D('.15') if estimate>0 else None,
                       margin_comparison_usable=rt.counter_synced and estimate>0 and all(
                           c in rt.quotes and timedelta(0)<=now-rt.quotes[c].time<=timedelta(seconds=5) for c,s in positions))
        else:row.update(estimated_margin=None,margin_comparison_usable=False)
        if all(getattr(account,k) is not None for k in ('previous_balance','deposit','withdrawal')):
            row['day_pnl']=account.equity-account.previous_balance-account.deposit+account.withdrawal
        with self.account_log.open('a',encoding='utf-8') as h:
            h.write(json.dumps(row,ensure_ascii=False,default=str)+'\n')
        if now.time()>=time(15,15):
            row['status']='last_counter_observation_not_a_settlement_statement'
            p=self.account_log.parent/('daily_'+str(self.prepared or rt.current_day)+'.json')
            temp=p.with_suffix('.tmp');temp.write_text(json.dumps(row,ensure_ascii=False,default=str,indent=2),encoding='utf-8');temp.replace(p)
        self.last_error=None

    def tick(self,tick,now=None):
        with self.lock:
            real_clock=now is None
            now=now or datetime.now(TZ)
            if tick.exchange!='CFFEX' or tick.instrument_id not in self.subscriptions:return
            day=datetime.strptime(tick.trading_day,'%Y%m%d').date()
            if not self.prepare(day,now):return
            if real_clock:now=datetime.now(TZ)
            self.refresh(now)
            if real_clock:now=datetime.now(TZ)
            stamp=aware(tick.datetime)
            if not timedelta(0)<=now-stamp<=timedelta(seconds=10):raise ValueError('Stale or future tick')
            code=tick.instrument_id;rt=self.runtime
            rt.quotes[code]=Quote(D(tick.last_price),D(tick.bid_price1),D(tick.ask_price1),stamp,day,
                                  D(tick.upper_limit_price),D(tick.lower_limit_price))
            required=set(rt.callback.mains.values())
            active=required|{p['instrument'] for p in rt.lots}
            if code not in active:return
            # Separate opening-price observation; never fabricate auction fills.
            if time(9,30)<=stamp.time()<time(9,31) and D(tick.open_price)>0:
                if code not in self.opens:
                    # First continuous snapshot establishes a conservative volume
                    # baseline: don't treat unseparated auction volume as M1 volume.
                    self.clock.total_volume[code]=int(tick.volume)
                self.opens[code]=D(tick.open_price)
                if not self.auction_observed and required<=self.opens.keys():
                    at=datetime.combine(day,time(9,30),TZ)
                    rt.frame(at,{}, {c:Auction(c,at,day,self.opens[c],0) for c in required})
                    self.auction_observed=True
            if not self.auction_observed and now.time()>=time(9,31):
                raise ValueError('Missing opening observations; start before auction')
            self.clock.tick(code,stamp,day,tick.last_price,tick.volume)
            if time(9,30)<=stamp.time()<=time(11,30) or time(13)<=stamp.time()<=time(15):
                end=stamp.replace(second=0,microsecond=0)+timedelta(minutes=1)
                if stamp.time() in (time(11,30),time(15)):end=stamp
                rt.record(dict(kind='quote_risk',instrument=code,price=str(D(tick.last_price)),time=str(stamp),end=str(end)))
            self.run_timer(now)

    def run_timer(self,now=None):
        with self.lock:
            now=now or datetime.now(TZ);rt=self.runtime
            if self.prepared is None or now.date()!=self.prepared:return
            self.refresh(now)
            if time(9,25)<=now.time()<time(9,30) and not self.pre_open_done:
                rt.frame(now.replace(microsecond=0),{});self.pre_open_done=True
            for at,bars in self.clock.pop(now,set(rt.callback.mains.values())):
                rt.frame(at,bars)
            if now.time()>=time(9,31) and not self.auction_observed:
                raise ValueError('Missing auction observations; no retrospective opening signal')
            last=rt.last_frame
            if last and last.time()>=time(9,30) and last.time()<time(15):
                next_end=last+timedelta(minutes=1)
                if last.time()==time(11,30):next_end=last.replace(hour=13,minute=1)
                if now>next_end+timedelta(seconds=2):
                    raise ValueError('Missing complete main-contract minute; no forward fill')
            if self.config['mode']=='simulation':rt.pump(self.gateway,now,allow_auction=now.time()<time(9,30))
            if rt.audit:
                for row in rt.audit:self.log('admission',row)
                rt.audit.clear()

    def identify(self,data):
        memo=str(getattr(data,'memo',''))
        if memo in self.runtime.orders:return memo
        matches=[k for k,o in self.runtime.orders.items() if o['broker_id']==data.order_id]
        if len(matches)==1:return matches[0]
        raise ValueError('Unknown or ambiguous strategy order; execution stopped')

    def trade(self,trade,now=None):
        with self.lock:
            now=now or datetime.now(TZ);rt=self.runtime
            client_id=self.identify(trade);o=rt.orders[client_id]
            if not trade.trade_id or not trade.order_sys_id:raise ValueError('Missing exchange trade identity')
            expected_buy=(o['side']==1)==(o['effect']=='open')
            if (trade.instrument_id!=o['instrument'] or trade.exchange!='CFFEX' or trade.hedgeflag!='1' or
                trade.direction!=('0' if expected_buy else '1') or trade.offset!=('0' if o['effect']=='open' else '1')):
                raise ValueError('Trade side/offset/account ownership mismatch')
            stamp=datetime.strptime(trade.trade_time,'%Y%m%d %H:%M:%S').replace(tzinfo=TZ)
            key=(self.config['investor'],'CFFEX',str(rt.current_day),trade.trade_id,trade.order_sys_id)
            rt.receive_fill(client_id,key,trade.volume,D(trade.price),stamp,rt.current_day,received_at=now)
            self.log('real_fill',dict(client_id=client_id,instrument=o['instrument'],quantity=trade.volume,
                price=trade.price,action=o['action'],fee_basis='confirmed-rule estimate; counter commission reconciled separately'))
            self.last_query=None

    def order(self,order,cancel=False):
        with self.lock:
            k=self.identify(order);o=self.runtime.orders[k]
            if o['broker_id'] is None:self.runtime.record(dict(kind='ack',client_id=k,broker_id=order.order_id))
            if cancel:
                filled=o['quantity']-order.cancel_volume;terminal=True
            else:
                filled=order.traded_volume;terminal=order.status in ('全部成交','部成部撤','已撤销','投资经理驳回','交易员驳回')
            self.runtime.order_status(k,filled,terminal)
            self.last_query=None

    def close(self):
        # Do not pretend cancelling or flattening is instantaneous on stop.
        try:self.log('stopped',dict(active_orders=len(self.runtime.live_orders()),physical_lots=len(self.runtime.lots)))
        finally:
            import msvcrt
            self.process_lock.seek(0);msvcrt.locking(self.process_lock.fileno(),msvcrt.LK_UNLCK,1);self.process_lock.close()
