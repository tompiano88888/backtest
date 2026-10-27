"""One-lot R-Breaker callback and offline minute integration experiment."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import csv
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from .accounting import Ledger
from .engine import Engine, ExecutionConfig, Frame
from .factor_data import load_research_data
from .models import Bar, D, MarginRule
from .storage import json_default

TZ = ZoneInfo('Asia/Shanghai')
DAILY = '0fff65706ce54991a3725f1bcbb6c6c9'
MINUTE = 'df7196984c186d24c4c24fdb1aa87ecbbfcde14d35b20ea12be05c8600ce41c1'
METADATA = 'f12d3b245a824432a144c967bbe3bc36'


def levels(previous, f1=D('.47'), f2=D('.07'), f3=D('.25')):
    h, l, c = (D(previous[k]) for k in ('high', 'low', 'close'))
    sell_setup, buy_setup = h + f1 * (c-l), l - f1 * (h-c)
    return dict(bbreak=sell_setup + f3*(sell_setup-buy_setup),
                sbreak=buy_setup - f3*(sell_setup-buy_setup),
                ssetup=sell_setup, bsetup=buy_setup,
                senter=(1+f2)/2*(h+c)-f2*l,
                benter=(1+f2)/2*(l+c)-f2*h)


def select_main(previous_rows, instruments, day):
    """Only prior-session observations; ties by volume then code."""
    candidates = [r for r in previous_rows if instruments[r['instrument']].expiry >= day]
    return min(candidates, key=lambda r: (-D(r['open_interest']), -D(r['volume']), r['instrument']))['instrument'] if candidates else None


def minute_ends(day):
    for hour, minute in ((9, 30), (13, 0)):
        begin = datetime.combine(day, time(hour, minute), TZ)
        for offset in range(1, 121):
            yield begin + timedelta(minutes=offset)


def normalize_minute(row):
    """Explicit experiment assumption: vendor label=end; exclude opening record."""
    end = row['source_timestamp'].replace(tzinfo=TZ)
    if end.time() == time(9, 30):
        return None
    if not (time(9, 31) <= end.time() <= time(11, 30) or time(13, 1) <= end.time() <= time(15)):
        raise ValueError(f'Unexpected session timestamp: {end}')
    volume = D(row['volume'])
    if volume != int(volume):
        raise ValueError('Noninteger volume')
    return Bar(row['instrument'], end-timedelta(minutes=1), end, end.date(),
               *(D(row[k]) for k in ('open', 'high', 'low', 'close')), int(volume))


class RBreaker:
    def __init__(self):
        self.audit = []
        self.mode = {}

    def begin_day(self, main, previous):
        self.main = main
        self.level = levels(previous) if previous else None
        self.high = self.low = None

    def __call__(self, ctx):
        # Closing obsolete contracts precedes any entry into the new main.
        old = {p['instrument'] for p in ctx.positions if p['instrument'] != self.main}
        for code in old:
            ctx.target(code, 0)
        bar = ctx.bars.get(self.main)
        auction = getattr(ctx,'auctions',{}).get(self.main)
        if (bar is None and auction is None) or self.level is None:
            return
        high,low,price = (bar.high,bar.low,bar.close) if bar is not None else (auction.price,auction.price,auction.price)
        self.high = high if self.high is None else max(self.high, high)
        self.low = low if self.low is None else min(self.low, low)
        held = sum(p['side']*p['quantity'] for p in ctx.positions if p['instrument'] == self.main)
        target, reason = held, None
        k = self.level
        if ctx.time.time() >= time(14, 59):
            # Final close is unknowable before the final tradable open.
            if ctx.time.time() == time(14, 59):
                keep = self.mode.get(self.main) == 'breakout' and (held > 0 and price > k['bbreak'] or held < 0 and price < k['sbreak'])
                if not keep:
                    target, reason = 0, 'end_of_day_non_breakout'
                self.audit.append(dict(time=str(ctx.time), instrument=self.main, reason='overnight_decision', held=held, keep=bool(keep), close=str(price)))
        elif old:
            return
        elif held > 0 and self.high > k['ssetup'] and price < k['senter']:
            target, reason = -1, 'reversal'
        elif held < 0 and self.low < k['bsetup'] and price > k['benter']:
            target, reason = 1, 'reversal'
        elif not held and price > k['bbreak']:
            target, reason = 1, 'breakout'
        elif not held and price < k['sbreak']:
            target, reason = -1, 'breakout'
        if reason is not None:
            # Persistent targets also replace pending orders; never accumulate lots.
            ctx.target(self.main, target)
            if target:
                self.mode[self.main] = reason
            self.audit.append(dict(time=str(ctx.time), instrument=self.main, reason=reason, target=target, close=str(price)))


class MinuteCache:
    def __init__(self, root, report):
        self.root, self.manifest = Path(root).resolve(), report['manifest']
        self.cache = {}
        self.verified = set()
        self.auctions = {}

    def day(self, code, day, *, daily_open=None):
        import pyarrow.parquet as pq
        result = {}
        opening_seen = False
        used = set()
        for item in self.manifest:
            if item['instrument'] != code or not item['first'][:10] <= str(day) <= item['last'][:10]:
                continue
            key = item['path']
            used.add(key)
            if key not in self.cache:
                path = (self.root/key).resolve()
                if not path.is_relative_to(self.root) or hashlib.sha256(path.read_bytes()).hexdigest() != item['sha256']:
                    raise ValueError(f'Minute checksum/path failure: {key}')
                records = pq.read_table(path).to_pylist()
                if len(records) != item['rows']:
                    raise ValueError('Minute row count mismatch')
                self.cache[key] = records
                self.verified.add(key)
            for row in self.cache[key]:
                if row['source_timestamp'].date() != day:
                    continue
                if daily_open is not None and row['source_timestamp'].time()==time(9,30):
                    from .ifind_auction import index_futures_auction
                    if opening_seen:
                        raise ValueError('Duplicate auction result')
                    opening_seen = True
                    self.auctions[code] = index_futures_auction(row,day,daily_open)
                bar = normalize_minute(row)
                if bar is not None:
                    if bar.end in result:
                        raise ValueError('Duplicate minute')
                    result[bar.end] = bar
        expected = set(minute_ends(day))
        if set(result) != expected:
            raise ValueError(f'Missing/extra minute: {code}/{day}')
        if daily_open is not None and (code not in self.auctions or self.auctions[code].trading_date!=day):
            raise ValueError(f'Missing auction result: {code}/{day}')
        # Retain just the current partitions, bounded by active contracts.
        self.cache = {k:v for k,v in self.cache.items() if k in used}
        return result


def write_csv(path, rows):
    if not rows:
        return
    with path.open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(dict.fromkeys(k for row in rows for k in row)))
        writer.writeheader()
        writer.writerows(rows)


def reconcile_fills(engine):
    """Independent original-entry cash PnL; does not use settlement cash flows."""
    lots, pnl = defaultdict(list), D(0)
    for fill in engine.ledger.fills:
        leg = fill.instruction
        inst = engine.ledger.instruments[leg.instrument]
        key = leg.instrument, leg.side, leg.group
        pnl -= fill.fee
        if leg.effect == 'open':
            lots[key].append([leg.quantity, fill.price])
        else:
            remaining = leg.quantity
            while remaining:
                lot = lots[key][0]
                used = min(remaining, lot[0])
                pnl += used*leg.side*inst.multiplier*(fill.price-lot[1])
                remaining -= used
                lot[0] -= used
                if not lot[0]:
                    lots[key].pop(0)
    for (code, side, _), positions in lots.items():
        for quantity, entry in positions:
            pnl += quantity*side*engine.ledger.instruments[code].multiplier*(engine.ledger.marks[code]-entry)
    return pnl


def run(output='outputs/rbreaker_minute', start=date(2020, 1, 1), end=date(2026, 10, 8), products=('IF','IH','IC','IM')):
    import duckdb
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    daily_version, metadata, raw, facts, instruments = load_research_data('data/index_futures', DAILY, METADATA)
    with duckdb.connect('data/index_futures_minute/metadata.duckdb', read_only=True) as db:
        found = db.execute('SELECT report FROM futures_minute_snapshots WHERE version=?', [MINUTE]).fetchone()
    if found is None:
        raise ValueError('Pinned minute snapshot missing')
    report = json.loads(found[0])
    if report['source_daily_version'] != daily_version:
        raise ValueError('Minute/daily version mismatch')
    config = dict(start=str(start), end=str(end), products=list(products), minute_version=MINUTE, daily_version=daily_version,
                  metadata_version=metadata, size=1, f1='.47', f2='.07', f3='.25', fees='0', slippage_ticks=0,
                  margin='explicit research assumption 15%; not historical risk validation',
                  clock='vendor end labels; omit 09:30; daily observed dates; 09:30-11:30/13:00-15:00',
                  main='prior trading day open interest maximum; ties volume then code',
                  overnight='14:59 breakout-entry position still beyond directional breakout level; final open exit otherwise',
                  roll='close obsolete main then await fresh main signal',
                  limitations=['timestamp interpretation unverified', '32 vendor contract-day volume/amount discrepancies retained',
                               'metadata retrieved later; not verified historical announcement PIT', 'no historical limit-state feed',
                               'gross PnL; not transaction-cost or margin validation'])
    (output/'config.json').write_text(json.dumps(config, indent=2), encoding='utf-8')
    summaries, all_daily, all_months = [], [], []
    for product in products:
        folder = output/product
        folder.mkdir(exist_ok=True)
        rows = [r for r in raw if r['product'] == product]
        by_day = defaultdict(list)
        indexed = {(r['instrument'], r['trading_date']):r for r in rows}
        for row in rows:
            by_day[row['trading_date']].append(row)
        days = sorted(by_day)
        insts = {k:v for k,v in instruments.items() if v.product == product}
        engine = Engine(Ledger(insts, '10000000'), {k:MarginRule('future', D('.15')) for k in insts}, ExecutionConfig(participation=D('.1')))
        callback, cache = RBreaker(), MinuteCache('data/index_futures_minute', report)
        daily, mapping, audit, operations = [], [], [], []
        frame_count = overnight_days = 0
        previous_pnl = D(0)
        for index, day in enumerate(days):
            if day < start or day > end or index == 0:
                continue
            prior_day = days[index-1]
            code = select_main(by_day[prior_day], insts, day)
            if not code:
                continue
            prior = indexed.get((code, prior_day))
            callback.begin_day(code, prior)
            mapping.append(dict(day=day, information_day=prior_day, instrument=code, **{k:str(v) for k,v in callback.level.items()}))
            codes = {code} | {p.instrument for p in engine.ledger.lots}
            prices = {i:cache.day(i, day) for i in codes}
            for stamp in minute_ends(day):
                bars = {i:series[stamp] for i,series in prices.items()}
                final = stamp.time() == time(15)
                settlements = tuple((i,day,D(indexed[i,day]['settlement_price'])) for i in codes) if final else ()
                snapshot = engine.step(Frame(stamp, bars, frozenset(codes), frozenset(codes) if final else frozenset(), settlements), on_bar=callback)
                frame_count += 1
                if sum(p.quantity for p in engine.ledger.lots) > 1:
                    raise AssertionError('One-lot product cap exceeded')
                if final:
                    if snapshot.equity != snapshot.cash or any(p.basis != D(indexed[p.instrument, day]['settlement_price']) for p in engine.ledger.lots):
                        raise AssertionError('Daily settlement/basis mismatch')
                    check = reconcile_fills(engine)
                    if check != snapshot.pnl:
                        raise AssertionError(f'Independent PnL mismatch: {check} != {snapshot.pnl}')
            held = sum(p.side*p.quantity for p in engine.ledger.lots)
            overnight_days += bool(held)
            daily.append(dict(product=product, day=day, main=code, cash=snapshot.cash, equity=snapshot.equity,
                              pnl=snapshot.pnl, daily_pnl=snapshot.pnl-previous_pnl, overnight=held,
                              settlement=indexed[code,day]['settlement_price'], reconciliation_difference=check-snapshot.pnl))
            previous_pnl = snapshot.pnl
            # Stream completed diagnostics out of Engine; preserve all active state.
            operations.extend(asdict(op) for op in engine.operations if op.state not in ('pending','unwinding'))
            engine.operations[:] = [op for op in engine.operations if op.state in ('pending','unwinding')]
            engine.snapshots.clear()
            if len(daily) % 100 == 0:
                print(f'{product} {day} days={len(daily)} pnl={snapshot.pnl}', flush=True)
        if not daily:
            raise ValueError(f'No eligible days: {product}')
        write_csv(folder/'daily.csv', daily)
        write_csv(folder/'main_mapping.csv', mapping)
        write_csv(folder/'signals.csv', callback.audit)
        write_csv(folder/'cash_entries.csv', engine.ledger.entries)
        fills = [dict(time=f.time, trading_date=f.trading_date, **asdict(f.instruction), price=f.price, fee=f.fee) for f in engine.ledger.fills]
        write_csv(folder/'fills.csv', fills)
        (folder/'operations.json').write_text(json.dumps(operations+[asdict(o) for o in engine.operations], default=json_default), encoding='utf-8')
        (folder/'warnings.json').write_text(json.dumps(engine.warnings, default=json_default), encoding='utf-8')
        month_pnl = defaultdict(lambda:D(0))
        for entry in engine.ledger.entries:
            month_pnl[entry['instrument']] += D(entry['amount'])
        for lot in engine.ledger.lots:
            month_pnl[lot.instrument] += lot.side*lot.quantity*insts[lot.instrument].multiplier*(engine.ledger.marks[lot.instrument]-lot.basis)
        if sum(month_pnl.values(), D(0)) != snapshot.pnl:
            raise AssertionError('Contract PnL attribution mismatch')
        all_months.extend(dict(product=product, instrument=k, pnl=v) for k,v in sorted(month_pnl.items()))
        peak = D(0)
        max_dd = D(0)
        for row in daily:
            peak = max(peak,row['pnl'])
            max_dd = max(max_dd,peak-row['pnl'])
        summaries.append(dict(product=product, first_day=daily[0]['day'], last_day=daily[-1]['day'], pnl=snapshot.pnl,
                              fills=len(fills), overnight_days=overnight_days, final_quantity=daily[-1]['overnight'],
                              max_daily_drawdown_yuan=max_dd, frames=frame_count, reconciliation_difference=check-snapshot.pnl))
        all_daily.extend(daily)
        print(f'COMPLETE {product}: {snapshot.pnl}', flush=True)
    write_csv(output/'summary.csv', summaries)
    write_csv(output/'contract_pnl.csv', all_months)
    total = sum((r['pnl'] for r in summaries), D(0))
    summary = dict(products=summaries,total_pnl=total,config=config,
                   code_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    (output/'summary.json').write_text(json.dumps(summary, indent=2,default=json_default), encoding='utf-8')
    text = '# R-Breaker 分钟统一框架测试\n\n'
    text += '全流程使用单一 on_bar 回调 → Engine 撮合 → Ledger 每日结算，未另建执行账务路径。\n\n'
    text += '|品种|起始日|截止日|PnL（元）|成交笔数|隔夜天数|期末净手数|\n|---|---|---|---:|---:|---:|---:|\n'
    for r in summaries:
        text += f"|{r['product']}|{r['first_day']}|{r['last_day']}|{r['pnl']}|{r['fills']}|{r['overnight_days']}|{r['final_quantity']}|\n"
    text += f'\n合计 PnL：{total} 元。每品种最多一手；各品种独立运行，用于框架验证。\n\n'
    text += '每日日终以真实日线 settlement_price 结算，现金=权益、持仓basis=结算价；独立按原始开平仓价计算PnL并逐日核对，差异均为零。contract_pnl.csv 按实际月份合约归因；fills/cash_entries/operations 保留审计。\n\n'
    text += '参数及关键假设见 config.json 和 docs/RBREAKER_MINUTE.md。零手续费、零滑点，保证金15%仅为显式研究假设，不是历史保证金验收。分钟时间边界、历史涨跌停状态及资料可知时间尚未全部验收；32个合约交易日供应商量额差异仍保留。故本结果为条件性分钟接入实验，不代表数据质量或完整V1验收。\n'
    (output/'report.md').write_text(text,encoding='utf-8')
    return summary
