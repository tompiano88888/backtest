"""AKShare-only daily ingestion and replay; no vendor fields enter Engine."""
from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime
from decimal import InvalidOperation
import importlib
import re

from .models import Account, Asset, Bar, D
from .runner import engine_from_config, instrument_from_record, run_scenario
from .storage import Store, json_default
import json


def number(value):
    try:
        return D(value)
    except (InvalidOperation, ValueError, TypeError) as error:
        raise ValueError(f"Invalid/missing AKShare numeric value: {value!r}") from error


def validate(config):
    if config.get('accounting'):
        raise ValueError('Daily accounting-event calendar is not implemented')
    if config.get('frequency', '1d') != '1d':
        raise ValueError('Only daily (1d) data is supported')
    instruments = {r['id']: instrument_from_record(r) for r in config['instruments']}
    if not instruments or len(instruments) != len(config['instruments']):
        raise ValueError('Nonempty unique instruments required')
    if set(config['symbols']) != set(instruments):
        raise ValueError('Explicit AKShare symbol mapping required for every instrument')
    for key, instrument in instruments.items():
        if instrument.asset not in (Asset.EQUITY, Asset.FUTURE):
            raise ValueError('Only stocks and actual futures contracts are supported')
        if instrument.account != (Account.SECURITY if instrument.asset == Asset.EQUITY else Account.FUTURES):
            raise ValueError('Asset/account mismatch')
        symbol = config['symbols'][key]
        if instrument.asset == Asset.EQUITY:
            if not re.fullmatch(r'\d{6}', symbol) or instrument.sell_delay_days != 1 or instrument.lot != 100:
                raise ValueError('A shares require six digit symbol, lot=100 and sell_delay_days=1')
        else:
            if not re.fullmatch(r'[A-Za-z]+\d{3,4}', symbol):
                raise ValueError('Continuous futures cannot be traded; specify actual contract')
            if instrument.exchange not in {'CFFEX', 'SHFE', 'DCE', 'CZCE', 'INE', 'GFEX'}:
                raise ValueError('Unsupported futures exchange')
            if key not in config.get('margin_rules', {}):
                raise ValueError('Explicit futures margin rule required')
    if config.get('historical_fees'):
        raise ValueError('AKShare branch does not load external historical fee providers')
    # Trading dates and night-session boundaries must be supplied explicitly.
    sessions = {}
    for row in config['sessions']:
        key = (row['instrument'], date.fromisoformat(row['trading_date']))
        start, end = datetime.fromisoformat(row['start']), datetime.fromisoformat(row['end'])
        if key[0] not in instruments or key in sessions or start.tzinfo is None or end.tzinfo is None or start >= end:
            raise ValueError('Invalid/duplicate daily session')
        upper, lower = (number(row[f]) for f in ('upper_limit', 'lower_limit'))
        if lower <= 0 or upper < lower:
            raise ValueError('Invalid historical price limits')
        sessions[key] = (start, end, row)
    if not sessions or {key[0] for key in sessions} != set(instruments):
        raise ValueError('Explicit daily sessions required for every instrument')
    for key in instruments:
        periods = sorted((start, end) for (name, _), (start, end, _) in sessions.items() if name == key)
        if any(current[0] < previous[1] for previous, current in zip(periods, periods[1:])):
            raise ValueError('Overlapping daily sessions')
    engine_from_config(config)  # validate execution, fees and margin before network access
    return instruments, sessions


def download(config, api=None):
    instruments, sessions = validate(config)
    api = api or importlib.import_module('akshare')
    batches = {}
    bars = []
    for key, instrument in instruments.items():
        days = sorted(day for name, day in sessions if name == key)
        start, end = (d.strftime('%Y%m%d') for d in (days[0], days[-1]))
        if instrument.asset == Asset.EQUITY:
            table = api.stock_zh_a_hist(symbol=config['symbols'][key], period='daily', start_date=start, end_date=end, adjust='', timeout=30)
        else:
            batch = (instrument.exchange, start, end)
            if batch not in batches:
                batches[batch] = api.get_futures_daily(start_date=start, end_date=end, market=instrument.exchange)
            table = batches[batch]
        if table is None or table.empty:
            raise ValueError(f'Empty AKShare response: {key}')
        seen = set()
        for record in table.to_dict('records'):
            equity = instrument.asset == Asset.EQUITY
            if not equity and str(record['symbol']).upper() != config['symbols'][key].upper():
                continue
            day = date.fromisoformat(str(record['日期'])[:10]) if equity else datetime.strptime(str(record['date']).replace('-', '')[:8], '%Y%m%d').date()
            if (key, day) not in sessions:
                if days[0] <= day <= days[-1]:
                    raise ValueError(f'AKShare row has no configured session: {key}/{day}')
                continue
            if day in seen:
                raise ValueError(f'Duplicate daily row: {key}/{day}')
            seen.add(day)
            begin, finish, session = sessions[key, day]
            fields = ('开盘', '最高', '最低', '收盘') if equity else ('open', 'high', 'low', 'close')
            prices = [number(record[f]) for f in fields]
            if min(prices) <= 0:
                raise ValueError(f'Nonpositive price: {key}/{day}')
            volume = number(record['成交量' if equity else 'volume']) * (100 if equity else 1)
            if volume != volume.to_integral_value():
                raise ValueError('Nonintegral normalized volume')
            settlement = None if equity else number(record['settle'])
            if settlement is not None and settlement <= 0:
                raise ValueError('Missing/nonpositive futures settlement; close fallback forbidden')
            if not equity and (day > instrument.expiry or day > instrument.delivery):
                raise ValueError('Futures quote outside contract lifecycle')
            # Limits must be historical observations, not guessed from today's board rules.
            upper, lower = (number(session[f]) for f in ('upper_limit', 'lower_limit'))
            if lower <= 0 or lower > upper or max(prices) > upper or min(prices) < lower:
                raise ValueError('Invalid historical price limits')
            bars.append(Bar(key, begin, finish, day, *prices, int(volume), settlement,
                            upper, lower, bool(session.get('suspended', False))))
        missing = set(days) - seen
        if missing:
            raise ValueError(f'Missing expected daily bars: {key}/{sorted(missing)}; no forward fill')
    return sorted(bars, key=lambda bar: (bar.end, bar.instrument))


def update(config, root, api=None):
    bars = download(config, api)
    with Store(root) as store:
        # Independent complete snapshot: never mix in a previous provider's bars.
        return store.publish(bars, source='akshare', frequency='1d')


def scenario(config, root, version):
    instruments, sessions = validate(config)
    with Store(root) as store:
        source = store.db.execute('SELECT source FROM datasets WHERE version=?', [version]).fetchone()
        if not source or source[0] != 'akshare':
            raise ValueError('Replay requires an explicit AKShare snapshot')
        bars = store.bars(version, instruments, min(s[1] for s in sessions.values()), max(s[1] for s in sessions.values()), '1d')
    expected = set(sessions)
    actual = {(b.instrument, b.trading_date) for b in bars}
    if expected != actual:
        raise ValueError('Snapshot does not cover configured daily sessions exactly')
    frames = {}
    for bar in bars:
        start, end, _ = sessions[bar.instrument, bar.trading_date]
        if (bar.start, bar.end) != (start, end):
            raise ValueError('Snapshot/session timestamp mismatch')
        frame = frames.setdefault(end, dict(time=end, bars=[], open_instruments=[], settle_instruments=[], settlements=[]))
        frame['bars'].append(asdict(bar))
        frame['open_instruments'].append(bar.instrument)
        frame['settle_instruments'].append(bar.instrument)
        if instruments[bar.instrument].asset == Asset.FUTURE:
            frame['settlements'].append(dict(instrument=bar.instrument, trading_date=bar.trading_date, settlement_price=bar.settlement_price))
    if config.get('accounting'):
        raise ValueError('Automatic daily accounting calendar is not implemented; use explicit core Frames for accounting events')
    cfg = {k: v for k, v in config.items() if k not in ('sessions', 'symbols', 'frames')}
    cfg.update(frames=[frames[t] for t in sorted(frames)], data_version=version)
    return json.loads(json.dumps(cfg, default=json_default))


def run(config, root, version, on_bar=None):
    cfg = scenario(config, root, version)
    return run_scenario(cfg, on_bar), cfg
