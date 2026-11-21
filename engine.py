"""Demo (paper) trading engine. Simulation only: no orders, no accounts, no credentials.

Rules
  * Only the historical study's trading window is traded: Tue/Wed/Thu 19:00-23:00 IST,
    judged by the signal candle's close time. Signals outside it are logged and skipped.
  * The side comes from the level's origin direction, exactly as the Historical event study
    shows it: a Bullish-origin level -> BUY, a Bearish-origin level -> SELL. The pattern of
    the event (strong close above/below, rejection) is only the trigger, never the direction,
    so a bearish-origin level can never produce a BUY. Nothing lifts this: no setting and not
    the Force button.
  * One position at a time. A repeat of the last traded direction is skipped, whether a
    position is open or was closed; the opposite direction flips the position at the new
    signal's price, win or lose (exit reason REVERSED), and opens the other side.
  * Entry at the signal candle's close (or at the live price for a startup catch-up and
    the Force button). Fixed notional per trade (lot_usd). There is no stop-loss or
    take-profit: a trade runs until the opposite signal, the 23:00 IST close, a manual
    Stop, or the optional max_hold_bars time exit.
  * Every window trade is closed at 23:00 IST, profit or loss, if still open.
  * The window lane is RUNNING / PAUSED / STOPPED. PAUSED = no new entries, open trades
    keep running. STOPPED = no new entries and open trades are closed at the latest price.
Candles are processed in order, so a late or restarted worker never double-trades.
"""
import traceback
import uuid

import pandas as pd

import providers
import signals
from signals import INTERVAL_MIN, SIDE_LABEL, in_trade_window

DEFAULT_SETTINGS = dict(
    initial_balance=5000.0, lot_usd=20.0, fee_pct=0.0,
    window_lane="RUNNING", test_lane="STOPPED", sleep_outside_window=False,   # test_lane: legacy TEST trades only
    max_hold_bars=0,
    allow_usd_fallback=False, allowed_origins="Both",
    skip_invalidated_events=False, max_signal_age_bars=2,
    provider="Auto (fallback chain)", provider_fallback=True,
    interval="5m", bars=500, atr_period=14, impulse_atr=1.2, wick_body_ratio=1.5,
    close_edge_pct=70, min_spacing_atr=1.0, forward_bars=6, cooldown_bars=6, confirm_closes=3,
    rules_mode="Improved (fixes)",
)


def merge_settings(saved):
    saved = dict(saved or {})
    saved.pop("test_mode", None)                      # old on/off switch, replaced by the window lane
    saved.pop("tp_pct", None)                         # SL/TP exits were removed: hold until the flip or 23:00 IST
    saved.pop("sl_pct", None)
    saved.pop("allow_counter_origin", None)            # counter-origin trades are no longer possible
    saved["test_lane"] = "STOPPED"                    # outside-window trading no longer exists
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
            if wend is not None and row.open_time + iv >= wend:
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
    if s["window_lane"] == "PAUSED":
        return "trading is paused (stays awake so Start is instant)"
    if s["window_lane"] == "RUNNING":
        if not s["sleep_outside_window"] or _near_window(now_utc):
            return "window trading is running"
    return None


def usd_ok(s, quote):
    return quote == "USDT" or bool(s.get("allow_usd_fallback")) or s.get("provider") in providers.USD_PROVIDERS


def is_single(s):
    return True                     # skip-repeats / flip-on-opposite is one position by definition


ORIGIN_SIDE = {"Bullish": 1, "Bearish": -1}

RULES_BUILD = "origin-side v3 2026-11-21"     # shown on the page so you can confirm which build is live


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


def close_counter_origin(state, price, exit_time_iso, closed=None):
    """Close any open trade whose stored level origin points against its own side.

    Such a trade can only come from a build that still allowed counter-origin entries (or a
    forced entry from before the rule became absolute). The rule is now never bypassed, so
    these positions are closed instead of left running.
    """
    n = 0
    for t in state["trades"]:
        side = int(t.get("side_num") or 0)
        origin = t.get("origin_direction")
        if t["status"] != "OPEN" or not origin or not side:
            continue
        if ORIGIN_SIDE.get(origin, side) == side:
            continue
        seen = None
        if closed is not None and t.get("event_open_time"):
            seen = closed[(closed["open_time"] > pd.Timestamp(t["event_open_time"]))
                          & (closed["open_time"] <= pd.Timestamp(exit_time_iso))]
        close_trade(t, price, "ORIGIN_FIX", exit_time_iso, None if seen is None else len(seen), seen)
        n += 1
    return n


def block_reason(state, s, origin, invalidated, level_time_iso, quote, side=None, lane="window", force=False):
    """Trader rules shared by automatic and manual entries (the timing window is checked elsewhere).

    `force` is the Force-trade button: it bypasses the repeat-direction rule and the timing
    window (that is what forcing means). The origin-alignment rule and the data-currency rule
    are never bypassed.
    """
    if s["allowed_origins"] != "Both" and origin != s["allowed_origins"]:
        return f"origin filter ({s['allowed_origins']} only)"
    if side is not None and ORIGIN_SIDE.get(origin, 0) != side:
        return "against the level's origin direction"
    if s["skip_invalidated_events"] and invalidated:
        return "level-invalidated filter"
    if side is not None and not force:
        prev = int((state.get("signal_state") or {}).get("active_side", 0))
        if prev == side:
            return f"same {SIDE_LABEL[side]} signal as the last trade (waiting for the opposite signal)"
    if not usd_ok(s, quote):
        return "USD fallback data not allowed"
    return None


def _window_end_iso(entry_time_iso):
    """23:00 IST on the IST date of the entry, as a UTC ISO string."""
    t = pd.Timestamp(entry_time_iso).tz_convert(signals.IST)
    return (t.normalize() + pd.Timedelta(hours=23)).tz_convert("UTC").isoformat()


def build_trade(trade_id, s, side, pattern, origin, invalidated, level_time_iso, level_price,
                event_open_time_iso, entry_time_iso, entry, source, mode, trigger="auto"):
    return {
        "id": trade_id, "uid": uuid.uuid4().hex[:10], "status": "OPEN", "mode": mode, "trigger": trigger,
        "side": SIDE_LABEL[side], "side_num": side, "pattern": pattern,
        "origin_direction": origin, "level_invalidated": bool(invalidated),
        "level_time": level_time_iso, "level_price": float(level_price),
        "event_open_time": event_open_time_iso, "entry_time": entry_time_iso,
        "entry_price": float(entry), "lot_usd": float(s["lot_usd"]), "qty": s["lot_usd"] / entry,
        "horizon": int(s["max_hold_bars"]), "fee_pct": float(s["fee_pct"]), "source": source,
        "window_end": _window_end_iso(entry_time_iso) if mode == "WINDOW" else None,
    }


def _consider(state, s, ev, latest_open, iv, source, quote, closed=None, entry_override=None, trigger="auto"):
    # The level's origin direction is the trade direction (Bullish -> BUY, Bearish -> SELL).
    # The pattern only decides WHEN to trade; it never overrides the side.
    side = ORIGIN_SIDE.get(ev["origin_direction"], int(ev["signal_side"]))
    close_ts = ev["event_time"] + iv
    in_win = in_trade_window(close_ts)
    level_iso = pd.Timestamp(ev["level_time"]).isoformat()
    sig = {
        "event_time": ev["event_time"].isoformat(), "pattern": ev["pattern"], "side": SIDE_LABEL[side],
        "price": float(ev["event_price"]), "origin_direction": ev["origin_direction"],
        "level_invalidated": bool(ev["level_invalidated"]), "in_trade_window": bool(in_win),
        "source": source, "trigger": trigger, "action": "",
    }
    age = (latest_open - ev["event_time"]) / iv
    if not in_win:
        reason = "outside the Tue-Thu 19:00-23:00 IST trading window"
    elif s["window_lane"] != "RUNNING":
        reason = f"trading is {s['window_lane'].lower()}"
    elif age > s["max_signal_age_bars"]:
        reason = "stale signal (worker was behind)"
    else:
        reason = block_reason(state, s, ev["origin_direction"], ev["level_invalidated"], level_iso, quote, side)
    if reason:
        sig["action"] = "SKIPPED: " + reason
    else:
        entry = float(entry_override) if entry_override is not None else float(ev["event_price"])
        entry_iso = (latest_open + iv).isoformat() if entry_override is not None else close_ts.isoformat()
        flipped = close_opposite(state, "window", side, entry, entry_iso, closed,
                                 latest_open if entry_override is not None else ev["event_time"])
        state["trades"].append(build_trade(
            state["next_id"], s, side, ev["pattern"], ev["origin_direction"], ev["level_invalidated"],
            level_iso, ev["level_price"], ev["event_time"].isoformat(), entry_iso,
            entry, source, "WINDOW", trigger))
        state["next_id"] += 1
        state["signal_state"] = {
            "active_side": side, "last_traded_event": ev["event_time"].isoformat(),
            "last_entry_time": entry_iso, "last_level_time": level_iso,
        }
        sig["action"] = "TRADED" + (f" (flipped {flipped} opposite position)" if flipped else "")
    state["signals"].append(sig)


def _catch_up(state, s, closed, events, iv, source, quote):
    """Start now = trade now: on startup, or after pressing Start, act on the newest signal
    if it is inside the window, at most `max_signal_age_bars` old, and not already traded."""
    if s["window_lane"] != "RUNNING" or events is None or events.empty:
        return
    directional = events[events["signal_side"] != 0]
    if directional.empty or open_in_lane(state, "window"):
        return
    ev = directional.iloc[-1]
    latest_open = closed["open_time"].iloc[-1]
    age = (latest_open - ev["event_time"]) / iv
    if not in_trade_window(ev["event_time"] + iv) or age > s["max_signal_age_bars"]:
        return
    if (state.get("signal_state") or {}).get("last_traded_event") == ev["event_time"].isoformat():
        return
    _consider(state, s, ev, latest_open, iv, source, quote, closed,
              entry_override=float(closed["close"].iloc[-1]), trigger="startup")


def process(state, s, closed, events, source, quote, now=None):
    """Pure state transition: apply new closed candles/events to the paper account."""
    now = now if now is not None else pd.Timestamp.now(tz="UTC")
    iv = pd.Timedelta(minutes=INTERVAL_MIN[s["interval"]])
    latest_open = closed["open_time"].iloc[-1]
    state.update(last_run_utc=now.isoformat(), last_source=source, last_quote=quote,
                 last_price=float(closed["close"].iloc[-1]), last_error=None)
    _process_exits(state, closed, iv)
    fixed = close_counter_origin(state, float(closed["close"].iloc[-1]), (latest_open + iv).isoformat(), closed)
    if fixed:                                             # never leave a counter-origin position running
        print(f"[trader] closed {fixed} position(s) pointing against their level's origin", flush=True)
    for t in state["trades"]:                              # a STOPPED lane never keeps open trades
        if t["status"] == "OPEN" and s[f"{lane_of(t)}_lane"] == "STOPPED":
            close_trade(t, float(closed["close"].iloc[-1]), "STOPPED", (latest_open + iv).isoformat())
    prev_lane = state.get("last_lane_state")
    state["last_lane_state"] = s["window_lane"]
    if state["last_candle_time"] is None:                 # first run: baseline history, then catch up
        state["last_candle_time"] = latest_open.isoformat()
        _catch_up(state, s, closed, events, iv, source, quote)
        state["signals"] = state["signals"][-100:]
        return state
    last_seen = pd.Timestamp(state["last_candle_time"])
    if events is not None and not events.empty:
        new = events[(events["event_time"] > last_seen) & (events["signal_side"] != 0)]
        for _, ev in new.sort_values("event_time").iterrows():
            _consider(state, s, ev, latest_open, iv, source, quote, closed)
    if prev_lane != "RUNNING" and s["window_lane"] == "RUNNING":    # Start pressed mid-window
        _catch_up(state, s, closed, events, iv, source, quote)
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
