"""Current seasonal cash/futures signals executed only by the common Engine.

Daily raw OHLC is an explicit daily execution approximation, not minute data.
No legacy Portfolio, execute(), Ledger.apply(), rollback, or same-close fills.
"""
from bisect import bisect_right
from collections import defaultdict
from dataclasses import asdict
from datetime import date, datetime, time, timedelta
import csv
import hashlib
import json
import math
from pathlib import Path
from zoneinfo import ZoneInfo

from .accounting import Ledger
from .account_events import AccountingEvents, CashDividend, IdleCashIncome
from .cash_carry_callback import ParameterSchedule, export_parameters
from .cash_futures_basis import PAIRS, load_spot
from .dividend_season_signal import DividendSeasonSignal, load_total_return
from .engine import Engine, ExecutionConfig, Frame
from .factor_data import load_research_data
from .front_contract_arbitrage import front_map
from .models import Account, Asset, Bar, D, Instrument, Instruction, MarginRule
from .research_dividends import load_events
from .runner import result
from .storage import json_default

ZONE = ZoneInfo('Asia/Shanghai')
SOURCE = Path('outputs/arbitrage_dividend_may_aug_optimized_floors')


def write(path, rows):
    rows = list(rows)
    if not rows:
        path.write_text('', 'utf-8')
        return
    fields = list(dict.fromkeys(k for r in rows for k in r))
    with path.open('w', newline='', encoding='utf-8-sig') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


class Liquidity:
    """Strategy T+1 reserve from actual close fills, never prospective fills.

    Reserve ETF cash proceeds plus released future margin until next observed
    trading day. Close residual future PnL remains real account cash; this is
    explicitly different from the legacy whole-group release formula.
    """
    def __init__(self, days, instruments, margin):
        self.days, self.instruments, self.margin = days, instruments, margin
        self.cursor = 0
        self.locks = []
        self.audit = []

    def sync(self, ledger, day):
        self.locks = [r for r in self.locks if r['release'] is None or r['release'] > day]
        for fill in ledger.fills[self.cursor:]:
            leg = fill.instruction
            if leg.effect != 'close':
                continue
            inst = self.instruments[leg.instrument]
            amount = fill.price * leg.quantity * inst.multiplier
            if inst.asset == Asset.FUTURE:
                amount *= self.margin
            elif inst.asset != Asset.FUND:
                continue
            index = bisect_right(self.days, fill.trading_date)
            release = self.days[index] if index < len(self.days) else None
            row = dict(day=fill.trading_date, group=leg.group, instrument=inst.id,
                       amount=max(amount-fill.fee, D(0)), release=release)
            self.locks.append(row)
            self.audit.append(row)
        self.cursor = len(ledger.fills)

    @property
    def locked(self):
        return sum((r['amount'] for r in self.locks), D(0))


class ReservedIncome(IdleCashIncome):
    def __init__(self, liquidity):
        super().__init__([dict(valid_from='2020-01-01', annual_rate='.02'),
                          dict(valid_from='2021-01-01', annual_rate='.015')])
        self.liquidity = liquidity

    def accrue(self, day, ledger, rules):
        self.liquidity.sync(ledger, day)
        self.reserve = self.liquidity.locked
        super().accrue(day, ledger, rules)


class NodeCallback:
    def __init__(self, parameters, instruments, mapping, rate, floors, liquidity, margin=D('.15')):
        self.schedule = ParameterSchedule(parameters)
        self.instruments, self.mapping, self.rate = instruments, mapping, rate
        self.floors, self.liquidity, self.margin = floors, liquidity, margin
        self.groups, self.audit, self.signals = {}, [], []
        self.serial = 0

    def __call__(self, ctx):
        day = ctx.time.date()
        held = defaultdict(list)
        for position in ctx.positions:
            held[position['group']].append(position)
        free = ctx.account.cash-ctx.account.margin-self.liquidity.locked
        funding_short = free < 0
        ordered = sorted(self.groups.items(), key=lambda item: (
            abs((self.rate(item[1]['product'],item[1]['future'],day) or D(0))-item[1]['exit']), item[0]))
        for group, state in ordered:
            if state['operation'].state in ('pending','unwinding'):
                continue
            positions = held[group]
            if not positions:
                continue
            value = self.rate(state['product'], state['future'], day)
            residual = not any(p['instrument'] == state['future'] for p in positions)
            reason = ('retry_exit' if state.get('exiting') else 'residual_after_delivery' if residual
                      else 'frozen_threshold' if value is not None and value < state['exit']
                      else 'funding_next_open' if funding_short else None)
            if reason is None:
                continue
            quantities = defaultdict(int)
            for p in positions:
                quantities[p['instrument'], p['side']] += p['quantity']
            state['operation'] = ctx.submit([Instruction(i,s,q,'close',group) for (i,s),q in quantities.items()], combination=len(quantities)>1)
            state['exiting'] = True
            self.audit.append(dict(time=ctx.time, day=day, action='close', group=group, reason=reason))
            if reason == 'funding_next_open':
                # One group at a time, no assumption that its pending exit releases cash.
                break
        for state in self.groups.values():
            op = state['operation']
            if op.state in ('pending','unwinding'):
                for leg in op.legs:
                    if leg.effect == 'open':
                        inst = self.instruments[leg.instrument]
                        free -= ctx.marks[inst.id]*leg.quantity*inst.multiplier*(self.margin if inst.asset==Asset.FUTURE else 1)
        if funding_short:
            return
        candidates = []
        for product,(index,etf) in PAIRS.items():
            params = self.schedule.get(product, day)
            code = self.mapping.get((product,day))
            if params is None or not code or day >= self.instruments[code].expiry:
                continue
            value = self.rate(product, code, day)
            self.signals.append(dict(day=day,product=product,contract=code,rate=value,
                                     entry=params[0],exit=params[1],floor=self.floors[product]))
            if value is None or value <= params[0] or value < self.floors[product]:
                continue
            bars = [ctx.bars.get(i) for i in (code,index,etf)]
            if any(b is None for b in bars) or bars[2].close <= 0:
                continue
            inst = self.instruments[code]
            quantity = int(bars[1].close*inst.multiplier/bars[2].close/100)*100
            reserve = quantity*bars[2].close+bars[0].close*inst.multiplier*self.margin
            if quantity > 0 and reserve > 0:
                candidates.append((value-params[1],product,code,etf,quantity,reserve,params))
        candidates.sort(key=lambda c:(-c[0],c[1]))
        while candidates:
            chosen = next((c for c in candidates if c[5] <= free),None)
            if chosen is None:
                break
            gap,product,code,etf,quantity,reserve,params = chosen
            self.serial += 1
            group = f'{product}_engine_{self.serial:06d}'
            op = ctx.submit([Instruction(code,-1,1,group=group),Instruction(etf,1,quantity,group=group)], combination=True)
            self.groups[group] = dict(operation=op,product=product,future=code,exit=params[1],signal_day=day)
            self.audit.append(dict(time=ctx.time,day=day,action='open',group=group,product=product,
                                   future=code,etf=etf,quantity=quantity,expected_gap=gap,reserve=reserve,
                                   entry=params[0],exit=params[1]))
            free -= reserve


def raw_bar(code, row, day, is_index=False):
    """Preserve daily OHLC; never synthesize an intraday path or fill a missing row."""
    volume = row.get('volume',row.get('source_volume'))
    if any(row.get(k) is None for k in ('open','high','low','close')):
        return None
    if not is_index and (volume is None or row.get('ohlcv_complete',True) is False):
        return None
    return Bar(code,datetime.combine(day,time(9,30),ZONE),datetime.combine(day,time(15),ZONE),day,
               *(row[k] for k in ('open','high','low','close')),int(volume or 0),
               settlement_price=row.get('settlement_price'),previous_settlement_price=row.get('previous_settlement_price'))


def valid_open(state, day, instruments, mapping):
    code=state['future']
    return day<instruments[code].expiry and mapping.get((state['product'],day))==code


def run(output='outputs/arbitrage_node_engine_20261022', start=date(2020,1,1), end=date(2026,10,8)):
    out = Path(output)
    if out.exists():
        raise ValueError('Output directory already exists; use a new directory')
    out.mkdir(parents=True)
    prior = json.loads((SOURCE/'config.json').read_text('utf-8'))
    if prior['dividend_season_months'] != [5,6,7,8] or prior['nav_adjustment']:
        raise ValueError('Current node model mismatch')
    parameters = export_parameters(SOURCE,out/'parameters.json')
    versions = prior['source_snapshots']
    _,_,raw,facts,instruments = load_research_data('data/index_futures',versions['daily_version'],versions['metadata_version'])
    _,spot_rows = load_spot(version=versions['spot_version'])
    prices = {(r['instrument'],r['trading_date']):r for r in raw}
    spot = {(r['instrument'],r['trading_date']):r for r in spot_rows}
    calendars = {p:sorted({r['trading_date'] for r in raw if r['product']==p}) for p in PAIRS}
    days = sorted({d for ds in calendars.values() for d in ds if start<=d<=end})
    market_days = set(days)
    first = {c:date.fromisoformat(f['first_trade_date']) for c,f in facts.items()}
    mapping = front_map(instruments,first,calendars)
    signal = DividendSeasonSignal(prices,spot,instruments,load_total_return(prior['total_return_version']),active_months=[5,6,7,8])
    for product,(index,etf) in PAIRS.items():
        instruments[index] = Instrument(index,Asset.INDEX,Account.SECURITY,'SSE',product)
        instruments[etf] = Instrument(etf,Asset.FUND,Account.SECURITY,'SSE',product,tick=D('.001'),lot=100,sell_delay_days=1)
    margin = D(prior['margin_rate'])
    rules = {c:MarginRule('future',margin) for c,i in instruments.items() if i.asset==Asset.FUTURE}
    liquidity = Liquidity(days,instruments,margin)
    income = ReservedIncome(liquidity)
    events = [CashDividend(f"{e['instrument']}/{e['announcement_date']}",e['instrument'],e['record_date'],
                           e['exchange_ex_date'],e['payment_date'],e['cash_per_unit'],source=prior['dividend_version'])
              for e in load_events(prior['dividend_version']) if start<=e['record_date']<=end]
    accounting = AccountingEvents(events,income)
    floors = {p:D(v) for p,v in prior['min_expected_annual_return_by_product'].items()}
    callback = NodeCallback(parameters,instruments,mapping,signal.rate,floors,liquidity,margin)
    guard_audit = []
    def guard(ctx, fill):
        liquidity.sync(engine.ledger,ctx.trading_date)
        leg = fill.instruction
        if leg.effect == 'close':
            return leg.quantity
        if not valid_open(callback.groups[leg.group],ctx.trading_date,instruments,mapping):
            guard_audit.append(dict(time=ctx.time,group=leg.group,instrument=leg.instrument,
                                    quantity=leg.quantity,allowed=0,reason='expiry_or_front_changed'))
            return 0
        inst = instruments[leg.instrument]
        required = fill.price*leg.quantity*inst.multiplier*(margin if inst.asset==Asset.FUTURE else 1)+fill.fee
        free = ctx.account.cash-ctx.account.margin-liquidity.locked
        allowed = leg.quantity if required <= free else 0
        guard_audit.append(dict(time=ctx.time,group=leg.group,instrument=inst.id,quantity=leg.quantity,
                                allowed=allowed,required=required,free=free))
        return allowed
    engine = Engine(Ledger(instruments,prior['initial']),rules,ExecutionConfig(participation=D('.1')),
                    {c:ExecutionConfig(participation=D('.1'),slippage_ticks=1) for c in rules},
                    accounting_events=accounting,execution_guard=guard)
    daily,completed_operations = [],[]
    day = start
    while day<=end:
        bars = {}
        if day in market_days:
            codes = [c for c,i in instruments.items() if i.asset!=Asset.FUTURE or first[c]<=day<=i.expiry]
            for code in codes:
                row = prices.get((code,day)) if instruments[code].asset==Asset.FUTURE else spot.get((code,day))
                if row:
                    bar = raw_bar(code,row,day,instruments[code].asset==Asset.INDEX)
                    if bar:
                        bars[code] = bar
            expected = frozenset(codes)
            settlements = tuple((c,day,prices[c,day]['settlement_price']) for c in codes if c in rules and (c,day) in prices)
        else:
            expected, settlements = frozenset(), ()
        # Opening values are observation-only, never synthetic tradable bars.
        # They permit ex-date receivable recognition with raw ex-date valuation.
        engine.step(Frame(datetime.combine(day,time(9,30),ZONE),{},frozenset(),
                          valuation_marks={c:b.open for c,b in bars.items()},accounting_day_start=day))
        snap = engine.step(Frame(datetime.combine(day,time(15),ZONE),bars,expected,
                                 settle_instruments=expected,settlements=settlements,accounting_day_end=day),
                           on_day_end=callback if day in market_days else None)
        daily.append(dict(day=day,is_market_day=day in market_days,**asdict(snap),locked_T1=liquidity.locked))
        # Same external-runner compaction used by R-Breaker: keep active objects,
        # archive completed operations for reporting; no trading/ledger changes.
        completed_operations.extend(asdict(op) for op in engine.operations if op.state not in ('pending','unwinding'))
        engine.operations[:]=[op for op in engine.operations if op.state in ('pending','unwinding')]
        if day.month==1 and day.day<5:
            print(f'{day} equity={snap.equity} fills={len(engine.ledger.fills)}',flush=True)
        day += timedelta(days=1)
    config = dict(initial_cash=prior['initial'],node_source=SOURCE.as_posix(),node_config=prior,
                  execution='Engine daily raw OHLC, 10% native-volume participation; future 1tick; ETF zero fee/slippage; all fees zero as node',
                  sizing='completed-close integer quantities; no open-price lookahead or retrospective cancellation',
                  differences=['daily OHLC, not complete minute history; 3 common daily bars for retries',
                               'next allowed daily open for all exits including funding/residual ETF after delivery',
                               'daily source volume treated as units under unverified supplier-unit assumption',
                               'T1 locks ETF sale proceeds and released future margin; excludes residual close PnL',
                               'observed futures-date calendar; historical limits/suspensions/PIT not independently verified'],
                  code_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    output_result = result(engine,config)
    output_result['operations']=sorted(completed_operations+output_result['operations'],key=lambda op:op['id'])
    (out/'result.json').write_text(json.dumps(output_result,default=json_default,ensure_ascii=False), 'utf-8')
    (out/'config.json').write_text(json.dumps(config,default=json_default,ensure_ascii=False,indent=2), 'utf-8')
    fills = [dict(time=f.time,day=f.trading_date,**asdict(f.instruction),price=f.price,fee=f.fee) for f in engine.ledger.fills]
    write(out/'fills.csv',fills)
    write(out/'daily.csv',daily)
    write(out/'decisions.csv',callback.audit)
    write(out/'signals.csv',callback.signals)
    write(out/'execution_guard.csv',guard_audit)
    write(out/'liquidity_locks.csv',liquidity.audit)
    write(out/'idle_cash.csv',income.audit)
    write(out/'cash_entries.csv',engine.ledger.entries)
    write(out/'signal_forecasts.csv',signal.audit.values())
    previous,peak,dd = D(prior['initial']),D(prior['initial']),D(0)
    returns = []
    for row in daily:
        peak=max(peak,row['equity']);dd=max(dd,1-row['equity']/peak)
        if row['is_market_day']:
            returns.append(float(row['equity']/previous-1));previous=row['equity']
    mean=sum(returns)/len(returns)
    std=math.sqrt(sum((r-mean)**2 for r in returns)/(len(returns)-1))
    years=(end-start).days/365.25
    summary = dict(net_pnl=snap.pnl,equity=snap.equity,cagr=(float(snap.equity/D(prior['initial'])))**(1/years)-1,
                   sharpe=mean/std*math.sqrt(252) if std else 0,max_drawdown=dd,fills=len(fills),
                   groups=len(callback.groups),positions=len(engine.ledger.lots),
                   guard_deferred=sum(r['allowed']==0 for r in guard_audit),
                   max_usage=max((r['margin']/r['equity'] for r in daily if r['equity']>0),default=D(0)),
                   min_available=min(r['cash']-r['margin']-r['locked_T1'] for r in daily),
                   product_pnl={p:sum((v for g,v in output_result['group_pnl'].items() if g.startswith(p+'_')),D(0)) for p in PAIRS},
                   idle_cash=output_result['income']['idle_cash_income'],dividends=output_result['income']['dividend_gross'])
    assert sum(summary['product_pnl'].values(),summary['idle_cash']) == snap.pnl
    (out/'summary.json').write_text(json.dumps(summary,default=json_default,indent=2), 'utf-8')
    from .performance import export_performance
    export_performance(daily,prior['initial'],start,out,title='期现套利 · 统一Engine',
                       subtitle='日频真实OHLC · Engine / FeeService / Ledger',
                       caveat='统一日频执行与原研究节点不同；资金缺口照实披露，非完整跨市场分钟验收。')
    print(json.dumps(summary,default=json_default,indent=2),flush=True)
    return out
