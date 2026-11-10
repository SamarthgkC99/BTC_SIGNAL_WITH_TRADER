"""Demo (paper) trading engine. Simulation only: no orders, no accounts, no credentials.

Rules
  * BUY on bullish patterns, SELL (short) on bearish patterns; plain touches are skipped.
  * Entry at the signal candle's close. Fixed notional per trade (lot_usd).
  * Exit on take-profit / stop-loss (percent from entry; SL wins if both hit in one
    candle), or when an opposite signal arrives (position is flipped), or - only if
    max_hold_bars > 0 - after that many candles. Default: hold until TP/SL or a flip.
  * Window trades are closed at 23:00 IST (window end) whether in profit or loss, if close_at_window_end.
  * Two independent lanes, each RUNNING / PAUSED / STOPPED:
      window lane = signals inside Tue/Wed/Thu 19:00-23:00 IST (trades tagged WINDOW),
      test lane   = signals outside that window (trades tagged TEST).
    PAUSED = no new entries, open trades keep running. STOPPED = no new entries and the
    lane's open trades are closed at the latest price.
Candles are processed in order, so a late or restarted worker never double-trades.
"""
import traceback
import uuid

import pandas as pd

import providers
import signals
from signals import INTERVAL_MIN, SIDE_LABEL, in_trade_window

DEFAULT_SETTINGS = dict(
    initial_balance=5000.0, lot_usd=20.0, tp_pct=0.5, sl_pct=0.5, fee_pct=0.0, max_open=3,
    window_lane="RUNNING", test_lane="STOPPED", sleep_outside_window=False,
    position_mode="Single position (flip on opposite signal)", max_hold_bars=0, close_at_window_end=True,
    allow_usd_fallback=False, allowed_origins="Both",
    skip_invalidated_events=False, max_signal_age_bars=2,
    provider="Auto (fallback chain)", provider_fallback=True,
    interval="5m", bars=500, atr_period=14, impulse_atr=1.2, wick_body_ratio=1.5,
    close_edge_pct=70, min_spacing_atr=1.0, forward_bars=6, cooldown_bars=6, confirm_closes=3,
    rules_mode="Improved (fixes)",
)


def merge_settings(saved):
    saved = dict(saved or {})
    if "test_lane" not in saved and "test_mode" in saved:     # old on/off switch -> lane state
        saved["test_lane"] = "RUNNING" if saved["test_mode"] else "STOPPED"
    saved.pop("test_mode", None)
    if saved and "rules_mode" not in saved:        # settings saved before the rules switch existed:
        saved.pop("cooldown_bars", None)           # use the current defaults for the cooldown/confirm settings
        saved.pop("confirm_closes", None)
    return {**DEFAULT_SETTINGS, **saved}


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


def lane_of(trade):
    return "window" if trade.get("mode") == "WINDOW" else "test"


def close_trade(t, price, reason, exit_time_iso, bars_held=None, seen=None):
    """Book a trade's exit. `seen` = candles from entry to exit (for the excursion columns)."""
    gross, fee, net = _pnl(t, price)
    t.update(status="CLOSED", exit_reason=reason, exit_price=float(price), exit_time=exit_time_iso,
             bars_held=bars_held, gross_pnl_usd=gross, fee_usd=fee, pnl_usd=net)
    if seen is not None and len(seen):
        t["future_high_excursion_pct"] = (float(seen["high"].max()) / t["entry_price"] - 1) * 100
        t["future_low_excursion_pct"] = (float(seen["low"].min()) / t["entry_price"] - 1) * 100


def close_lane_trades(state, lane, price, exit_time_iso, reason="STOPPED"):
    """Close every open trade of a lane at `price`. Returns how many were closed."""
    n = 0
    for t in state["trades"]:
        if t["status"] == "OPEN" and lane_of(t) == lane:
            close_trade(t, price, reason, exit_time_iso)
            n += 1
    return n


def _process_exits(state, closed, iv):
    for t in state["trades"]:
        if t["status"] != "OPEN":
            continue
        ev_open, h = pd.Timestamp(t["event_open_time"]), int(t["horizon"])
        wend = pd.Timestamp(t["window_end"]) if t.get("window_end") else None
        if len(closed) and closed["open_time"].iloc[0] > ev_open + iv:    # candles after entry are no longer loaded
            close_trade(t, float(closed["close"].iloc[-1]), "HISTORY", (closed["open_time"].iloc[-1] + iv).isoformat())
            continue
        after = closed[closed["open_time"] > ev_open]
        if h > 0:
            after = after.head(h)
        for k, row in enumerate(after.itertuples(index=False), start=1):
            if t["side_num"] > 0:
                sl_hit, tp_hit = row.low <= t["sl"], row.high >= t["tp"]
            else:
                sl_hit, tp_hit = row.high >= t["sl"], row.low <= t["tp"]
            if sl_hit:
                price, reason = t["sl"], "SL"
            elif tp_hit:
                price, reason = t["tp"], "TP"
            elif wend is not None and row.open_time + iv >= wend:
                price, reason = float(row.close), "WINDOW_END"
            elif h > 0 and k >= h:
                price, reason = float(row.close), "TIME"
            else:
                continue
            close_trade(t, price, reason, (row.open_time + iv).isoformat(), k, after.iloc[:k])
            break


def _near_window(now_utc, margin_min=30):
    t = pd.Timestamp(now_utc).tz_convert(signals.IST)
    minutes = t.hour * 60 + t.minute
    return t.weekday() in (1, 2, 3) and 19 * 60 - margin_min <= minutes < 23 * 60 + margin_min


def needs_awake(s, state, now_utc=None):
    """Reason the app should keep Render awake, or None if it may sleep (saves free hours)."""
    now_utc = now_utc if now_utc is not None else pd.Timestamp.now(tz="UTC")
    if any(t["status"] == "OPEN" for t in state.get("trades", [])):
        return "open trades are being managed"
    if "PAUSED" in (s["window_lane"], s["test_lane"]):
        return "a lane is paused (stays awake so Start is instant)"
    if s["test_lane"] == "RUNNING":
        return "test trading is running"
    if s["window_lane"] == "RUNNING":
        if not s["sleep_outside_window"] or _near_window(now_utc):
            return "window trading is running"
    return None


def usd_ok(s, quote):
    return quote == "USDT" or bool(s.get("allow_usd_fallback")) or s.get("provider") in providers.USD_PROVIDERS


def is_single(s):
    return str(s.get("position_mode", "")).startswith("Single")


def open_in_lane(state, lane):
    return [t for t in state["trades"] if t["status"] == "OPEN" and lane_of(t) == lane]


def close_opposite(state, lane, side, price, exit_time_iso, closed=None, upto_open_time=None):
    """Flip: close this lane's open trades that point the other way. Returns how many were closed."""
    n = 0
    for t in open_in_lane(state, lane):
        if t["side_num"] == side:
            continue
        seen, bars = None, None
        if closed is not None and upto_open_time is not None:
            seen = closed[(closed["open_time"] > pd.Timestamp(t["event_open_time"])) & (closed["open_time"] <= upto_open_time)]
            bars = len(seen)
        close_trade(t, price, "REVERSED", exit_time_iso, bars, seen)
        n += 1
    return n


def block_reason(state, s, origin, invalidated, level_time_iso, quote, side=None, lane=None):
    """Trader rules shared by automatic and manual test entries (the timing window is checked elsewhere)."""
    open_trades = [t for t in state["trades"] if t["status"] == "OPEN"]
    if s["allowed_origins"] != "Both" and origin != s["allowed_origins"]:
        return f"origin filter ({s['allowed_origins']} only)"
    if s["skip_invalidated_events"] and invalidated:
        return "level-invalidated filter"
    if is_single(s) and side is not None and lane is not None:
        if any(t["side_num"] == side for t in open_in_lane(state, lane)):
            return f"already in a {SIDE_LABEL[side]} position"
    else:
        if len(open_trades) >= s["max_open"]:
            return "max open trades reached"
        if any(t["level_time"] == level_time_iso for t in open_trades):
            return "level already has an open trade"
    if not usd_ok(s, quote):
        return "USD fallback data not allowed"
    return None


def _window_end_iso(entry_time_iso):
    """23:00 IST on the IST date of the entry, as a UTC ISO string."""
    t = pd.Timestamp(entry_time_iso).tz_convert(signals.IST)
    return (t.normalize() + pd.Timedelta(hours=23)).tz_convert("UTC").isoformat()


def build_trade(trade_id, s, side, pattern, origin, invalidated, level_time_iso, level_price,
                event_open_time_iso, entry_time_iso, entry, source, mode, trigger="auto"):
    tp_f, sl_f = s["tp_pct"] / 100.0, s["sl_pct"] / 100.0
    return {
        "id": trade_id, "uid": uuid.uuid4().hex[:10], "status": "OPEN", "mode": mode, "trigger": trigger,
        "side": SIDE_LABEL[side], "side_num": side, "pattern": pattern,
        "origin_direction": origin, "level_invalidated": bool(invalidated),
        "level_time": level_time_iso, "level_price": float(level_price),
        "event_open_time": event_open_time_iso, "entry_time": entry_time_iso,
        "entry_price": float(entry), "lot_usd": float(s["lot_usd"]), "qty": s["lot_usd"] / entry,
        "tp": entry * (1 + side * tp_f), "sl": entry * (1 - side * sl_f),
        "horizon": int(s["max_hold_bars"]), "fee_pct": float(s["fee_pct"]), "source": source,
        "window_end": _window_end_iso(entry_time_iso) if (mode == "WINDOW" and s.get("close_at_window_end")) else None,
    }


def _consider(state, s, ev, latest_open, iv, source, quote, closed=None):
    side = int(ev["signal_side"])
    close_ts = ev["event_time"] + iv
    in_win = in_trade_window(close_ts)
    level_iso = pd.Timestamp(ev["level_time"]).isoformat()
    sig = {
        "event_time": ev["event_time"].isoformat(), "pattern": ev["pattern"], "side": SIDE_LABEL[side],
        "price": float(ev["event_price"]), "origin_direction": ev["origin_direction"],
        "level_invalidated": bool(ev["level_invalidated"]), "in_trade_window": bool(in_win),
        "source": source, "action": "",
    }
    age = (latest_open - ev["event_time"]) / iv
    lane_name = "window" if in_win else "test"
    lane_now = s[f"{lane_name}_lane"]
    if age > s["max_signal_age_bars"]:
        reason = "stale signal (worker was behind)"
    elif lane_now != "RUNNING":
        reason = (f"window trading is {lane_now.lower()}" if in_win
                  else f"outside the window and test trading is {lane_now.lower()}")
    else:
        reason = block_reason(state, s, ev["origin_direction"], ev["level_invalidated"], level_iso, quote, side, lane_name)
    if reason:
        sig["action"] = "SKIPPED: " + reason
    else:
        flipped = 0
        if is_single(s):
            flipped = close_opposite(state, lane_name, side, float(ev["event_price"]), close_ts.isoformat(),
                                     closed, ev["event_time"])
        state["trades"].append(build_trade(
            state["next_id"], s, side, ev["pattern"], ev["origin_direction"], ev["level_invalidated"],
            level_iso, ev["level_price"], ev["event_time"].isoformat(), close_ts.isoformat(),
            float(ev["event_price"]), source, "WINDOW" if in_win else "TEST"))
        state["next_id"] += 1
        sig["action"] = "TRADED" + (f" (flipped {flipped} opposite position)" if flipped else "")
    state["signals"].append(sig)


def process(state, s, closed, events, source, quote, now=None):
    """Pure state transition: apply new closed candles/events to the paper account."""
    now = now if now is not None else pd.Timestamp.now(tz="UTC")
    iv = pd.Timedelta(minutes=INTERVAL_MIN[s["interval"]])
    latest_open = closed["open_time"].iloc[-1]
    state.update(last_run_utc=now.isoformat(), last_source=source, last_quote=quote,
                 last_price=float(closed["close"].iloc[-1]), last_error=None)
    _process_exits(state, closed, iv)
    for t in state["trades"]:                              # a STOPPED lane never keeps open trades
        if t["status"] == "OPEN" and s[f"{lane_of(t)}_lane"] == "STOPPED":
            close_trade(t, float(closed["close"].iloc[-1]), "STOPPED", (latest_open + iv).isoformat())
    if state["last_candle_time"] is None:                 # first run: start from "now", never trade history
        state["last_candle_time"] = latest_open.isoformat()
        return state
    last_seen = pd.Timestamp(state["last_candle_time"])
    if events is not None and not events.empty:
        new = events[(events["event_time"] > last_seen) & (events["signal_side"] != 0)]
        for _, ev in new.sort_values("event_time").iterrows():
            _consider(state, s, ev, latest_open, iv, source, quote, closed)
    state["last_candle_time"] = max(last_seen, latest_open).isoformat()
    state["signals"] = state["signals"][-100:]
    state["trades"] = state["trades"][-2000:]
    return state


def _merge_foreign(state, fresh):
    """Keep trades/signals the page added (test trades) while this cycle was running."""
    fresh_by_uid = {t.get("uid"): t for t in fresh.get("trades", []) if t.get("uid")}
    for t in state["trades"]:                              # trades the page closed (Stop / manual) while we ran
        ft = fresh_by_uid.get(t.get("uid"))
        if t["status"] == "OPEN" and ft and ft["status"] == "CLOSED" and ft.get("exit_reason") in ("MANUAL", "STOPPED"):
            t.clear()
            t.update(ft)
    own_uids = {t.get("uid") for t in state["trades"]}
    own_ids = {t["id"] for t in state["trades"]}
    for t in fresh.get("trades", []):
        if t.get("uid") and t["uid"] not in own_uids:
            if t["id"] in own_ids:
                t["id"] = state["next_id"]
            state["next_id"] = max(state["next_id"], t["id"]) + 1
            own_ids.add(t["id"])
            state["trades"].append(t)
    seen = {(x["event_time"], x["side"], x["action"]) for x in state["signals"]}
    for x in fresh.get("signals", []):
        if (x["event_time"], x["side"], x["action"]) not in seen:
            state["signals"].append(x)
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
        df, source, quote, _ = providers.fetch_candles(s["interval"], int(s["bars"]), s["allow_usd_fallback"],
                                                      s["provider"], s["provider_fallback"])
        closed, _ = signals.closed_only(df, s["interval"])
        _, _, events = signals.run_study(closed, signals.params_from(s), s["interval"])
        state = process(state, s, closed, events, source, quote)
    except Exception as exc:                              # noqa: BLE001
        state["last_error"] = f"{type(exc).__name__}: {exc}"
        state["last_run_utc"] = pd.Timestamp.now(tz="UTC").isoformat()
        print("[worker] cycle failed:\n" + traceback.format_exc(), flush=True)
    try:
        fresh = store.read("state")
        if fresh:
            state = _merge_foreign(state, fresh)
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
    for col in ("exit_time", "exit_price", "exit_reason", "bars_held", "pnl_usd", "gross_pnl_usd", "fee_usd",
                "future_high_excursion_pct", "future_low_excursion_pct", "trigger", "uid"):
        if col not in df:
            df[col] = None
    for col in ("exit_price", "bars_held", "pnl_usd", "gross_pnl_usd", "fee_usd",
                "future_high_excursion_pct", "future_low_excursion_pct"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    for col in ("entry_time", "exit_time", "level_time", "event_open_time"):
        if col in df:
            df[col] = pd.to_datetime(df[col], utc=True, errors="coerce", format="ISO8601")
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
