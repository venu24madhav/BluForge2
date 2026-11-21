"""Delay and jitter statistics that stay exact where it matters and bounded in memory for a run of any length.

A run can last hours (tens of thousands of media packets per hour, per link), so the whole-run figures are kept in a sparse
histogram instead of a list: 0.1 ms resolution up to 1 s, 1 ms resolution beyond, an exact minimum, maximum, sum and count
beside it. The per-interval figures (one value per sample row) are small and are computed from the exact values.

Definitions (all delays are in milliseconds):
  delay        time one packet needed (here: first fragment handed to the controller -> last fragment completed)
  jitter       delay variation between consecutive packets, |delay[i] - delay[i-1]|  (RFC 3393 IPDV, absolute value);
               "avg" is its mean, "p95" its 95th percentile, "rfc3550" the smoothed estimator of RFC 3550 section 6.4.1
  percentile   nearest rank (the smallest value that at least p % of the observations do not exceed); from the histogram it is
               the middle of its 0.1 ms bin (1 ms bin above 1 s), never outside the exact min..max
"""
import math

FINE_US = 100                  # histogram bin below COARSE_FROM_US
COARSE_FROM_US = 1000000
COARSE_US = 1000
_COARSE_BASE = COARSE_FROM_US // FINE_US
MAX_US = 3600 * 1000000        # a "delay" beyond an hour is a bookkeeping error, not a measurement


def _key(us):
    if us < COARSE_FROM_US:
        return int(us // FINE_US)
    return _COARSE_BASE + int((min(us, MAX_US) - COARSE_FROM_US) // COARSE_US)


def _mid_ms(key):
    if key < _COARSE_BASE:
        return (key * FINE_US + FINE_US / 2.0) / 1000.0
    return (COARSE_FROM_US + (key - _COARSE_BASE) * COARSE_US + COARSE_US / 2.0) / 1000.0


def percentile_exact(sorted_vals, p):
    """Nearest-rank percentile of an already sorted list (None when empty)."""
    if not sorted_vals:
        return None
    rank = max(1, int(math.ceil(p / 100.0 * len(sorted_vals))))
    return sorted_vals[min(rank, len(sorted_vals)) - 1]


class Hist:
    """Sparse histogram of non-negative values in ms with exact count, sum, min and max."""

    def __init__(self):
        self.bins = {}
        self.n = 0
        self.total = 0.0
        self.lo = None
        self.hi = None

    def add(self, ms):
        us = ms * 1000.0
        if us < 0 or us != us:               # negative or NaN: caller bug, never silently stored
            raise ValueError("delay must be a non-negative number, got %r" % (ms,))
        k = _key(us)
        self.bins[k] = self.bins.get(k, 0) + 1
        self.n += 1
        self.total += ms
        if self.lo is None or ms < self.lo:
            self.lo = ms
        if self.hi is None or ms > self.hi:
            self.hi = ms

    def percentile(self, p):
        if not self.n:
            return None
        rank = max(1, int(math.ceil(p / 100.0 * self.n)))
        seen = 0
        for k in sorted(self.bins):
            seen += self.bins[k]
            if seen >= rank:
                return min(max(_mid_ms(k), self.lo), self.hi)
        return self.hi


def _r(x, nd=3):
    return None if x is None else round(x, nd)


class SignedHist:
    """Histogram of values that may be negative (a difference of two timestamps), same bins as Hist."""

    def __init__(self):
        self.pos = Hist()
        self.neg = Hist()                    # holds |value| of the negative ones

    @property
    def n(self):
        return self.pos.n + self.neg.n

    def add(self, ms):
        if ms < 0:
            self.neg.add(-ms)
        else:
            self.pos.add(ms)

    def _values(self):
        """[(value_ms, count)] in ascending order, one entry per bin."""
        out = [(-min(max(_mid_ms(k), self.neg.lo), self.neg.hi), c) for k, c in self.neg.bins.items()]
        out.sort()
        out.extend((min(max(_mid_ms(k), self.pos.lo), self.pos.hi), c) for k, c in sorted(self.pos.bins.items()))
        return out

    def percentile(self, p):
        if not self.n:
            return None
        rank = max(1, int(math.ceil(p / 100.0 * self.n)))
        seen = 0
        for v, c in self._values():
            seen += c
            if seen >= rank:
                return v
        return None

    def summary(self):
        if not self.n:
            return None
        lo = -self.neg.hi if self.neg.n else self.pos.lo
        hi = self.pos.hi if self.pos.n else -self.neg.lo
        return {"n": self.n, "min": _r(lo), "p50": _r(self.percentile(50)), "p95": _r(self.percentile(95)), "max": _r(hi)}


class DelayAcc:
    """Whole-run and per-interval delay + jitter of one packet stream. Per-interval values are only kept when `intervals`
    is true (and then only until take_interval() is called), so a long run that never asks for them uses constant memory."""

    def __init__(self, late_limit_ms=None, intervals=True):
        self.keep_intervals = intervals
        self.delay = Hist()
        self.diff = Hist()
        self.prev = None
        self.rfc = 0.0                       # RFC 3550 smoothed jitter
        self.late_limit_ms = late_limit_ms
        self.late = 0
        self.bytes = 0
        self.ibytes = 0
        self.ival = []                       # delays since the last take_interval()
        self.idiff = []

    def add(self, ms, nbytes=0):
        self.delay.add(ms)
        self.bytes += nbytes
        self.ibytes += nbytes
        if self.keep_intervals:
            self.ival.append(ms)
        if self.late_limit_ms is not None and ms > self.late_limit_ms:
            self.late += 1
        if self.prev is not None:
            d = abs(ms - self.prev)
            self.diff.add(d)
            if self.keep_intervals:
                self.idiff.append(d)
            self.rfc += (d - self.rfc) / 16.0
        self.prev = ms

    @property
    def n(self):
        return self.delay.n

    def take_interval(self):
        """Statistics of what was added since the previous call (None if nothing), then start a new interval."""
        v, d = sorted(self.ival), self.idiff
        self.ival, self.idiff, self.ibytes = [], [], 0
        if not v:
            return None
        sd = sorted(d)
        return {"n": len(v), "avg": _r(sum(v) / len(v)), "p50": _r(percentile_exact(v, 50)),
                "p95": _r(percentile_exact(v, 95)), "max": _r(v[-1]),
                "jitter_avg": _r(sum(d) / len(d)) if d else None, "jitter_p95": _r(percentile_exact(sd, 95)) if d else None}

    def summary(self):
        h = self.delay
        if not h.n:
            return None
        out = {"n": h.n, "min": _r(h.lo), "avg": _r(h.total / h.n), "p50": _r(h.percentile(50)),
               "p95": _r(h.percentile(95)), "p99": _r(h.percentile(99)), "max": _r(h.hi)}
        jit = None
        if self.diff.n:
            jit = {"avg": _r(self.diff.total / self.diff.n), "p95": _r(self.diff.percentile(95)),
                   "max": _r(self.diff.hi), "rfc3550": _r(self.rfc), "n": self.diff.n}
        return {"delay_ms": out, "jitter_ms": jit,
                "late": ({"limit_ms": self.late_limit_ms, "n": self.late} if self.late_limit_ms is not None else None)}
