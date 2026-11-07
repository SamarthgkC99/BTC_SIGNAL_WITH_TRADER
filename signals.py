"""Signal engine for the BTC momentum-level study (pure pandas/numpy, no Streamlit).

Fixes vs. the first version:
  1. Level invalidation is broader (strong cross OR N consecutive closes beyond,
     checked on every bar, so gaps / drifts through a level also invalidate it).
  2. One event per candle (no duplicates across nearby levels); default cooldown
     equals the forward horizon so forward windows of one level do not overlap.
  3. A strong break is evaluated BEFORE rejection patterns.
  4. Direction-adjusted forward metrics are added next to the raw ones.
"""
from dataclasses import dataclass
from datetime import timedelta, timezone

import numpy as np
import pandas as pd

IST = timezone(timedelta(hours=5, minutes=30), "IST")
INTERVAL_MIN = {"5m": 5, "15m": 15, "1h": 60}

PATTERN_SIDE = {
    "Bullish rejection (candidate)": 1,
    "Bearish rejection (candidate)": -1,
    "Strong close above level (candidate)": 1,
    "Strong close below level (candidate)": -1,
}
SIDE_LABEL = {1: "BUY", -1: "SELL", 0: ""}


@dataclass
class Params:
    atr_period: int = 14
    impulse_atr: float = 1.2
    wick_body_ratio: float = 1.5
    close_edge_pct: int = 70
    min_spacing_atr: float = 1.0
    forward_bars: int = 6
    cooldown_bars: int = 6
    confirm_closes: int = 3


def params_from(settings: dict) -> Params:
    return Params(**{k: settings[k] for k in Params.__dataclass_fields__ if k in settings})


def in_trade_window(ts_utc, days=(1, 2, 3), start_hm=(19, 0), end_hm=(23, 0)) -> bool:
    """True if the UTC timestamp falls on Tue/Wed/Thu between 19:00 and 23:00 IST."""
    t = pd.Timestamp(ts_utc)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    t = t.tz_convert(IST)
    minutes = t.hour * 60 + t.minute
    return t.weekday() in days and start_hm[0] * 60 + start_hm[1] <= minutes < end_hm[0] * 60 + end_hm[1]


def closed_only(df: pd.DataFrame, interval: str, now=None):
    """Drop the still-forming last candle. Returns (closed_df, forming_flag)."""
    now = now if now is not None else pd.Timestamp.now(tz="UTC")
    last_open = df["open_time"].iloc[-1]
    forming = now < last_open + pd.Timedelta(minutes=INTERVAL_MIN[interval])
    out = df.iloc[:-1] if forming else df
    return out.reset_index(drop=True), bool(forming)


def add_indicators(df: pd.DataFrame, p: Params) -> pd.DataFrame:
    df = df.copy()
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    df["atr"] = tr.rolling(p.atr_period, min_periods=p.atr_period).mean()
    df["body"] = (df["close"] - df["open"]).abs()
    df["range"] = (df["high"] - df["low"]).replace(0, np.nan)
    df["body_frac"] = df["body"] / df["range"]
    df["close_position"] = (df["close"] - df["low"]) / df["range"]
    df["upper_wick"] = df["high"] - df[["open", "close"]].max(axis=1)
    df["lower_wick"] = df[["open", "close"]].min(axis=1) - df["low"]
    df["impulse"] = df["atr"].notna() & (df["body"] >= p.impulse_atr * df["atr"])
    df["direction"] = np.where(df["close"] > df["open"], "Bullish",
                               np.where(df["close"] < df["open"], "Bearish", "Neutral"))
    return df


def find_levels(df: pd.DataFrame, p: Params) -> list:
    levels = []
    for i in np.flatnonzero(df["impulse"].to_numpy()):
        row = df.iloc[i]
        cand = {
            "index": int(i), "time": row["open_time"], "price": float(row["open"]),
            "direction": row["direction"], "atr": float(row["atr"]), "body": float(row["body"]),
            "invalidated": False, "invalidated_time": None,
        }
        min_dist = p.min_spacing_atr * cand["atr"]
        if all(abs(cand["price"] - lv["price"]) >= min_dist for lv in levels):
            levels.append(cand)
    return levels


def find_events(df: pd.DataFrame, levels: list, p: Params) -> pd.DataFrame:
    n = len(df)
    o, h, l, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    atr = df["atr"].to_numpy(float)
    body = df["body"].to_numpy(float)
    cpos = df["close_position"].to_numpy(float)
    upw = df["upper_wick"].to_numpy(float)
    low_w = df["lower_wick"].to_numpy(float)
    times = df["open_time"]
    edge = p.close_edge_pct / 100.0
    fb = p.forward_bars
    rows = []

    for level_no, level in enumerate(levels, start=1):
        lvl = level["price"]
        ref = 1 if level["direction"] == "Bullish" else -1      # side price sits on after the origin candle
        prev_side, opp_run, last_event_i = ref, 0, -10 ** 9
        for i in range(level["index"] + 1, n):
            if np.isnan(atr[i]):
                continue
            side = 1 if c[i] > lvl else (-1 if c[i] < lvl else 0)
            opp_run = opp_run + 1 if side == -ref else 0
            strong_body = body[i] >= p.impulse_atr * atr[i]
            breaks_up = ref == -1 and side == 1 and prev_side != 1 and strong_body and cpos[i] >= edge
            breaks_dn = ref == 1 and side == -1 and prev_side != -1 and strong_body and cpos[i] <= 1 - edge
            label = None
            if breaks_up:
                label = "Strong close above level (candidate)"
            elif breaks_dn:
                label = "Strong close below level (candidate)"
            elif opp_run >= p.confirm_closes:
                level["invalidated"], level["invalidated_time"] = True, times.iloc[i]
                break                                            # drifted through: level is dead, no event
            elif i <= last_event_i + p.cooldown_bars:
                if side != 0:
                    prev_side = side
                continue
            elif l[i] <= lvl <= h[i]:
                bd = max(body[i], 1e-12)
                if low_w[i] / bd >= p.wick_body_ratio and c[i] > o[i] and cpos[i] >= 0.5:
                    label = "Bullish rejection (candidate)"
                elif upw[i] / bd >= p.wick_body_ratio and c[i] < o[i] and cpos[i] <= 0.5:
                    label = "Bearish rejection (candidate)"
                else:
                    label = "Touch / no defined pattern"
            if side != 0:
                prev_side = side
            if label is None:
                continue

            sig = PATTERN_SIDE.get(label, 0)
            if i + 1 + fb <= n:
                fut_h, fut_l, fut_c = h[i + 1:i + 1 + fb].max(), l[i + 1:i + 1 + fb].min(), c[i + fb]
                fwd = (fut_c / c[i] - 1) * 100
                hi = (fut_h / c[i] - 1) * 100
                lo = (fut_l / c[i] - 1) * 100
            else:
                fwd = hi = lo = np.nan
            rows.append({
                "level_id": level_no, "level_time": level["time"], "level_price": lvl,
                "origin_direction": level["direction"], "event_time": times.iloc[i],
                "event_price": float(c[i]), "pattern": label, "signal_side": sig,
                "forward_close_change_pct": fwd, "future_high_excursion_pct": hi,
                "future_low_excursion_pct": lo,
                "fwd_return_dir_pct": fwd * sig if sig else np.nan,
                "favorable_excursion_pct": (hi if sig > 0 else -lo) if sig else np.nan,
                "adverse_excursion_pct": (lo if sig > 0 else -hi) if sig else np.nan,
                "level_invalidated": label.startswith("Strong close"),
            })
            last_event_i = i
            if label.startswith("Strong close"):
                level["invalidated"], level["invalidated_time"] = True, times.iloc[i]
                break

    ev = pd.DataFrame(rows)
    if ev.empty:
        return ev
    # One event per candle: prefer a defined pattern, then the most recent level.
    ev["_def"] = (ev["signal_side"] != 0).astype(int)
    ev = (ev.sort_values(["event_time", "_def", "level_time"], ascending=[True, False, False])
            .drop_duplicates("event_time").drop(columns="_def"))
    return ev.sort_values("event_time").reset_index(drop=True)


def run_study(closed_df: pd.DataFrame, p: Params, interval: str = "5m"):
    df = add_indicators(closed_df, p)
    levels = find_levels(df, p)
    events = find_events(df, levels, p)
    if not events.empty:
        events["event_close_time"] = events["event_time"] + pd.Timedelta(minutes=INTERVAL_MIN[interval])
    return df, levels, events


def summarize(events: pd.DataFrame) -> pd.DataFrame:
    if events.empty:
        return pd.DataFrame()
    g = events.groupby("pattern", dropna=False)
    out = g.agg(
        events=("pattern", "count"),
        avg_forward_close_change_pct=("forward_close_change_pct", "mean"),
        median_forward_close_change_pct=("forward_close_change_pct", "median"),
        avg_future_high_excursion_pct=("future_high_excursion_pct", "mean"),
        avg_future_low_excursion_pct=("future_low_excursion_pct", "mean"),
        avg_dir_return_pct=("fwd_return_dir_pct", "mean"),
        avg_favorable_pct=("favorable_excursion_pct", "mean"),
        avg_adverse_pct=("adverse_excursion_pct", "mean"),
    )
    out["measured_events"] = g["forward_close_change_pct"].count()
    out["dir_win_rate_pct"] = g["fwd_return_dir_pct"].apply(
        lambda s: (s.dropna() > 0).mean() * 100 if s.notna().any() else np.nan)
    return out.reset_index()
