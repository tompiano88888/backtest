from datetime import date, datetime, time, timedelta
import hashlib
import json

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from backtest.accounting import Ledger
from backtest.engine import Engine, ExecutionConfig, Frame
from backtest.ifind import IFinDError
from backtest.ifind_minute import MinuteSnapshot, normalize_day, session_minute_ends
from backtest.ifind_normalize import SessionCalendar, SHANGHAI
from backtest.models import Account, Asset, D, Instrument, Instruction, MarginRule


DAY = date(2020, 1, 2)


def stamp(value, day=DAY):
    return datetime.combine(day, time.fromisoformat(value), SHANGHAI)


def calendar(codes=('E', 'F', 'I'), day=DAY):
    return SessionCalendar([dict(instrument=c, trading_date=str(day),
                                 start=stamp(a, day).isoformat(), end=stamp(b, day).isoformat())
                            for c in codes for a, b in [('09:30', '09:34'), ('13:00', '13:02')]])


def row(code='E', at='09:31', volume='1000', price='10', day=DAY):
    return dict(instrument=code, source_timestamp=stamp(at, day).replace(tzinfo=None),
                open=price, high=price, low=price, close=price, volume=volume, amount='10000')


def normalized(rows, code='E', asset='fund', multiplier='1', **kwargs):
    return normalize_day(rows, code, DAY, profile=dict(id=code, asset=asset, volume_multiplier=multiplier),
                         calendar=calendar(), timestamp_at='end', **kwargs)


def test_explicit_units_boundary_and_missing_clock():
    data = normalized([row(at='09:30'), row(volume='12')], multiplier='100',
                      observation_times=(time(9, 30),))
    assert list(data.bars) == [stamp('09:31')]
    bar = data.bars[stamp('09:31')]
    assert bar.start == stamp('09:30') and bar.volume == 1200
    assert list(data.observations) == [stamp('09:30')]
    assert stamp('09:32') in data.missing_ends
    assert stamp('12:00') not in session_minute_ends(calendar(), 'E', DAY)


def test_optional_turnover_mapping_has_explicit_units_and_preserves_missing():
    profile=dict(id='E',asset='fund',volume_multiplier='100',turnover_multiplier='1000')
    values=[dict(row(volume='10'),amount='10'),dict(row(at='09:32',volume='10'),amount=None)]
    data=normalize_day(values,'E',DAY,profile=profile,calendar=calendar(),timestamp_at='end',turnover_column='amount')
    assert data.bars[stamp('09:31')].turnover==10000
    assert data.bars[stamp('09:31')].volume==1000
    assert data.bars[stamp('09:32')].turnover is None


def test_index_fractional_volume_remains_reference_only():
    raw = row('I', volume='12.3456789')
    data = normalized([raw], 'I', 'index')
    assert data.bars[stamp('09:31')].volume == 0
    assert raw['volume'] == '12.3456789'
    inst = Instrument('I', Asset.INDEX, Account.SECURITY, 'X', 'X')
    engine = Engine(Ledger({'I': inst}, 100), {})
    with pytest.raises(ValueError, match='Unsupported trade'):
        engine.submit([Instruction('I', 1, 1)], stamp('09:30'))


@pytest.mark.parametrize('rows', [[row(), row()], [row(at='12:00')], [row(volume='.5')]])
def test_invalid_continuous_rows_fail(rows):
    with pytest.raises((ValueError, IFinDError)):
        normalized(rows)


def test_timestamp_interpretation_is_required():
    with pytest.raises(IFinDError):
        normalize_day([row()], 'E', DAY, profile=dict(id='E', asset='fund', volume_multiplier='1'),
                      calendar=calendar(), timestamp_at=None)


def published(tmp_path, rows=None, *, no_quote=False, bad_path=False):
    records = [row()] if rows is None else rows
    path = tmp_path / 'part.parquet'
    pq.write_table(pa.Table.from_pylist(records), path)
    item = dict(instrument='E', path='../outside.parquet' if bad_path else 'part.parquet',
                rows=len(records), first='2020-01-02 09:31', last='2020-01-02 09:31',
                sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    report = dict(start=str(DAY), end='2020-01-03', manifest=[item])
    if no_quote:
        report['no_quote_dates'] = [dict(instrument='E', date='2020-01-03', reason='suspended', source='fixture')]
    version = hashlib.sha256(json.dumps(report, sort_keys=True).encode()).hexdigest()
    with duckdb.connect(str(tmp_path / 'metadata.duckdb')) as db:
        db.execute('CREATE TABLE index_etf_minute_snapshots(version VARCHAR, report JSON)')
        db.execute('INSERT INTO index_etf_minute_snapshots VALUES (?,?)', [version, json.dumps(report)])
    return MinuteSnapshot(tmp_path, version, dataset='index_etf'), path


def test_pinned_snapshot_and_whole_day_gap(tmp_path):
    source, _ = published(tmp_path, no_quote=True)
    assert len(source.rows('E', DAY)) == 1
    assert source.rows('E', date(2020, 1, 3)) == []
    with pytest.raises(ValueError, match='absent'):
        source.rows('UNKNOWN', DAY)
    with pytest.raises(ValueError, match='outside'):
        source.rows('E', date(2020, 1, 4))


def test_undeclared_missing_day_fails(tmp_path):
    source, _ = published(tmp_path)
    with pytest.raises(ValueError, match='Undeclared'):
        source.rows('E', date(2020, 1, 3))


@pytest.mark.parametrize('bad_path', [False, True])
def test_partition_integrity_and_path_are_checked(tmp_path, bad_path):
    source, path = published(tmp_path, bad_path=bad_path)
    if not bad_path:
        path.write_bytes(b'corrupted')
    with pytest.raises(ValueError, match='path/checksum'):
        source.rows('E', DAY)


def engine():
    instruments = {
        'E': Instrument('E', Asset.FUND, Account.SECURITY, 'X', 'X', lot=100, sell_delay_days=1),
        'F': Instrument('F', Asset.FUTURE, Account.FUTURES, 'X', 'X', multiplier=D(10),
                        expiry=date(2020, 1, 31), delivery=date(2020, 1, 31))}
    # Intentionally too little cash: the framework still fills the requested size.
    return Engine(Ledger(instruments, 1), {'F': MarginRule('future', D('.1'))},
                  ExecutionConfig(participation=D('.1')))


def test_minute_partial_remainder_and_shared_liquidity_manual_ledger():
    e = engine()
    cal = calendar()
    series = {}
    for code, asset, volume in [('E', 'fund', '1000'), ('F', 'future', '10')]:
        series[code] = normalize_day([row(code, at=at, volume=volume) for at in ('09:31', '09:32')],
                                    code, DAY, profile=dict(id=code, asset=asset, volume_multiplier='1'),
                                    calendar=cal, timestamp_at='end').bars
    group = e.submit([Instruction('E', 1, 200, group='pair'), Instruction('F', -1, 1, group='pair')],
                     stamp('09:30'), combination=True)
    competing = e.submit([Instruction('E', 1, 100, group='other')], stamp('09:30'))
    for at in ('09:31', '09:32'):
        t = stamp(at)
        e.step(Frame(t, {c: s[t] for c, s in series.items()}, frozenset(series)))
    assert group.state == 'completed' and group.attempts == 2
    assert competing.state == 'pending'
    assert [f.instruction.quantity for f in e.ledger.fills if f.instruction.instrument == 'E'] == [100, 100]
    # 1 - 200*10 = -1999 cash; 200*10 asset; future unchanged; equity=1.
    snap = e.ledger.snapshot(e.margin_rules)
    assert snap.cash == D(-1999) and snap.equity == D(1) and snap.margin == D(10)
    assert snap.funding_gap == D(2009)
    assert all(f.time < stamp('09:32') for f in e.ledger.fills)


def test_missing_three_common_minutes_exit_fourth_then_etf_t1():
    e = engine()
    cal = calendar()
    bars = normalize_day([row(at=at) for at in ('09:31', '09:32', '09:33', '09:34')],
                         'E', DAY, profile=dict(id='E', asset='fund', volume_multiplier='1'),
                         calendar=cal, timestamp_at='end').bars
    op = e.submit([Instruction('E', 1, 100, group='pair'), Instruction('F', -1, 1, group='pair')],
                  stamp('09:30'), combination=True)
    for at in ('09:31', '09:32', '09:33'):
        t = stamp(at)
        e.step(Frame(t, {'E': bars[t]}, frozenset({'E', 'F'})))
    assert op.attempts == 3 and op.state == 'unwinding'
    assert len(e.ledger.fills) == 1  # no fabricated future; no third-Bar rollback
    e.step(Frame(stamp('09:34'), {'E': bars[stamp('09:34')]}, frozenset({'E', 'F'})))
    assert len(e.ledger.fills) == 1  # T+1 blocks same-day cleanup
    next_day = date(2020, 1, 3)
    data = normalize_day([row(day=next_day)], 'E', next_day,
                         profile=dict(id='E', asset='fund', volume_multiplier='1'),
                         calendar=calendar(day=next_day), timestamp_at='end')
    e.step(Frame(stamp('09:31', next_day), {'E': data.bars[stamp('09:31', next_day)]}, frozenset({'E'})))
    assert op.state == 'unwound' and not e.ledger.lots
    assert e.ledger.snapshot(e.margin_rules).pnl == 0


def test_callback_uses_completed_minute_then_next_open():
    e = engine()
    data = normalized([row(at=at) for at in ('09:31', '09:32')])
    def on_bar(ctx):
        if ctx.time == stamp('09:31'):
            ctx.submit([Instruction('E', 1, 100)])
            assert not ctx.positions
    e.step(Frame(stamp('09:31'), {'E': data.bars[stamp('09:31')]}, frozenset({'E'})), on_bar=on_bar)
    assert not e.ledger.fills
    e.step(Frame(stamp('09:32'), {'E': data.bars[stamp('09:32')]}, frozenset({'E'})))
    assert e.ledger.fills[0].time == stamp('09:31')


def test_filled_future_exits_on_fourth_minute_and_accounts_loss():
    e = engine()
    data = normalize_day([row('F', at=at, volume='10', price=price)
                          for at, price in [('09:31','10'), ('09:32','10'), ('09:33','10'), ('09:34','11')]],
                         'F', DAY, profile=dict(id='F', asset='future', volume_multiplier='1'),
                         calendar=calendar(), timestamp_at='end')
    op = e.submit([Instruction('F', -1, 1, group='pair'), Instruction('E', 1, 100, group='pair')],
                  stamp('09:30'), combination=True)
    for at in ('09:31','09:32','09:33'):
        t = stamp(at)
        e.step(Frame(t, {'F': data.bars[t]}, frozenset({'F','E'})))
    assert len(e.ledger.fills) == 1 and op.state == 'unwinding'
    e.step(Frame(stamp('09:34'), {'F': data.bars[stamp('09:34')]}, frozenset({'F'})))
    assert e.ledger.fills[-1].time == stamp('09:33')
    assert op.state == 'unwound' and not e.ledger.lots
    # Short 1 contract at 10, buy back at 11, multiplier 10: loss 10.
    snap = e.ledger.snapshot(e.margin_rules)
    assert snap.cash == D(-9) and snap.pnl == D(-10) and snap.margin == 0
