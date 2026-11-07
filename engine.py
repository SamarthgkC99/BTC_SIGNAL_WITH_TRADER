"""Demo (paper) trading engine. Simulation only: no orders, no accounts, no credentials.

Rules
  * BUY on bullish patterns, SELL (short) on bearish patterns; plain touches are skipped.
  * Entry at the signal candle's close. Fixed notional per trade (lot_usd).
  * Exit on take-profit / stop-loss (percent from entry; SL wins if both hit in one
    candle) or after `forward_bars` candles at the close.
  * Trades are tagged WINDOW (Tue/Wed/Thu 19:00-23:00 IST) or TEST (outside the
    window, only when test_mode is on).
Candles are processed in order, so a late or restarted worker never double-trades.
"""
import traceback

import pandas as pd

import providers
import signals
from signals import INTERVAL_MIN, SIDE_LABEL, in_trade_window

DEFAULT_SETTINGS = dict(
    initial_balance=5000.0, lot_usd=20.0, tp_pct=0.5, sl_pct=0.5, fee_pct=0.0, max_open=3,
    test_mode=True, allow_usd_fallback=False, allowed_origins="Both",
    skip_invalidated_events=False, max_signal_age_bars=2,
    interval="5m", bars=500, atr_period=14, impulse_atr=1.2, wick_body_ratio=1.5,
    close_edge_pct=70, min_spacing_atr=1.0, forward_bars=6, cooldown_bars=6, confirm_closes=3,
)


def merge_settings(saved):
    return {**DEFAULT_SETTINGS, **(saved or {})}


def new_state(settings):
    return {
        "initial_balance": float(settings["initial_balance"]), "trades": [], "signals": [],
        "next_id": 1, "last_candle_time": None, "last_run_utc": None, "last_source": None,
        "last_quote": None, "last_price": None, "last_error": None,
    }


def _pnl(t, exit_price):
    gross = t["qty"] * (exit_price - t["entry_price"]) * t["side_num"]
    fee = (t["lot_usd"] + t["qty"] * exit_price) * t["fee_pct"] / 100.0
    return gross, fee, gross - fee


def _process_exits(state, closed, iv):
    for t in state["trades"]:
        if t["status"] != "OPEN":
            continue
        after = closed[closed["open_time"] > pd.Timestamp(t["event_open_time"])].head(t["horizon"])
        for k, row in enumerate(after.itertuples(index=False), start=1):
            if t["side_num"] > 0:
                sl_hit, tp_hit = row.low <= t["sl"], row.high >= t["tp"]
            else:
                sl_hit, tp_hit = row.high >= t["sl"], row.low <= t["tp"]
            if sl_hit:
                price, reason = t["sl"], "SL"
            elif tp_hit:
                price, reason = t["tp"], "TP"
            elif k >= t["horizon"]:
                price, reason = float(row.close), "TIME"
            else:
                continue
            seen = after.iloc[:k]
            gross, fee, net = _pnl(t, price)
            t.update(
                status="CLOSED", exit_reason=reason, exit_price=float(price),
                exit_time=(row.open_time + iv).isoformat(), bars_held=k,
                gross_pnl_usd=gross, fee_usd=fee, pnl_usd=net,
                future_high_excursion_pct=(float(seen["high"].max()) / t["entry_price"] - 1) * 100,
                future_low_excursion_pct=(float(seen["low"].min()) / t["entry_price"] - 1) * 100,
            )
            break


def _consider(state, s, ev, latest_open, iv, source, quote):
    side = int(ev["signal_side"])
    close_ts = ev["event_time"] + iv
    in_win = in_trade_window(close_ts)
    sig = {
        "event_time": ev["event_time"].isoformat(), "pattern": ev["pattern"], "side": SIDE_LABEL[side],
        "price": float(ev["event_price"]), "origin_direction": ev["origin_direction"],
        "level_invalidated": bool(ev["level_invalidated"]), "in_trade_window": bool(in_win),
        "source": source, "action": "",
    }
    open_trades = [t for t in state["trades"] if t["status"] == "OPEN"]
    age = (latest_open - ev["event_time"]) / iv
    reason = None
    if age > s["max_signal_age_bars"]:
        reason = "stale signal (worker was behind)"
    elif not in_win and not s["test_mode"]:
        reason = "outside trade window"
    elif s["allowed_origins"] != "Both" and ev["origin_direction"] != s["allowed_origins"]:
        reason = f"origin filter ({s['allowed_origins']} only)"
    elif s["skip_invalidated_events"] and ev["level_invalidated"]:
        reason = "level-invalidated filter"
    elif len(open_trades) >= s["max_open"]:
        reason = "max open trades reached"
    elif any(t["level_time"] == pd.Timestamp(ev["level_time"]).isoformat() for t in open_trades):
        reason = "level already has an open trade"
    elif quote != "USDT" and not s["allow_usd_fallback"]:
        reason = "USD fallback data not allowed"
    if reason:
        sig["action"] = "SKIPPED: " + reason
    else:
        entry = float(ev["event_price"])
        qty = s["lot_usd"] / entry
        tp_f, sl_f = s["tp_pct"] / 100.0, s["sl_pct"] / 100.0
        state["trades"].append({
            "id": state["next_id"], "status": "OPEN", "mode": "WINDOW" if in_win else "TEST",
            "side": SIDE_LABEL[side], "side_num": side, "pattern": ev["pattern"],
            "origin_direction": ev["origin_direction"], "level_invalidated": bool(ev["level_invalidated"]),
            "level_time": pd.Timestamp(ev["level_time"]).isoformat(), "level_price": float(ev["level_price"]),
            "event_open_time": ev["event_time"].isoformat(), "entry_time": close_ts.isoformat(),
            "entry_price": entry, "lot_usd": float(s["lot_usd"]), "qty": qty,
            "tp": entry * (1 + side * tp_f), "sl": entry * (1 - side * sl_f),
            "horizon": int(s["forward_bars"]), "fee_pct": float(s["fee_pct"]), "source": source,
        })
        state["next_id"] += 1
        sig["action"] = "TRADED"
    state["signals"].append(sig)


def process(state, s, closed, events, source, quote, now=None):
    """Pure state transition: apply new closed candles/events to the paper account."""
    now = now if now is not None else pd.Timestamp.now(tz="UTC")
    iv = pd.Timedelta(minutes=INTERVAL_MIN[s["interval"]])
    latest_open = closed["open_time"].iloc[-1]
    state.update(last_run_utc=now.isoformat(), last_source=source, last_quote=quote,
                 last_price=float(closed["close"].iloc[-1]), last_error=None)
    _process_exits(state, closed, iv)
    if state["last_candle_time"] is None:                 # first run: start from "now", never trade history
        state["last_candle_time"] = latest_open.isoformat()
        return state
    last_seen = pd.Timestamp(state["last_candle_time"])
    if events is not None and not events.empty:
        new = events[(events["event_time"] > last_seen) & (events["signal_side"] != 0)]
        for _, ev in new.sort_values("event_time").iterrows():
            _consider(state, s, ev, latest_open, iv, source, quote)
    state["last_candle_time"] = max(last_seen, latest_open).isoformat()
    state["signals"] = state["signals"][-100:]
    state["trades"] = state["trades"][-2000:]
    return state


def run_cycle(store):
    """One worker pass. Never raises; errors are logged and stored in the state."""
    try:
        s = merge_settings(store.read("settings"))
        state = store.read("state") or new_state(s)
    except Exception as exc:                              # storage unreachable: do nothing, do not overwrite
        print(f"[worker] storage read failed: {exc}", flush=True)
        return False
    try:
        df, source, quote, _ = providers.fetch_candles(s["interval"], int(s["bars"]), s["allow_usd_fallback"])
        closed, _ = signals.closed_only(df, s["interval"])
        _, _, events = signals.run_study(closed, signals.params_from(s), s["interval"])
        state = process(state, s, closed, events, source, quote)
    except Exception as exc:                              # noqa: BLE001
        state["last_error"] = f"{type(exc).__name__}: {exc}"
        state["last_run_utc"] = pd.Timestamp.now(tz="UTC").isoformat()
        print("[worker] cycle failed:\n" + traceback.format_exc(), flush=True)
    try:
        store.write("state", state)
    except Exception as exc:                              # noqa: BLE001
        print(f"[worker] storage write failed: {exc}", flush=True)
        return False
    return state["last_error"] is None


# ---------- dashboard helpers ----------

def trades_df(state, mode="All"):
    df = pd.DataFrame(state.get("trades", []))
    if df.empty:
        return df
    for col in ("entry_time", "exit_time", "level_time", "event_open_time"):
        if col in df:
            df[col] = pd.to_datetime(df[col], utc=True, errors="coerce")
    return df if mode == "All" else df[df["mode"] == mode].reset_index(drop=True)


def account_summary(state, mode="All", live_price=None):
    df = trades_df(state, mode)
    initial = float(state.get("initial_balance", 5000.0))
    out = dict(initial=initial, realized=0.0, unrealized=0.0, open=0, closed=0, wins=0, win_rate=None,
               balance=initial, equity=initial, curve=pd.DataFrame({"time": [], "balance": []}))
    if df.empty:
        return out
    closed = df[df["status"] == "CLOSED"].sort_values("exit_time")
    opened = df[df["status"] == "OPEN"]
    out["closed"], out["open"] = len(closed), len(opened)
    if len(closed):
        out["realized"] = float(closed["pnl_usd"].sum())
        out["wins"] = int((closed["pnl_usd"] > 0).sum())
        out["win_rate"] = out["wins"] / len(closed) * 100
        out["curve"] = pd.DataFrame({"time": closed["exit_time"], "balance": initial + closed["pnl_usd"].cumsum()})
    if live_price and len(opened):
        out["unrealized"] = float((opened["qty"] * (live_price - opened["entry_price"]) * opened["side_num"]).sum())
    out["balance"] = initial + out["realized"]
    out["equity"] = out["balance"] + out["unrealized"]
    return out
