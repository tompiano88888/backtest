"""Explicit opening-clock policy; signals and ownership come from the caller."""
from dataclasses import dataclass
from datetime import timedelta


@dataclass(frozen=True)
class LockScope:
    instrument: str
    long_group: str
    short_group: str


class OpeningUnlockPolicy:
    """Observe the explicit first minute, then submit a reusable paired release.

    Call after evaluating that minute's opening signals, before routing new orders.
    An empty signal set is an explicit no-opening-signal declaration. Auctions,
    missing first Bars and later Bars cannot substitute for the first minute.
    """

    def __init__(self):
        self.decisions = []
        self._seen = set()
        self._active = {}

    @property
    def pending_instruments(self):
        return frozenset(scope.instrument for scope, op in self._active.items()
                         if op.state in ('pending', 'unwinding'))

    def after_bar(self, ctx, *, scopes, opening_signals, first_bar_ends):
        scopes = tuple(scopes)
        signals = frozenset(opening_signals)
        if len(set(scopes)) != len(scopes):
            raise ValueError('Duplicate self-lock scope')
        requests = []
        for scope in scopes:
            end = first_bar_ends.get(scope.instrument)
            if end is None:
                continue
            if end.tzinfo is None:
                raise ValueError('First minute clock must be timezone-aware')
            if ctx.time != end or (scope, end) in self._seen:
                continue
            self._seen.add((scope, end))
            bar = ctx.bars.get(scope.instrument)
            reason = None
            if bar is None or bar.end != end or bar.end-bar.start != timedelta(minutes=1):
                reason = 'missing_first_minute'
            elif scope.instrument in signals:
                reason = 'opening_signal'
            if reason is None:
                op = ctx.close_locked(scope.instrument, scope.long_group, scope.short_group)
                if op is not None:
                    self._active[scope] = op
                    requests.append(op)
                    reason = 'release_submitted'
                else:
                    reason = 'no_overlapping_inventory'
            self.decisions.append(dict(time=ctx.time, instrument=scope.instrument,
                                       long_group=scope.long_group, short_group=scope.short_group,
                                       reason=reason))
        return tuple(requests)
