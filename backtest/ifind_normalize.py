"""Validated iFinD records -> standard Bars, using explicit trading sessions."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .ifind import IFinDConfigurationError, IFinDError
from .models import Asset, Bar, D

SHANGHAI = ZoneInfo("Asia/Shanghai")
PRICE_FIELDS = {"open", "high", "low", "close", "settlement_price", "upper_limit", "lower_limit", "turnover", "open_interest"}


def _timestamp(value, *, aware_required=False):
    try:
        stamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    except ValueError:
        raise IFinDError("Invalid iFinD timestamp") from None
    if stamp.tzinfo is None:
        if aware_required:
            raise IFinDConfigurationError("Calendar sessions require timezone-aware boundaries")
        stamp = stamp.replace(tzinfo=SHANGHAI)
    return stamp.astimezone(SHANGHAI)


@dataclass(frozen=True)
class Session:
    instrument: str
    trading_date: date
    start: datetime
    end: datetime


class SessionCalendar:
    """Calendar facts, not a weekday/night-date heuristic."""

    def __init__(self, rows):
        self.by_instrument = {}
        self.by_day = {}
        for row in rows:
            try:
                session = Session(row["instrument"], date.fromisoformat(row["trading_date"]),
                                  _timestamp(row["start"], aware_required=True), _timestamp(row["end"], aware_required=True))
            except (KeyError, TypeError, ValueError):
                raise IFinDConfigurationError("Invalid calendar session record") from None
            if not session.instrument or session.start >= session.end:
                raise IFinDConfigurationError("Invalid calendar session interval")
            self.by_instrument.setdefault(session.instrument, []).append(session)
            self.by_day.setdefault((session.instrument, session.trading_date), []).append(session)
        for sessions in self.by_instrument.values():
            sessions.sort(key=lambda s: s.start)
            if any(a.end > b.start for a, b in zip(sessions, sessions[1:])):
                raise IFinDConfigurationError("Calendar sessions overlap")

    def trading_day(self, instrument, start, end):
        for session in self.by_instrument.get(instrument, ()):
            if session.start <= start and end <= session.end:
                return session.trading_date
        raise IFinDError(f"No containing trading session for {instrument}; do not infer night-session dates")

    def day_bounds(self, instrument, day):
        sessions = self.by_day.get((instrument, day))
        if not sessions:
            raise IFinDError(f"Missing daily trading sessions for {instrument}")
        return min(s.start for s in sessions), max(s.end for s in sessions)


class IFinDBarNormalizer:
    def __init__(self, config, frequency, calendar: SessionCalendar):
        self.frequency, self.calendar = frequency, calendar
        self.instruments = config["instruments"]
        self.columns = config["columns"]
        self.edge = config.get("timestamp_at")
        if frequency not in ("1m", "1d") or frequency == "1m" and self.edge not in ("start", "end"):
            raise IFinDConfigurationError("Minute bars require explicit timestamp_at=start or end")
        if not {"open", "high", "low", "close", "volume"} <= self.columns.keys():
            raise IFinDConfigurationError("Map all OHLCV fields explicitly")
        if set(self.columns) - PRICE_FIELDS - {"volume", "suspended"}:
            raise IFinDConfigurationError("Unsupported normalized field")
        if frequency == "1m" and "settlement_price" in self.columns:
            raise IFinDConfigurationError("Load settlement prices from daily bars; do not expose daily settlement in minute history")
        for profile in self.instruments.values():
            try:
                Asset(profile["asset"])
                if not profile["id"] or D(profile["volume_multiplier"]) <= 0:
                    raise ValueError()
                if "turnover" in self.columns and D(profile["turnover_multiplier"]) <= 0:
                    raise ValueError()
            except (KeyError, ValueError):
                raise IFinDConfigurationError("Each instrument needs id, asset and positive explicit unit multipliers") from None
            if frequency == "1d" and profile["asset"] in ("future", "option") and "settlement_price" not in self.columns:
                raise IFinDConfigurationError("Daily derivatives require an explicit settlement_price column")

    def __call__(self, row):
        code = row.get("thscode")
        if code not in self.instruments:
            raise IFinDError("Unmapped iFinD security code")
        profile = self.instruments[code]
        instrument = profile["id"]
        if "time" not in row:
            raise IFinDError("Market record has no timestamp")
        stamp = _timestamp(row["time"])
        if self.frequency == "1m":
            if stamp.second or stamp.microsecond:
                raise IFinDError("One-minute timestamp is not minute-aligned")
            start = stamp if self.edge == "start" else stamp - timedelta(minutes=1)
            end = start + timedelta(minutes=1)
            day = self.calendar.trading_day(instrument, start, end)
        else:
            day = stamp.date()
            start, end = self.calendar.day_bounds(instrument, day)
        values = {}
        for target, source in self.columns.items():
            if source not in row:
                raise IFinDError(f"Configured iFinD field is missing: {target}")
            value = row[source]
            if target == "suspended":
                if type(value) is not bool:
                    raise IFinDError("Suspension must be explicitly normalized to boolean")
                values[target] = value
                continue
            if value is None:
                if target in {"open", "high", "low", "close", "volume"} or target in {"settlement_price", "previous_settlement_price"} and profile["asset"] in ("future", "option"):
                    raise IFinDError(f"Required iFinD field is null: {target}")
                values[target] = None
                continue
            try:
                value = D(value)
            except Exception:
                raise IFinDError(f"Invalid numeric iFinD field: {target}") from None
            if target == "volume":
                value *= D(profile["volume_multiplier"])
                if value != int(value):
                    raise IFinDError("Normalized volume is not an integer")
                value = int(value)
            elif target == "turnover":
                value *= D(profile["turnover_multiplier"])
            values[target] = value
        try:
            return Bar(instrument=instrument, start=start, end=end, trading_date=day, **values)
        except (ValueError, TypeError):
            raise IFinDError("Normalized iFinD OHLCV failed validation") from None
