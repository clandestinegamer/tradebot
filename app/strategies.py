"""Trading rules. Each strategy looks at candles and says BUY, SELL or HOLD.

Protective exits (stop-loss, trailing stop, break-even, optional take-profit) are handled by
the engine on every tick, so strategies only describe entries and "the idea is over" exits.
The market-wide regime filter also lives in the engine, because it needs a second symbol's data.

Every parameter can be overridden per symbol in config.toml:
    params = { adx_min = 25, fast = 10, slow = 30 }
"""
from __future__ import annotations

import math

import pandas as pd

from .indicators import add_all, rsi, sma
from .models import Position, Signal


def _num(x) -> float:
    try:
        v = float(x)
        return v if math.isfinite(v) else float("nan")
    except (TypeError, ValueError):
        return float("nan")


class Prepared:
    """Indicators computed once over a whole candle history. All indicators are causal
    (each value uses only that candle and earlier ones), so row i is identical whether
    computed on the full history or on history[:i+1] — backtests match live decisions."""

    def __init__(self, df: pd.DataFrame):
        self.index = [str(x) for x in df.index]
        self.cols = {c: df[c].to_numpy(dtype=float) for c in df.columns}

    def __len__(self) -> int:
        return len(self.index)

    def row(self, i: int) -> dict:
        return {c: float(v[i]) for c, v in self.cols.items()}


def _snapshot(row: dict) -> dict:
    return {
        k: round(_num(row[k]), 4)
        for k in ("close", "ema_fast", "ema_slow", "long_ma", "rsi", "atr", "adx", "volume", "vol_avg")
        if k in row and not math.isnan(_num(row[k]))
    }


def _check(params: dict, defaults: dict) -> None:
    unknown = set(params) - set(defaults)
    if unknown:
        raise TypeError(f"unknown parameter(s) {sorted(unknown)}; allowed: {sorted(defaults)}")


def _filtered(reason: str, price: float, atr: float, snap: dict) -> Signal:
    """A real setup that a filter rejected — logged so you can see what the filters are doing."""
    return Signal("HOLD", reason, price, atr, snap, filtered=True)


class TrendRSI:
    """Trend-following with RSI timing, evaluated on CLOSED candles.

    BUY when ALL of:
      • trend up: fast EMA > slow EMA and price above slow EMA
      • long-term trend up: price above the `long_ma`-candle average (0 = off)
      • trend is strong: ADX ≥ adx_min (skips choppy, sideways markets) (0 = off)
      • trigger: fast EMA just crossed above slow EMA, or RSI just recovered above 40
      • not overbought: RSI < 70
      • volume confirms: trigger candle volume ≥ vol_mult × 20-candle average (0 = off)
    SELL when the fast EMA crosses back below the slow EMA, or RSI > 80.
    """

    name = "trend_rsi"
    uses_forming_bar = False
    DEFAULTS = dict(fast=20, slow=50, long_ma=200, adx_min=20.0, vol_mult=1.2, rsi_max=70.0)

    def __init__(self, **params):
        _check(params, self.DEFAULTS)
        p = {**self.DEFAULTS, **params}
        self.fast, self.slow, self.long_ma = int(p["fast"]), int(p["slow"]), int(p["long_ma"])
        self.adx_min, self.vol_mult, self.rsi_max = float(p["adx_min"]), float(p["vol_mult"]), float(p["rsi_max"])
        self.params = p
        self.min_bars = max(self.slow, self.long_ma) + 10

    def prepare(self, candles: pd.DataFrame) -> Prepared:
        return Prepared(add_all(candles, self.fast, self.slow, self.long_ma))

    def evaluate(self, candles: pd.DataFrame, position: Position | None, live_price: float,
                 progress: float | None = None) -> Signal:
        if len(candles) < self.min_bars:
            return Signal("HOLD", f"warming up ({len(candles)}/{self.min_bars} candles)")
        p = self.prepare(candles)
        return self.decide(p, len(p) - 1, position, live_price, progress)

    def decide(self, p: Prepared, i: int, position: Position | None, live_price: float,
               progress: float | None = None) -> Signal:
        if i + 1 < self.min_bars:
            return Signal("HOLD", f"warming up ({i + 1}/{self.min_bars} candles)")
        last, prev = p.row(i), p.row(i - 1)
        snap = _snapshot(last)
        a = _num(last["atr"])
        if any(math.isnan(_num(v)) for v in (last["rsi"], prev["rsi"], a)):
            return Signal("HOLD", "indicators not ready", snapshot=snap)

        cross_up = prev["ema_fast"] <= prev["ema_slow"] and last["ema_fast"] > last["ema_slow"]
        cross_dn = prev["ema_fast"] >= prev["ema_slow"] and last["ema_fast"] < last["ema_slow"]

        if position:
            if cross_dn:
                return Signal("SELL", "trend reversed: fast EMA crossed below slow EMA", live_price, a, snap)
            if last["rsi"] > 80:
                return Signal("SELL", f"RSI {last['rsi']:.0f} > 80, locking in gains", live_price, a, snap)
            return Signal("HOLD", "holding: trend intact", live_price, a, snap)

        uptrend = last["ema_fast"] > last["ema_slow"] and last["close"] > last["ema_slow"]
        rsi_recover = prev["rsi"] < 40 <= last["rsi"]
        if not (uptrend and (cross_up or rsi_recover)):
            return Signal("HOLD", "no setup", live_price, a, snap)
        why = "fast EMA crossed above slow EMA" if cross_up else "RSI recovered above 40 in an uptrend"

        # filters — a setup that fails one is reported, not silently dropped
        if last["rsi"] >= self.rsi_max:
            return _filtered(f"{why}, but RSI {last['rsi']:.0f} is overbought", live_price, a, snap)
        if self.long_ma and not last["close"] > _num(last["long_ma"]):
            return _filtered(f"{why}, but price is below its {self.long_ma}-candle average", live_price, a, snap)
        if self.adx_min and not _num(last["adx"]) >= self.adx_min:
            return _filtered(f"{why}, but trend is weak (ADX {_num(last['adx']):.0f} < {self.adx_min:g})",
                          live_price, a, snap)
        if self.vol_mult and not last["volume"] >= self.vol_mult * _num(last["vol_avg"]):
            return _filtered(f"{why}, but volume is light ({last['volume'] / _num(last['vol_avg']):.1f}× avg)",
                          live_price, a, snap)
        return Signal("BUY", f"{why} (RSI {last['rsi']:.0f}, ADX {_num(last['adx']):.0f})", live_price, a, snap)


class VolBreakout:
    """Larry Williams volatility breakout, evaluated on the FORMING candle.

    BUY when price breaks above today's open + k × yesterday's range, yesterday closed above the fast EMA,
        and the candle's volume is on pace for ≥ vol_mult × average (pace = volume ÷ share of candle elapsed).
    SELL at the start of the next candle (one-candle hold) — stops still apply.
    """

    name = "vol_breakout"
    uses_forming_bar = True
    DEFAULTS = dict(k=0.5, fast=20, vol_mult=1.5, min_progress=0.1)

    def __init__(self, **params):
        _check(params, self.DEFAULTS)
        p = {**self.DEFAULTS, **params}
        self.k, self.fast = float(p["k"]), int(p["fast"])
        self.vol_mult, self.min_progress = float(p["vol_mult"]), float(p["min_progress"])
        self.params = p
        self.min_bars = 30

    def prepare(self, candles: pd.DataFrame) -> Prepared:
        return Prepared(add_all(candles, self.fast, max(self.fast + 1, 50)))

    def evaluate(self, candles: pd.DataFrame, position: Position | None, live_price: float,
                 progress: float | None = None) -> Signal:
        if len(candles) < self.min_bars:
            return Signal("HOLD", f"warming up ({len(candles)}/{self.min_bars} candles)")
        p = self.prepare(candles)
        return self.decide(p, len(p) - 1, position, live_price, progress)

    def decide(self, p: Prepared, i: int, position: Position | None, live_price: float,
               progress: float | None = None) -> Signal:
        """Row i is the FORMING candle (its open is known; high/low/close/volume are so far)."""
        if i + 1 < self.min_bars:
            return Signal("HOLD", f"warming up ({i + 1}/{self.min_bars} candles)")
        today, yday = p.row(i), p.row(i - 1)
        snap = _snapshot(yday)
        a = _num(yday["atr"])
        bar_id = p.index[i]

        if position:
            if position.entry_bar and position.entry_bar != bar_id:
                return Signal("SELL", "breakout hold period over (new candle)", live_price, a, snap)
            return Signal("HOLD", "holding breakout", live_price, a, snap)

        rng = float(yday["high"] - yday["low"])
        trigger = float(today["open"]) + self.k * rng
        snap["breakout_level"] = round(trigger, 4)
        if not (rng > 0 and not math.isnan(a) and live_price >= trigger):
            return Signal("HOLD", "no breakout", live_price, a, snap)
        why = f"price broke above {trigger:,.2f} (open + {self.k:g}×range)"
        if not yday["close"] > yday["ema_fast"]:
            return _filtered(f"{why}, but yesterday closed below the fast EMA", live_price, a, snap)
        if self.vol_mult:
            prog = 1.0 if progress is None else max(progress, self.min_progress)
            pace = float(today["volume"]) / prog
            avg = _num(today["vol_avg"])
            snap["volume_pace_x"] = round(pace / avg, 2) if avg else None
            if not (avg and pace >= self.vol_mult * avg):
                return _filtered(f"{why}, but volume pace is light ({pace / avg if avg else 0:.1f}× avg)",
                              live_price, a, snap)
        return Signal("BUY", why, live_price, a, snap)


class DipBuy:
    """Mean reversion ("buy the dip in an uptrend", after Larry Connors' RSI-2), on CLOSED candles.

    Works when markets chop sideways or pull back inside an uptrend — the conditions where trend rules struggle.
    BUY  when the 2-period RSI drops below rsi_entry (a sharp short-term sell-off) while price is still above
         its long_ma-candle average (so it's a dip, not a collapse).
    SELL when price closes back above its exit_ma-candle average (the bounce), or after max_hold candles.
    The engine's stop-loss and trailing stop still apply.
    """

    name = "dip_buy"
    uses_forming_bar = False
    DEFAULTS = dict(long_ma=200, rsi_len=2, rsi_entry=10.0, exit_ma=5, max_hold=10)

    def __init__(self, **params):
        _check(params, self.DEFAULTS)
        p = {**self.DEFAULTS, **params}
        self.long_ma, self.rsi_len, self.exit_ma, self.max_hold = (int(p["long_ma"]), int(p["rsi_len"]),
                                                                   int(p["exit_ma"]), int(p["max_hold"]))
        self.rsi_entry = float(p["rsi_entry"])
        self.params = p
        self.min_bars = max(self.long_ma, 50) + 10

    def prepare(self, candles: pd.DataFrame) -> Prepared:
        df = add_all(candles, 20, 50, self.long_ma)
        df["rsi_fast"] = rsi(df["close"], self.rsi_len)
        df["exit_ma"] = sma(df["close"], self.exit_ma)
        return Prepared(df)

    def evaluate(self, candles: pd.DataFrame, position: Position | None, live_price: float,
                 progress: float | None = None) -> Signal:
        if len(candles) < self.min_bars:
            return Signal("HOLD", f"warming up ({len(candles)}/{self.min_bars} candles)")
        p = self.prepare(candles)
        return self.decide(p, len(p) - 1, position, live_price, progress)

    def decide(self, p: Prepared, i: int, position: Position | None, live_price: float,
               progress: float | None = None) -> Signal:
        if i + 1 < self.min_bars:
            return Signal("HOLD", f"warming up ({i + 1}/{self.min_bars} candles)")
        last = p.row(i)
        snap = _snapshot(last)
        snap["rsi_fast"] = round(_num(last["rsi_fast"]), 2)
        a = _num(last["atr"])
        if any(math.isnan(_num(last[k])) for k in ("rsi_fast", "exit_ma", "atr")):
            return Signal("HOLD", "indicators not ready", snapshot=snap)

        if position:
            if last["close"] > last["exit_ma"]:
                return Signal("SELL", f"bounced back above the {self.exit_ma}-candle average", live_price, a, snap)
            if position.entry_bar in p.index:
                held = i - p.index.index(position.entry_bar)
                if held >= self.max_hold:
                    return Signal("SELL", f"no bounce after {held} candles — exiting", live_price, a, snap)
            return Signal("HOLD", "waiting for the bounce", live_price, a, snap)

        if last["rsi_fast"] >= self.rsi_entry:
            return Signal("HOLD", "no dip", live_price, a, snap)
        why = f"sharp dip: RSI({self.rsi_len}) {last['rsi_fast']:.0f} < {self.rsi_entry:g}"
        if self.long_ma and not last["close"] > _num(last["long_ma"]):
            return _filtered(f"{why}, but price is below its {self.long_ma}-candle average (falling, not dipping)",
                             live_price, a, snap)
        return Signal("BUY", why, live_price, a, snap)


class _Pair:
    """Prepared indicators for both halves of the auto strategy."""

    def __init__(self, trend: Prepared, dip: Prepared):
        self.trend, self.dip, self.index = trend, dip, trend.index

    def __len__(self) -> int:
        return len(self.index)


class AutoSwitch:
    """Picks the strategy that suits the market right now, on CLOSED candles:
    trending (ADX ≥ adx_switch) → trend_rsi rules; choppy (ADX below) → dip_buy rules.
    A trade is always exited by the rules that opened it."""

    name = "auto"
    uses_forming_bar = False
    DEFAULTS = dict(adx_switch=20.0, long_ma=200)

    def __init__(self, **params):
        _check(params, self.DEFAULTS)
        p = {**self.DEFAULTS, **params}
        self.adx_switch, self.params = float(p["adx_switch"]), p
        self.trend = TrendRSI(long_ma=int(p["long_ma"]), adx_min=0)
        self.dip = DipBuy(long_ma=int(p["long_ma"]))
        self.min_bars = max(self.trend.min_bars, self.dip.min_bars)

    def prepare(self, candles: pd.DataFrame) -> _Pair:
        return _Pair(self.trend.prepare(candles), self.dip.prepare(candles))

    def evaluate(self, candles: pd.DataFrame, position: Position | None, live_price: float,
                 progress: float | None = None) -> Signal:
        if len(candles) < self.min_bars:
            return Signal("HOLD", f"warming up ({len(candles)}/{self.min_bars} candles)")
        p = self.prepare(candles)
        return self.decide(p, len(p) - 1, position, live_price, progress)

    def decide(self, p: _Pair, i: int, position: Position | None, live_price: float,
               progress: float | None = None) -> Signal:
        if i + 1 < self.min_bars:
            return Signal("HOLD", f"warming up ({i + 1}/{self.min_bars} candles)")
        if position:
            sub, sp = (self.dip, p.dip) if "dip mode" in (position.entry_reason or "") else (self.trend, p.trend)
            return sub.decide(sp, i, position, live_price, progress)
        adx_now = _num(p.trend.row(i)["adx"])
        trending = not math.isnan(adx_now) and adx_now >= self.adx_switch
        sub, sp, label = (self.trend, p.trend, "trend mode") if trending else (self.dip, p.dip, "dip mode")
        sig = sub.decide(sp, i, None, live_price, progress)
        sig.reason = f"{label} (ADX {adx_now:.0f}): {sig.reason}"
        return sig


STRATEGIES = {"trend_rsi": TrendRSI, "vol_breakout": VolBreakout, "dip_buy": DipBuy, "auto": AutoSwitch}


def get_strategy(name: str, params: dict | None = None):
    if name not in STRATEGIES:
        raise ValueError(f"unknown strategy {name}")
    return STRATEGIES[name](**(params or {}))
