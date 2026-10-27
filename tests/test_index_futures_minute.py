from datetime import date

import pytest

from scripts.download_index_futures_minute import FIELDS, validate


def sample():
    row = dict.fromkeys(FIELDS, '0')
    row.update(thscode='IF2001.CFE', time='2020-01-02 09:30',
               open='4131.2', high='4131.2', low='4131.2', close='4131.2',
               volume='324.0', amount='401552640.0', openInterest='72897.0')
    return row


def test_preserves_auction_timestamp_and_decimal():
    rows = validate([sample(), sample()], 'IF2001.CFE', {date(2020, 1, 2)})
    assert len(rows) == 1
    assert rows[0]['source_timestamp'].strftime('%H:%M') == '09:30'
    assert str(rows[0]['amount']) == '401552640.0'


def test_missing_reference_date_blocks_publication():
    with pytest.raises(ValueError, match='Missing daily-reference'):
        validate([sample()], 'IF2001.CFE', {date(2020, 1, 2), date(2020, 1, 3)})


def test_conflicting_duplicate_rejected():
    other = sample()
    other['volume'] = '325'
    with pytest.raises(ValueError, match='Conflicting'):
        validate([sample(), other], 'IF2001.CFE', {date(2020, 1, 2)})


@pytest.mark.parametrize('field,value', [('volume', '0.5'), ('close', '5000'), ('amount', '-1'), ('openInterest', None)])
def test_bad_vendor_data_rejected(field, value):
    row = sample()
    row[field] = value
    with pytest.raises(ValueError):
        validate([row], 'IF2001.CFE', {date(2020, 1, 2)})


def test_index_fractional_volume_and_missing_amount_are_preserved():
    row = sample()
    row.update(thscode='000905.SH', volume='123.456789', amount=None)
    rows = validate([row], '000905.SH', {date(2020, 1, 2)},
                    fields=('open', 'high', 'low', 'close', 'volume', 'amount'),
                    required=('open', 'high', 'low', 'close'), integer_volume=False)
    assert str(rows[0]['volume']) == '123.456789'
    assert rows[0]['amount'] is None


def test_fund_fractional_volume_is_rejected():
    row = sample()
    row.update(thscode='510300.SH', volume='0.5')
    with pytest.raises(ValueError, match='Noninteger'):
        validate([row], '510300.SH', {date(2020, 1, 2)},
                 fields=('open', 'high', 'low', 'close', 'volume', 'amount'),
                 required=('open', 'high', 'low', 'close'))
