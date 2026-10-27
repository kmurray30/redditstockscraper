"""Price series access and forward-outcome labels.

Everything here works on split- and dividend-adjusted prices (``adjusted_close`` and the
open/high/low scaled by the same factor), so a return over any window is a total return.

Entry timing (point-in-time): a signal built from information up to a *cutoff day* is
traded at the **open of the first bar strictly after the cutoff day** (plus slippage). The
position is then measured over fixed horizons. For each horizon the label reports whether
it is **complete** (the whole horizon is observed), **censored** (the data ends first; the
stock may well be alive) or **delisted** (the series stops before the horizon ends:
bankruptcy, acquisition, going dark). Censored rows must never be counted as failures.
A window containing an unexplained >6x one-day move is **bad_data** (see ``clean_series``):
the vendor's raw bars hold bad ticks and unadjusted splits, which otherwise show up as
absurd "multibaggers" (200,000x) that dominate every mean-based statistic.

Delisting assumption: a delisted holding is valued at the last close and the proceeds are
held flat afterwards. For bankruptcies that is generous (the last quote is often above
the eventual recovery); permanent-loss stats should be read with that in mind.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

import numpy as np
import pyarrow.parquet as pq

DAY0 = np.datetime64("1970-01-01", "D")
YEAR_DAYS = 365.25
MULTIPLES = (2, 5, 10)
PERM_LOSS = 0.2  # a holding at or below 20% of entry is a permanent loss (-80%)
SPIKE_UP, SPIKE_DOWN = 4.0, 0.25  # a bar this far from both neighbours, which agree, is a bad tick
BAD_JUMP = 6.0                    # a surviving one-day move this large (up or down) is not trusted
DIV_FACTOR_MIN, DIV_FACTOR_MAX = 0.02, 1.05  # adj/raw once splits are removed: dividends only
MAX_ENTRY_GAP = 7  # days: no bar this soon after the cutoff means "not tradable then"
END_GRACE = 10  # days: a series ending this close to the horizon is not "delisted early"


def to_day(d) -> int:
    """'YYYY-MM-DD' / date / datetime64 -> integer days since 1970-01-01."""
    return int((np.datetime64(str(d)[:10], "D") - DAY0).astype(np.int64))


def to_iso(day: int) -> str:
    return str(DAY0 + np.timedelta64(int(day), "D"))


@dataclass(frozen=True)
class Series:
    code: str
    days: np.ndarray        # int64 days since epoch, ascending
    open: np.ndarray        # adjusted
    high: np.ndarray        # adjusted
    low: np.ndarray         # adjusted
    close: np.ndarray       # adjusted (total return)
    raw_close: np.ndarray   # as quoted at the time
    volume: np.ndarray      # EODHD's: adjusted for every split up to the download date
    bad_days: np.ndarray = None  # days of unexplained >6x one-day moves (see clean_series)
    split_factor: np.ndarray = None  # per bar, product of 1/ratio over later splits; None = no split data

    def __post_init__(self):
        if self.bad_days is None:
            object.__setattr__(self, "bad_days", np.empty(0, dtype=np.int64))

    @property
    def close_adj_over_raw(self) -> np.ndarray:
        with np.errstate(divide="ignore", invalid="ignore"):
            return np.where(self.raw_close > 0, self.close / self.raw_close, np.nan)

    def __len__(self):
        return len(self.days)

    @property
    def last_day(self) -> int:
        return int(self.days[-1])

    def entry_index(self, earliest_day: int) -> int | None:
        """Index of the first bar on or after ``earliest_day``, if one exists within
        MAX_ENTRY_GAP days (otherwise the stock was not trading then)."""
        i = int(np.searchsorted(self.days, earliest_day, side="left"))
        if i >= len(self.days) or self.days[i] - earliest_day > MAX_ENTRY_GAP:
            return None
        return i

    def index_on_or_before(self, day: int) -> int | None:
        i = int(np.searchsorted(self.days, day, side="right")) - 1
        return i if i >= 0 else None


def series_from_rows(code: str, rows: dict) -> Series | None:
    if not rows["date"]:
        return None
    days = np.array(rows["date"], dtype="datetime64[D]").astype(np.int64)
    raw = np.array(rows["close"], dtype=float)
    adj = np.array(rows["adjusted_close"], dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        f = np.where(raw > 0, adj / raw, np.nan)
    def scaled(name):
        return np.array(rows[name], dtype=float) * f
    s = Series(code, days, scaled("open"), scaled("high"), scaled("low"), adj, raw,
               np.array(rows["volume"], dtype=float))
    keep = np.isfinite(s.close) & (s.close > 0) & np.isfinite(s.open) & (s.open > 0)
    if not keep.all():
        s = Series(code, *(a[keep] for a in (s.days, s.open, s.high, s.low, s.close,
                                             s.raw_close, s.volume)))
    return s if len(s) else None


def _take(s: Series, keep: np.ndarray) -> Series:
    return Series(s.code, *(a[keep] for a in (s.days, s.open, s.high, s.low, s.close,
                                              s.raw_close, s.volume)), bad_days=s.bad_days)


def drop_spikes(s: Series, passes: int = 3) -> tuple[Series, int]:
    """Remove isolated bad ticks: a bar that jumps >4x (or <1/4) away from its neighbours and
    snaps back. Returns the cleaned series and how many bars were dropped."""
    dropped = 0
    for _ in range(passes):
        c = s.close
        if len(c) < 3:
            break
        r = c[1:] / c[:-1]
        up = (r[:-1] > SPIKE_UP) & (r[1:] < SPIKE_DOWN)
        down = (r[:-1] < SPIKE_DOWN) & (r[1:] > SPIKE_UP)
        bad = np.nonzero(up | down)[0] + 1
        if not len(bad):
            break
        keep = np.ones(len(c), dtype=bool)
        keep[bad] = False
        s, dropped = _take(s, keep), dropped + len(bad)
    return s, dropped


def fix_unadjusted_splits(s: Series, splits: list[tuple[int, float]]) -> tuple[Series, int]:
    """EODHD sometimes leaves a split unadjusted in ``adjusted_close``, so the price jumps by
    the split ratio. For each known split whose ratio matches a jump in the series within a
    few bars of its date, rescale everything before the jump so the series is continuous."""
    fixed = 0
    o, h, l, c = s.open.copy(), s.high.copy(), s.low.copy(), s.close.copy()
    for day, ratio in sorted(splits):
        if ratio <= 0 or abs(ratio - 1) < 1e-6:
            continue
        lo = max(int(np.searchsorted(s.days, day, side="left")) - 2, 1)
        for j in range(lo, min(lo + 6, len(c))):
            move = c[j] / c[j - 1]
            if (move < 0.77 or move > 1.3) and 0.7 < move * ratio < 1.3:
                for a in (o, h, l, c):
                    a[:j] *= move
                fixed += 1
                break
    return Series(s.code, s.days, o, h, l, c, s.raw_close, s.volume, s.bad_days), fixed


def inconsistent_adjustment_days(s: Series, splits: list[tuple[int, float]]) -> np.ndarray:
    """Days whose ``adjusted_close / close`` is not explained by splits and dividends.

    Splits pull the ratio to the product of 1/ratio over later splits; what remains is the
    dividend adjustment, which can only shrink the price, and rarely below a few percent.
    A ratio outside [0.02, 1.05] after removing the splits means the vendor's adjustment (or
    the raw price) is corrupt for that bar (seen: a 1990s-2020 utility whose 2015 bars read
    $1.05 with an adjustment factor of 0.0017). Needs the split list: without one, no check."""
    if splits is None:
        return np.empty(0, dtype=np.int64)
    f = s.close_adj_over_raw
    # cumulative split factor in force at each bar: product of 1/ratio over splits after it
    left = f / split_factors(s.days, splits)
    bad = ~((left >= DIV_FACTOR_MIN) & (left <= DIV_FACTOR_MAX))
    return s.days[bad].astype(np.int64)


def mark_bad_days(s: Series, extra: np.ndarray | None = None) -> Series:
    """Record days with an unexplained one-day move beyond BAD_JUMP; outcomes whose window
    contains one are labelled bad_data instead of trusted."""
    if len(s) < 2:
        return s
    r = s.close[1:] / s.close[:-1]
    bad = s.days[1:][(r > BAD_JUMP) | (r < 1 / BAD_JUMP)].astype(np.int64)
    if extra is not None and len(extra):
        bad = np.union1d(bad, extra)
    return Series(s.code, s.days, s.open, s.high, s.low, s.close, s.raw_close, s.volume, bad)


def split_factors(days: np.ndarray, splits: list[tuple[int, float]]) -> np.ndarray:
    """Per bar, the product of 1/ratio over the splits after it (1 after the last split)."""
    sf = np.ones(len(days))
    for day, ratio in splits:
        if ratio > 0:
            sf[days < day] *= 1.0 / ratio
    return sf


def clean_series(s: Series, splits: list[tuple[int, float]] | None = None) -> Series:
    """Drop bad ticks, repair unadjusted splits, and mark whatever can't be trusted. ``splits``
    None means "no split data for this code": the adjustment-consistency check is skipped;
    an empty list means "no splits happened": the check runs against a factor of 1."""
    s, _ = drop_spikes(s)
    if splits:
        s, _ = fix_unadjusted_splits(s, splits)
    s = mark_bad_days(s, inconsistent_adjustment_days(s, splits))
    if splits is not None:
        object.__setattr__(s, "split_factor", split_factors(s.days, splits))
    return s


class PriceStore:
    def __init__(self, eodhd_root: Path, cache: int = 4096):
        self.root = Path(eodhd_root)
        self._load = lru_cache(maxsize=cache)(self._read)
        self._data_end: int | None = None

    def _read(self, code: str) -> Series | None:
        path = self.root / "eod" / (quote(code, safe="") + ".parquet")
        if not path.exists():
            return None
        t = pq.read_table(path, columns=["date", "open", "high", "low", "close",
                                         "adjusted_close", "volume"])
        s = series_from_rows(code, t.to_pydict())
        if s is None:
            return None
        return clean_series(s, self._splits(code))

    def has_splits(self, code: str) -> bool:
        return (self.root / "splits" / (quote(code, safe="") + ".parquet")).exists()

    def _splits(self, code: str) -> list[tuple[int, float]] | None:
        path = self.root / "splits" / (quote(code, safe="") + ".parquet")
        if not path.exists():
            return None  # never pulled: skip the adjustment-consistency check
        t = pq.read_table(path).to_pydict()
        return [(to_day(d), r) for d, r in zip(t["date"], t["ratio"]) if r]

    def get(self, code: str | None) -> Series | None:
        return self._load(code) if code else None

    @property
    def data_end(self) -> int:
        """Last day any series has a bar: horizons reaching past it are censored."""
        if self._data_end is None:
            idx = pq.read_table(self.root / "eod_index.parquet", columns=["last_date"])
            self._data_end = to_day(max(idx.column("last_date").to_pylist()))
        return self._data_end


# ---------------------------------------------------------------- outcomes

@dataclass(frozen=True)
class Outcome:
    status: str                 # complete | censored | delisted | bad_data
    ret: float                  # terminal / entry - 1 (at horizon end, last close, or data end)
    cagr: float | None
    excess: float | None        # ret minus benchmark return over the same window
    max_mult: float             # best close / entry within the window
    min_mult: float             # worst close / entry within the window
    max_dd: float               # worst peak-to-trough decline on closes since entry (<= 0)
    days_to: dict               # {2: days or None, 5: ..., 10: ...} to first close >= k x entry
    perm_loss: bool             # terminal <= 20% of entry and the outcome is final
    entry_day: int
    end_day: int


def _terminal_index(s: Series, entry_i: int, horizon_end: int) -> int:
    return max(int(np.searchsorted(s.days, horizon_end, side="right")) - 1, entry_i)


def outcome(s: Series, entry_i: int, years: float, data_end: int, *,
            bench: Series | None = None, slippage_bps: float = 25.0) -> Outcome:
    """Forward outcome of buying at the open of bar ``entry_i`` and looking ``years`` ahead."""
    entry_day = int(s.days[entry_i])
    entry_px = s.open[entry_i] * (1 + slippage_bps / 1e4)
    horizon_end = entry_day + int(round(years * YEAR_DAYS))
    last_i = _terminal_index(s, entry_i, min(horizon_end, data_end))
    ended_early = s.last_day < min(horizon_end, data_end) - END_GRACE
    if horizon_end > data_end:
        status = "delisted" if ended_early else "censored"
    else:
        status = "delisted" if ended_early else "complete"
    closes = s.close[entry_i:last_i + 1]
    mult = closes / entry_px
    terminal = s.close[last_i] * (1 - slippage_bps / 1e4)
    ret = terminal / entry_px - 1
    peak = np.maximum.accumulate(np.concatenate(([1.0], mult)))[1:]
    max_dd = float(np.min(mult / peak - 1))
    days_to = {}
    for k in MULTIPLES:
        hit = np.nonzero(mult >= k)[0]
        days_to[k] = int(s.days[entry_i + hit[0]] - entry_day) if len(hit) else None
    end_day = int(s.days[last_i])
    elapsed = max(end_day - entry_day, 1) / YEAR_DAYS
    cagr = (1 + ret) ** (1 / elapsed) - 1 if elapsed >= 0.25 and ret > -1 else None
    excess = None
    if bench is not None:
        b0, b1 = bench.index_on_or_before(entry_day), bench.index_on_or_before(end_day)
        if b0 is not None and b1 is not None:
            excess = ret - (bench.close[b1] / bench.close[b0] - 1)
    if len(s.bad_days) and ((s.bad_days >= entry_day) & (s.bad_days <= end_day)).any():
        status = "bad_data"   # an unexplained >6x one-day move inside the window: do not trust
    final = status not in ("censored", "bad_data")
    return Outcome(status, float(ret), None if cagr is None else float(cagr), excess,
                   float(mult.max()), float(mult.min()), max_dd, days_to,
                   bool(final and terminal / entry_px <= PERM_LOSS), entry_day, end_day)


# ---------------------------------------------------------------- features at a date

def dollar_volume(s: Series) -> np.ndarray:
    """Dollar volume per bar as traded at the time, with no knowledge of later splits.

    EODHD's volume is adjusted for every split up to the download date (checked 2026-10-27 by
    ``volume_check.py``: share volume shows no jump across forward or reverse splits), so the
    raw close times it is wrong by the later splits' ratio: AMZN in July 2020 read $207B a day
    instead of about $10B, and a company that later reverse-split 1:20 read 20 times too small.
    Winners split and dying companies reverse-split, so that was look-ahead in the eligibility
    floor and the liquidity quintiles. The adjusted close times the volume is consistent across
    splits; it still carries the dividend adjustment, which is divided out where the split data
    explains the adjustment (dividend factor in [0.2, 1.05]); elsewhere it is left in."""
    base = s.close * s.volume
    if s.split_factor is None:
        return base
    with np.errstate(divide="ignore", invalid="ignore"):
        div = s.close_adj_over_raw / s.split_factor
    ok = np.isfinite(div) & (div >= 0.2) & (div <= DIV_FACTOR_MAX)
    return np.where(ok, base / np.where(ok, div, 1.0), base)


def trailing_dollar_volume(s: Series, i: int, n: int = 60) -> float | None:
    """Median dollar volume (``dollar_volume``) over the ``n`` bars up to and including bar ``i``."""
    lo = max(i - n + 1, 0)
    if i - lo + 1 < n // 2:
        return None
    return float(np.median(dollar_volume(s)[lo:i + 1]))


def momentum(s: Series, i: int, lookback: int = 252, skip: int = 21) -> float | None:
    """Return from ``lookback`` bars ago to ``skip`` bars ago (12-1 momentum), as of bar i."""
    a, b = i - lookback, i - skip
    if a < 0 or b <= a:
        return None
    return float(s.close[b] / s.close[a] - 1)
