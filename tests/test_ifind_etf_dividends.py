from datetime import date, datetime, timezone
from decimal import Decimal
import copy

import pytest

from backtest.ifind_etf_dividends import COUNT, UNIT, PLAN, DATES, event, publish, read_events
from backtest.storage import Store


def history():
    record = dict(thscode='510300.SH', **{COUNT: 1, UNIT: '0.123', PLAN: '每10份基金份额派发红利1.23元'})
    dates = ['20260112', '20260116', '20260127', '20260119', '20260119', '20251231']
    record.update(dict(zip(DATES.values(), dates)))
    return {'510300.SH': [dict(query_date='2026-10-12', observed_at='2026-10-12T12:00:00+08:00', record=record),
                         dict(query_date='2026-01-11', observed_at='2026-10-12T12:01:00+08:00', record={'thscode': '510300.SH'})]}


def test_units_dates_snapshot_pit_and_repeat(tmp_path):
    h = history()
    with Store(tmp_path) as store:
        version, report = publish(store, h, date(2026, 10, 12))
        assert report['events'] == 1
        item = read_events(store, version, '510300.SH')[0]
        assert item['cash_per_unit'] * 1000 == Decimal('123')
        assert item['record_date'] == date(2026, 1, 16)
        assert item['exchange_ex_date'] == date(2026, 1, 19)
        assert item['payment_date'] == date(2026, 1, 27)
        assert read_events(store, version, '510300.SH', as_of=datetime(2026, 1, 20, tzinfo=timezone.utc)) == []
        assert publish(store, h, date(2026, 10, 12))[0] == version
        newer = copy.deepcopy(h)
        newer['510300.SH'][0]['record'][UNIT] = '0.124'
        newer['510300.SH'][0]['record'][PLAN] = '每10份基金份额派发红利1.24元'
        new_version, _ = publish(store, newer, date(2026, 10, 12))
        assert read_events(store, new_version, '510300.SH')[0]['cash_per_unit'] == Decimal('0.124')
        assert read_events(store, version, '510300.SH')[0]['cash_per_unit'] == Decimal('0.123')


@pytest.mark.parametrize('field,value', [(COUNT, 2), (UNIT, '0.999'), (PLAN, '未知方案')])
def test_no_publication_on_missing_or_unsupported_data(tmp_path, field, value):
    h = history()
    h['510300.SH'][0]['record'][field] = value
    with Store(tmp_path) as store:
        with pytest.raises(ValueError):
            publish(store, h, date(2026, 10, 12))
        assert store.db.execute('SELECT COUNT(*) FROM metadata').fetchone()[0] == 0


def test_no_dividend_and_duplicate_history(tmp_path):
    h = history()
    h['159001.SZ'] = [dict(query_date='2026-10-12', observed_at='2026-10-12T12:00:00+08:00', record={'thscode': '159001.SZ'})]
    with Store(tmp_path) as store:
        version, report = publish(store, h, date(2026, 10, 12))
        assert report['no_reported_events'] == 1
        assert read_events(store, version, '159001.SZ') == []
        bad = copy.deepcopy(h)
        bad['510300.SH'].insert(1, bad['510300.SH'][0])
        with pytest.raises(ValueError):
            publish(store, bad, date(2026, 10, 12))


def test_multiple_events_reconcile_and_early_plan_format(tmp_path):
    h = history()
    older = copy.deepcopy(h['510300.SH'][0])
    older['query_date'] = '2026-01-11'
    older['record'].update({COUNT: 1, UNIT: '0.062', PLAN: '每10份基金份额派发现金人民币0.62元'})
    older['record'].update(dict(zip(DATES.values(), ['20250112', '20250116', '20250127', '20250119', '20250119', '20241231'])))
    h['510300.SH'][0]['record'].update({COUNT: 2, UNIT: '0.185'})
    h['510300.SH'].insert(1, older)
    h['510300.SH'][-1]['query_date'] = '2025-01-11'
    with Store(tmp_path) as store:
        version, report = publish(store, h, date(2026, 10, 12))
        assert report['events'] == 2
        assert sum(x['cash_per_unit'] for x in read_events(store, version, '510300.SH')) * 1000 == Decimal('185')


@pytest.mark.parametrize('plan', ['每10份基金份额派发现金红利0.20元', '每10份基金份额发放红利0.2000元', '每10份基金派发红利0.2元', '每10份基金份额派发现金收益0.20元', '每10份基金份额派发现金红利0.2元.', '每10份基金单位派0.2元'])
def test_observed_early_plan_formats(plan):
    record = history()['510300.SH'][0]['record']
    record[PLAN] = plan
    assert event(record)['cash_per_unit'] == Decimal('0.02')


def test_announced_future_event_is_preserved_without_counting_completed_total(tmp_path):
    h = history()
    row = h['510300.SH'][0]['record']
    row.update({COUNT: 0, UNIT: '0'})
    row.update(dict(zip(DATES.values(), ['20261009', '20261013', '20261016', '20261014', '20261014', '20260923'])))
    h['510300.SH'][-1]['query_date'] = '2026-10-08'
    with Store(tmp_path) as store:
        version, report = publish(store, h, date(2026, 10, 12))
        assert report['announced_future_events'] == 1
        assert report['coverage']['510300.SH']['completed_events'] == 0
        assert read_events(store, version, '510300.SH')[0]['exchange_ex_date'] == date(2026, 10, 14)
