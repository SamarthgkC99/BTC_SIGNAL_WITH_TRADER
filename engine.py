"""Demo (paper) trading engine. Simulation only: no orders, no accounts, no credentials.

Rules
  * Only the historical study's trading window is traded: Tue/Wed/Thu 19:00-23:00 IST,
    judged by the signal candle's close time. Signals outside it are logged and skipped.
  * The side comes from the level's origin direction, exactly as the Historical event study
    shows it: a Bullish-origin level -> BUY, a Bearish-origin level -> SELL. The pattern of
    the event (strong close above/below, rejection) is only the trigger, never the direction,
    so a bearish-origin level can never produce a BUY. Nothing lifts this: no setting and not
    the Force button.
  * No signal-freshness rule and no repeat-direction rule. However old the newest signal is,
    if its own candle closed inside a trading window the trader takes it (once per signal event,
    so a restart cannot trade the same one twice). The same direction can be traded again on a
    new signal; while that position is still open the single position is simply kept.
  * Two automatic window trades per IST day. Entries the worker places are capped at two per IST
    date (flips included). The Force button is the unlimited test lane: it is not counted and
    never capped.
  * One position at a time. A new signal in the other direction flips the position at that
    signal's price, win or lose (exit reason REVERSED), and opens the other side.
  * Entry at the signal candle's close (or at the live price for a startup catch-up and
    the Force button). Fixed notional per trade (lot_usd). There is no stop-loss or
    take-profit: a trade runs until the EMA20 exit, the opposite signal, the 23:00 IST close,
    a manual Stop, or the optional max_hold_bars time exit.
  * EMA20 trend exit (close only). A 20-candle EMA on the trader's own interval. A SELL is held
    while the price stays below the EMA20 and exits (reason EMA20) the first time a candle
    CLOSES above it; a BUY is mirrored and exits on a close below it. The crossing that goes
    with the trade - a SELL falling through the EMA, a BUY rising through it - is the move being
    followed, so it never closes the trade. Where the EMA sits at entry never blocks the trade.
    A trade entered on the wrong side (a SELL signalled above the EMA, a BUY below it) is managed
    once a candle has first closed on its own side, so the entry itself cannot trigger the exit.
    A touch of the EMA is never enough: the candle has to close across it.
  * Every window trade is closed at 23:00 IST, profit or loss, if still open.
  * The Force button is a manual test entry: your click is the consent for the trading hours
    only, so it may trade outside the window (and such a trade has no 23:00 close). Every other
    rule still applies: lane RUNNING, origin direction, level-invalidated, data currency and the
    one-position-at-a-time rule. It is not counted in the two-per-day window cap.
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
    initial_balance=5000.0, lot_usd=20.0, size_lots=1, fee_pct=0.0, usd_inr=0.0,
    window_lane="RUNNING", test_lane="STOPPED", sleep_outside_window=False,   # test_lane: legacy TEST trades only
    max_hold_bars=0,
    allow_usd_fallback=False, allowed_origins="Both",
    skip_invalidated_events=False,
    provider="Auto (fallback chain)", provider_fallback=True,
    interval="5m", bars=500, atr_period=14, impulse_atr=1.2, wick_body_ratio=1.5,
    close_edge_pct=70, min_spacing_atr=1.0, forward_bars=6, cooldown_bars=6, confirm_closes=3,
    rules_mode="Improved (fixes)",
    max_window_trades=4,              # automatic window entries per IST day; Force/test is unlimited
    carry_in_signal=True,             # at the window's first cycle, use that day's newest signal
)


def merge_settings(saved):
    saved = dict(saved or {})
    saved.pop("test_mode", None)                      # old on/off switch, replaced by the window lane
    saved.pop("tp_pct", None)                         # SL/TP exits were removed: hold until the flip or 23:00 IST
    saved.pop("sl_pct", None)
    saved.pop("allow_counter_origin", None)            # counter-origin trades are no longer possible
    saved.pop("max_signal_age_bars", None)             # the signal-freshness rule was removed
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


def _entry_ema_side(t, closed, iv):
    """Which side of the EMA20 the entry price was on (+1 above, -1 below), and the EMA value.

    Stored on the trade when it is opened. A trade opened by an older build has no anchor yet, so
    it is taken from the EMA on its own entry candle (or the oldest loaded candle if that candle
    has already rolled out of the 500-candle sample).
    """
    side = int(t.get("ema_entry_side") or 0)
    if side:
        return side, t.get("ema_entry")
    c = closed if "ema20" in closed else signals.add_ema(closed)
    entry_open = pd.Timestamp(t["entry_time"]) - iv          # the entry candle's open time
    row = c[c["open_time"] == entry_open]
    if row.empty:
        row = c.head(1)
    if row.empty or pd.isna(row["ema20"].iloc[0]):
        return 0, None
    ema = float(row["ema20"].iloc[0])
    side = ema_side(float(t["entry_price"]), ema) or (1 if int(t["side_num"]) > 0 else -1)
    t["ema_entry_side"], t["ema_entry"] = side, ema
    return side, ema


def _ema_exit_signal(row, side_num, armed):
    """(exit_now, armed) for this candle under the EMA20 trend exit.

    A SELL exits when a candle CLOSES above the EMA20, a BUY when it closes below it. The other
    crossing (a SELL closing below, a BUY closing above) is the trade's own direction, so it is
    held - it only arms the exit. A trade entered on the wrong side of the EMA therefore cannot
    be closed out by the entry itself: it is managed after its first close on its own side.
    """
    ema = getattr(row, "ema20", None)
    if ema is None or pd.isna(ema):
        return False, armed
    close, ema = float(row.close), float(ema)
    if (close - ema) * int(side_num) > 0:      # closed on the trade's own side (SELL below, BUY above)
        return False, True
    return armed, armed                        # closed against the trade: exit once it was armed


def _process_exits(state, closed, iv):
    if "ema20" not in closed:
        closed = signals.add_ema(closed)
    for t in state["trades"]:
        if t["status"] != "OPEN":
            continue
        # Exit only on candles that opened after the ENTRY candle. A carry-in trade is entered at
        # the current price, much later than the signal candle it came from, so using the signal's
        # open time here would let candles from before the entry close the position.
        entry_open, h = pd.Timestamp(t["entry_time"]) - iv, int(t["horizon"])
        wend = pd.Timestamp(t["window_end"]) if t.get("window_end") else None
        if len(closed) and closed["open_time"].iloc[0] > entry_open:      # candles after entry are no longer loaded
            close_trade(t, float(closed["close"].iloc[-1]), "HISTORY", (closed["open_time"].iloc[-1] + iv).isoformat())
            continue
        after = closed[closed["open_time"] > entry_open]
        if h > 0:
            after = after.head(h)
        entry_side, _ema = _entry_ema_side(t, closed, iv)
        side_num = int(t["side_num"])
        # Armed = the trade already sits on its own side of the EMA (a SELL entered below it),
        # so the first close against it is an exit. A wrong-side entry arms on its first own-side close.
        armed = bool(entry_side) and entry_side * side_num > 0
        for k, row in enumerate(after.itertuples(index=False), start=1):
            if wend is not None and row.open_time + iv >= wend:
                price, reason = float(row.close), "WINDOW_END"
            elif h > 0 and k >= h:
                price, reason = float(row.close), "TIME"
            else:
                hit, armed = _ema_exit_signal(row, side_num, armed)
                if not hit:
                    continue
                price, reason = float(row.close), "EMA20"     # a candle closed against the trade's side
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


def side_from_origin(origin):
    """The trade side IS the level's origin direction shown in the event study:
    Bullish level -> BUY, Bearish level -> SELL. 0 means the level has no usable origin
    direction, and then there is NO trade — the pattern side is never used as a fallback."""
    return ORIGIN_SIDE.get(str(origin).strip().title(), 0) if origin is not None else 0


def signal_age(ev, latest_open, iv):
    """How many candles have closed since the signal's candle closed (display only)."""
    return int((pd.Timestamp(latest_open) - pd.Timestamp(ev["event_time"])) / iv)


MAX_WINDOW_TRADES_PER_DAY = 2        # fallback only: the live limit is the `max_window_trades` setting


def max_window_trades(s):
    """The day's automatic window-entry limit from the settings (Force/test is never capped)."""
    try:
        return max(1, int(s.get("max_window_trades", MAX_WINDOW_TRADES_PER_DAY)))
    except (TypeError, ValueError):
        return MAX_WINDOW_TRADES_PER_DAY


def ist_date(ts):
    """The IST calendar date of a timestamp (a UTC ISO string works too)."""
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    return t.tz_convert(signals.IST).date()


def auto_trades_on(state, day):
    """Automatic (worker/startup) window entries opened on that IST date. Forced trades do not count."""
    n = 0
    for t in state.get("trades", []):
        if t.get("mode") != "WINDOW" or t.get("trigger") == "manual" or not t.get("entry_time"):
            continue
        if ist_date(t["entry_time"]) == day:
            n += 1
    return n


def ema_side(price, ema):
    """Which side of the EMA the price is on: +1 above, -1 below, 0 exactly on it."""
    return 1 if price > ema else (-1 if price < ema else 0)


def ema_side_ok(price, ema, side):
    """True when the trade's side fits where price sits relative to the EMA20 (BUY above, SELL below).
    None when the EMA or the side is unknown, so a caller can skip the check."""
    if ema is None or side == 0 or pd.isna(ema):
        return None
    return (float(price) > float(ema)) if side > 0 else (float(price) < float(ema))


def force_entry_guard(price, ema, atr, ev, max_atr=2.0):
    """Why the Force button must not follow this signal at the current price, or None when it may.

    A signal is only usable while price still sits where it fired: the live price must be within
    max_atr x ATR of the signal's own entry price, and the signal's side must agree with the side
    of the EMA20. This is what stops a day-old BUY being forced into a market that has already
    fallen far below its level.
    """
    side = side_from_origin(ev["origin_direction"])
    if side == 0:
        return "the level has no origin direction (not Bullish or Bearish)"
    entry = float(ev["event_price"])
    price = float(price)
    if atr is not None and pd.notna(atr) and float(atr) > 0:
        drift = abs(price - entry)
        if drift > max_atr * float(atr):
            return (f"the signal is stale: price {price:,.2f} is {drift:,.2f} away from its entry "
                    f"{entry:,.2f}, more than {max_atr:g} x ATR ({float(atr):,.2f})")
    if ema_side_ok(price, ema, side) is False:
        return (f"the trend is against this signal: it is {SIDE_LABEL.get(side, side)} but price {price:,.2f} "
                f"is {'below' if side > 0 else 'above'} the EMA20 {float(ema):,.2f}")
    return None


def ema_at(closed, ts):
    """The EMA20 value on the candle that opened at `ts` (None when that candle is not loaded)."""
    if closed is None or len(closed) == 0:
        return None
    c = closed if "ema20" in closed else signals.add_ema(closed)
    if pd.Timestamp(ts) < pd.Timestamp(c["open_time"].iloc[0]):
        return None
    row = c[c["open_time"] == pd.Timestamp(ts)]
    if row.empty or pd.isna(row["ema20"].iloc[0]):
        return None
    return float(row["ema20"].iloc[0])


def manual_entry_reason(state, s, ev, side, latest_open, iv, quote, price=None, ema=None, atr=None):
    """Everything the Force button checks before it may place a trade.

    Your click is the consent for exactly one rule: the trading hours (the caller ignores them).
    The lane, the origin direction, your settings filters and the one-position-at-a-time rule still
    apply, and the two-per-day window cap does not: the Force lane is the unlimited test lane.
    When the caller passes the live price (plus the EMA20 and ATR), a signal that no longer matches
    the market is refused too: see force_entry_guard.
    Returns the blocking reason, or None when the trade may be placed.
    """
    if s["window_lane"] != "RUNNING":
        return f"trading is {s['window_lane'].lower()}"
    if side == 0:
        return "the level has no origin direction (not Bullish or Bearish)"
    if price is not None:
        guard = force_entry_guard(price, ema, atr, ev)
        if guard:
            return guard
    return block_reason(state, s, ev["origin_direction"], bool(ev["level_invalidated"]),
                        pd.Timestamp(ev["level_time"]).isoformat(), quote, side, "window")


RULES_BUILD = "ema20 trend exit v19 2026-11-28"     # shown on the page so you can confirm which build is live


def open_in_lane(state, lane):
    return [t for t in state["trades"] if t["status"] == "OPEN" and lane_of(t) == lane]


def open_trades(state):
    """Every open trade. There is only one lane now, so one position at a time is global: a trade
    saved by an older build in the removed TEST lane counts exactly like a window trade."""
    return [t for t in state.get("trades", []) if t["status"] == "OPEN"]


def adopt_legacy_lane(state):
    """Move trades saved by an older build (mode TEST, the removed second lane) into the one lane.

    Without this they are invisible to the flip rule and to the one-position guard, and the legacy
    'test lane is stopped' rule would close them at the next cycle instead of the EMA20 managing
    them. Returns how many were adopted.
    """
    n = 0
    for t in state.get("trades", []):
        if t.get("mode") != "WINDOW":
            t["mode"] = "WINDOW"
            n += 1
    return n


def close_opposite(state, lane, side, price, exit_time_iso, closed=None, upto_open_time=None):
    """Flip: close the open trades that point the other way. lane=None means any lane (there is one)."""
    n = 0
    for t in state["trades"]:
        if t["status"] != "OPEN" or (lane is not None and lane_of(t) != lane):
            continue
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


def block_reason(state, s, origin, invalidated, level_time_iso, quote, side=None, lane="window"):
    """Trader rules, shared by automatic entries and by the Force-trade button.

    The origin filter, the origin direction, the level-invalidated filter, the data-currency rule
    and one-position-at-a-time always apply. There is no signal-freshness rule and no
    repeat-direction rule: a same-direction signal with no position open is a normal trade again.
    """
    if side is not None and side == 0:
        return "the level has no origin direction (not Bullish or Bearish), so no side can be traded"
    if s["allowed_origins"] != "Both" and origin != s["allowed_origins"]:
        return f"origin filter ({s['allowed_origins']} only)"
    if side is not None and ORIGIN_SIDE.get(origin, 0) != side:
        return "against the level's origin direction"
    if s["skip_invalidated_events"] and invalidated:
        return "level-invalidated filter"
    if side is not None and any(int(t["side_num"]) == side for t in open_trades(state)):
        return f"a {SIDE_LABEL[side]} is already open (one position at a time)"
    if not usd_ok(s, quote):
        return "USD fallback data not allowed"
    return None


def _window_end_iso(entry_time_iso):
    """23:00 IST on the IST date of the entry, as a UTC ISO string."""
    t = pd.Timestamp(entry_time_iso).tz_convert(signals.IST)
    return (t.normalize() + pd.Timedelta(hours=23)).tz_convert("UTC").isoformat()


def build_trade(trade_id, s, side, pattern, origin, invalidated, level_time_iso, level_price,
                event_open_time_iso, entry_time_iso, entry, source, mode, trigger="auto", ema_entry=None):
    entry = float(entry)
    # A source that trades fixed contracts (Delta's BTCUSD perpetual: 1 lot = 0.001 BTC) is sized by
    # a lot count, exactly like the exchange's own quantity box. The stored lot_usd becomes the
    # traded notional, so the simulated fee follows the real position size.
    contract = providers.contract_size(source)
    lots = None
    if contract > 0:
        lots = max(1, int(round(float(s.get("size_lots", 1)))))
        qty = lots * contract
        lot_usd = qty * entry
    else:
        qty, lot_usd = float(s["lot_usd"]) / entry, float(s["lot_usd"])
    return {
        "id": trade_id, "uid": uuid.uuid4().hex[:10], "status": "OPEN", "mode": mode, "trigger": trigger,
        "side": SIDE_LABEL[side], "side_num": side, "pattern": pattern,
        "origin_direction": origin, "level_invalidated": bool(invalidated),
        "level_time": level_time_iso, "level_price": float(level_price),
        "event_open_time": event_open_time_iso, "entry_time": entry_time_iso,
        "entry_price": entry, "lot_usd": lot_usd, "qty": qty, "lots": lots, "contract_size": contract or None,
        "horizon": int(s["max_hold_bars"]), "fee_pct": float(s["fee_pct"]), "source": source,
        # Where the EMA sits at entry never blocks a trade. The stored side lets the EMA20 trend
        # exit arm a wrong-side entry only after its first close on the trade's own side.
        "ema_entry": None if ema_entry is None else float(ema_entry),
        "ema_entry_side": 0 if ema_entry is None else ema_side(float(entry), float(ema_entry)),
        "window_end": _window_end_iso(entry_time_iso) if mode == "WINDOW" else None,
    }


def _consider(state, s, ev, latest_open, iv, source, quote, closed=None, entry_override=None, trigger="auto",
              allow_any_window=False):
    # The level's origin direction is the trade direction (Bullish -> BUY, Bearish -> SELL).
    # The pattern only decides WHEN to trade; it never overrides the side.
    side = side_from_origin(ev["origin_direction"])
    close_ts = ev["event_time"] + iv
    in_win = in_trade_window(close_ts)
    level_iso = pd.Timestamp(ev["level_time"]).isoformat()
    sig = {
        "event_time": ev["event_time"].isoformat(), "pattern": ev["pattern"], "side": SIDE_LABEL.get(side, "none"),
        "price": float(ev["event_price"]), "origin_direction": ev["origin_direction"],
        "level_invalidated": bool(ev["level_invalidated"]), "in_trade_window": bool(in_win),
        "source": source, "trigger": trigger, "action": "",
    }
    entry_iso = (latest_open + iv).isoformat() if entry_override is not None else close_ts.isoformat()
    used_today = auto_trades_on(state, ist_date(entry_iso))
    cap = max_window_trades(s)
    if side == 0:
        reason = "the level has no origin direction (not Bullish or Bearish)"
    elif not in_win and not allow_any_window:
        reason = "outside the Tue-Thu 19:00-23:00 IST trading window"
    elif s["window_lane"] != "RUNNING":
        reason = f"trading is {s['window_lane'].lower()}"
    elif used_today >= cap:
        reason = (f"the day's {cap} automatic window trades are already used "
                  f"(a Force/test trade still works)")
    else:
        reason = block_reason(state, s, ev["origin_direction"], ev["level_invalidated"], level_iso, quote, side)
    if reason:
        sig["action"] = "SKIPPED: " + reason
    else:
        entry = float(entry_override) if entry_override is not None else float(ev["event_price"])
        ref_open = latest_open if entry_override is not None else ev["event_time"]
        flipped = close_opposite(state, None, side, entry, entry_iso, closed, ref_open)
        state["trades"].append(build_trade(
            state["next_id"], s, side, ev["pattern"], ev["origin_direction"], ev["level_invalidated"],
            level_iso, ev["level_price"], ev["event_time"].isoformat(), entry_iso,
            entry, source, "WINDOW", trigger, ema_entry=ema_at(closed, ref_open)))
        state["next_id"] += 1
        state["signal_state"] = {
            "last_traded_event": ev["event_time"].isoformat(),
            "last_entry_time": entry_iso, "last_level_time": level_iso,
        }
        sig["action"] = "TRADED" + (f" (flipped {flipped} opposite position)" if flipped else "")
        if allow_any_window and not in_win:
            sig["action"] += " (carried in from earlier the same day)"
    state["signals"].append(sig)


def _catch_up(state, s, closed, events, iv, source, quote, now=None):
    """Start now = trade now: on startup, or after pressing Start, take the newest signal whose
    own candle closed inside the trading window - however old it is - unless it was already
    traded. The two-per-day window cap still applies, and a trade opened while the window is
    shut has no 23:00 close waiting for it."""
    if s["window_lane"] != "RUNNING" or events is None or events.empty:
        return
    directional = events[events["signal_side"] != 0]
    if directional.empty or open_trades(state):
        return
    ev = directional.iloc[-1]
    latest_open = closed["open_time"].iloc[-1]
    if not in_trade_window(ev["event_time"] + iv):
        return
    if (state.get("signal_state") or {}).get("last_traded_event") == ev["event_time"].isoformat():
        return
    before = len(state["trades"])
    _consider(state, s, ev, latest_open, iv, source, quote, closed,
              entry_override=float(closed["close"].iloc[-1]), trigger="startup")
    if len(state["trades"]) > before and now is not None and not in_trade_window(now):
        state["trades"][-1]["window_end"] = None      # started late: nothing to close at 23:00


def _carry_in(state, s, closed, events, iv, source, quote, now):
    """The window is open and no automatic trade has been made today: take that day's newest signal,
    even if its own candle closed before 19:00, at the current price.

    The window is four hours long and a qualifying pattern lands inside it on barely one evening in
    three, so this is what turns an evening into a trade. It never doubles up - one carry-in per IST
    day, only before that day's first automatic trade - and the 23:00 close ends it like any other.
    """
    if not s.get("carry_in_signal", True) or s["window_lane"] != "RUNNING":
        return
    if not in_trade_window(now):                    # only inside Tue-Thu 19:00-23:00 IST
        return
    if events is None or events.empty or open_trades(state):
        return
    today = ist_date(now)
    if auto_trades_on(state, today) > 0:
        return
    directional = events[events["signal_side"] != 0]
    if directional.empty:
        return
    ev = directional.iloc[-1]
    if ist_date(ev["event_time"] + iv) != today:
        return
    if (state.get("signal_state") or {}).get("last_traded_event") == ev["event_time"].isoformat():
        return
    _consider(state, s, ev, closed["open_time"].iloc[-1], iv, source, quote, closed,
              entry_override=float(closed["close"].iloc[-1]), trigger="carry-in", allow_any_window=True)


def process(state, s, closed, events, source, quote, now=None):
    """Pure state transition: apply new closed candles/events to the paper account."""
    now = now if now is not None else pd.Timestamp.now(tz="UTC")
    iv = pd.Timedelta(minutes=INTERVAL_MIN[s["interval"]])
    latest_open = closed["open_time"].iloc[-1]
    state.update(last_run_utc=now.isoformat(), last_source=source, last_quote=quote,
                 last_price=float(closed["close"].iloc[-1]), last_error=None)
    adopted = adopt_legacy_lane(state)
    if adopted:                                           # old TEST-lane trades run under the same rules now
        print(f"[trader] adopted {adopted} trade(s) from the removed test lane", flush=True)
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
        _catch_up(state, s, closed, events, iv, source, quote, now)
        if in_trade_window(now):
            _carry_in(state, s, closed, events, iv, source, quote, now)
        state["signals"] = state["signals"][-100:]
        return state
    last_seen = pd.Timestamp(state["last_candle_time"])
    if events is not None and not events.empty:
        new = events[(events["event_time"] > last_seen) & (events["signal_side"] != 0)]
        for _, ev in new.sort_values("event_time").iterrows():
            _consider(state, s, ev, latest_open, iv, source, quote, closed)
    if prev_lane != "RUNNING" and s["window_lane"] == "RUNNING":    # Start pressed mid-window
        _catch_up(state, s, closed, events, iv, source, quote, now)
    if in_trade_window(now):                                         # the window's first cycle
        _carry_in(state, s, closed, events, iv, source, quote, now)
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
