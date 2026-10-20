"""Explicit index-futures 09:30 auction observation adapter."""
from datetime import time
from zoneinfo import ZoneInfo

from .models import Auction, D


def index_futures_auction(row,trading_date,daily_open):
    stamp = row['source_timestamp']
    tz = ZoneInfo('Asia/Shanghai')
    stamp = stamp.replace(tzinfo=tz) if stamp.tzinfo is None else stamp.astimezone(tz)
    price = D(daily_open)
    if stamp.time()!=time(9,30) or stamp.date()!=trading_date:
        raise ValueError('Auction time/date does not match explicit daily reference')
    if any(D(row[k]) != price for k in ('open','high','low','close')):
        raise ValueError('09:30 auction result differs from daily open')
    volume = D(row['volume'])
    if volume != int(volume):
        raise ValueError('Noninteger auction volume')
    return Auction(row['instrument'],stamp,trading_date,price,int(volume))
