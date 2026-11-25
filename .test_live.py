"""Live end-to-end test: real candles -> real study -> the exact functions the page and the worker call."""
import sys
sys.path.insert(0, "/workspace/chats/33d961658b15664da7d0d163/scratch-repo")
import pandas as pd
import providers, signals, engine

ROWS = []


def chk(name, ok, extra=""):
    ROWS.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{extra}]" if extra != "" else ""))


S = engine.merge_settings({})
S["window_lane"] = "RUNNING"
IV = pd.Timedelta(minutes=signals.INTERVAL_MIN[S["interval"]])
LABEL = {1: "BUY", -1: "SELL", 0: "none"}

# ---------------------------------------------------------------- live data + study
raw, source, quote, _ = providers.fetch_candles(S["interval"], 500, allow_usd=True)
closed, _forming = signals.closed_only(raw, S["interval"])
dfi, _levels, events = signals.run_study(closed, signals.params_from(S), S["interval"])
latest_open = closed["open_time"].iloc[-1]
d = events[events["signal_side"] != 0]
chk("study is sorted oldest -> newest", events["event_time"].is_monotonic_increasing,
    f"{len(events)} events from {source}")
chk("there is at least one defined signal", not d.empty)
chk("the study carries an EMA20", "ema20" in dfi and int(dfi["ema20"].notna().sum()) > 0,
    f"{dfi['ema20'].iloc[-1]:,.2f} on the last closed candle")
chk("EMA length is 20", signals.EMA_LENGTH == 20)
chk("the signal-age setting is gone", "max_signal_age_bars" not in S)
newest = d.sort_values("event_time").iloc[-1]
chk("newest signal is the last row by candle close", newest["event_time"] == d["event_time"].max())
chk("every defined signal has a Bullish/Bearish origin",
    all(engine.side_from_origin(o) in (-1, 1) for o in d["origin_direction"]))
print(f"     live: newest defined signal = {LABEL[engine.side_from_origin(newest['origin_direction'])]} "
      f"from level {newest['level_price']:,.1f} origin {newest['origin_direction']} "
      f"at {newest['event_time'].tz_convert('Asia/Kolkata'):%a %d %b %H:%M IST} · "
      f"{engine.signal_age(newest, latest_open, IV)} candle(s) old")

in_win = signals.in_trade_window(newest["event_time"] + IV)
why = engine.manual_entry_reason(engine.new_state(S), S, newest,
                                 engine.side_from_origin(newest["origin_direction"]), latest_open, IV, quote)
chk("Force ignores the trading hours (your click is that consent), whatever the signal's age",
    why is None, f"{why or 'allowed'} · signal candle {'inside' if in_win else 'outside'} the window")
st_live = engine.new_state(S)
engine._consider(st_live, S, newest, latest_open, IV, source, quote)
chk("the worker takes the live signal only if its own candle closed in the window",
    len(st_live["trades"]) == (1 if in_win else 0),
    st_live["signals"][-1]["action"] if st_live["signals"] else "no signal seen")

# ---------------------------------------------------------------- fixed sample, inside the window
BASE = pd.Timestamp("2026-11-24T15:15:00Z")          # Tue 20:45 IST


def mk(origin, pattern, side, minutes_ago=1, price=59000.0, base=BASE):
    return {"event_time": base - minutes_ago * IV,
            "pattern": pattern, "signal_side": side, "origin_direction": origin,
            "event_price": price, "level_invalidated": False,
            "level_time": base - (minutes_ago + 30) * IV, "level_price": 59139.8, "level_id": 12}


bear = mk("Bearish", "Strong close above level (candidate)", 1)
chk("bearish origin -> SELL", engine.side_from_origin(bear["origin_direction"]) == -1)
chk("Force allows a fresh bearish signal",
    engine.manual_entry_reason(engine.new_state(S), S, bear, -1, BASE, IV, quote) is None)

st_b = engine.new_state(S)
engine._consider(st_b, S, bear, BASE, IV, "test", quote)
chk("worker places SELL on a bearish signal", [t["side"] for t in st_b["trades"]] == ["SELL"],
    [t["side"] for t in st_b["trades"]])

st_u = engine.new_state(S)
engine._consider(st_u, S, mk("Bullish", "Strong close below level (candidate)", -1), BASE, IV, "test", quote)
chk("worker places BUY on a bullish signal", [t["side"] for t in st_u["trades"]] == ["BUY"],
    [t["side"] for t in st_u["trades"]])

# ---------------------------------------------------------------- no signal-freshness rule
LATE = pd.Timestamp("2026-11-24T17:20:00Z")          # Tue 22:50 IST, near the end of the window
old = mk("Bearish", "Strong close above level (candidate)", 1, minutes_ago=45, base=LATE)
chk("the sample signal really is 45 candles old and still in-window",
    engine.signal_age(old, LATE, IV) == 45 and signals.in_trade_window(old["event_time"] + IV))
chk("Force ignores the signal's age entirely",
    engine.manual_entry_reason(engine.new_state(S), S, old, -1, LATE, IV, quote) is None)
st_o = engine.new_state(S)
engine._consider(st_o, S, old, LATE, IV, "test", quote)
chk("worker trades a 45-candle-old in-window signal", [t["side"] for t in st_o["trades"]] == ["SELL"],
    st_o["signals"][-1]["action"])

# ---------------------------------------------------------------- no repeat-direction rule
st_rep = engine.new_state(S)
engine._consider(st_rep, S, mk("Bearish", "Strong close above level (candidate)", 1, minutes_ago=6),
                 BASE, IV, "test", quote)
st_rep["trades"][0].update(status="CLOSED", exit_reason="EMA20", exit_time=BASE.isoformat(), exit_price=58800.0)
engine._consider(st_rep, S, mk("Bearish", "Bearish rejection (candidate)", 1, minutes_ago=1),
                 BASE, IV, "test", quote)
chk("the same direction may be traded again after the last trade closed",
    len(st_rep["trades"]) == 2 and st_rep["trades"][-1]["status"] == "OPEN",
    st_rep["signals"][-1]["action"])

st_one = engine.new_state(S)
engine._consider(st_one, S, mk("Bearish", "Strong close above level (candidate)", 1, minutes_ago=8),
                 BASE, IV, "test", quote)
engine._consider(st_one, S, mk("Bearish", "Bearish rejection (candidate)", 1, minutes_ago=4),
                 BASE, IV, "test", quote)
chk("a same-direction signal keeps the single open position (no stacking)",
    len([t for t in st_one["trades"] if t["status"] == "OPEN"]) == 1, st_one["signals"][-1]["action"])
chk("Force keeps one position too",
    engine.manual_entry_reason(st_one, S, mk("Bearish", "Bearish rejection (candidate)", 1, minutes_ago=2),
                               -1, BASE, IV, quote) is not None)

# ---------------------------------------------------------------- flip on the opposite signal
st_f = engine.new_state(S)
engine._consider(st_f, S, mk("Bullish", "Strong close below level (candidate)", -1, minutes_ago=30),
                 BASE, IV, "test", quote)
engine._consider(st_f, S, bear, BASE, IV, "test", quote)
open_now = [t for t in st_f["trades"] if t["status"] == "OPEN"]
chk("open BUY is flipped to SELL by the newer bearish signal",
    len(open_now) == 1 and open_now[0]["side"] == "SELL",
    f"trades={[(t['id'], t['side'], t['status']) for t in st_f['trades']]}")

# a position left over from the older build's removed TEST lane must flip like any other
st_legacy = engine.new_state(S)
engine._consider(st_legacy, S, mk("Bullish", "Strong close below level (candidate)", -1, minutes_ago=10),
                 BASE, IV, "test", quote)
st_legacy["trades"][0]["mode"] = "TEST"              # exactly what the previous build saved
chk("a TEST-lane carry-over does not block the opposite side",
    engine.manual_entry_reason(st_legacy, S, bear, -1, BASE, IV, quote) is None,
    engine.manual_entry_reason(st_legacy, S, bear, -1, BASE, IV, quote) or "allowed")
engine._consider(st_legacy, S, bear, BASE, IV, "test", quote)
open_now = [t for t in st_legacy["trades"] if t["status"] == "OPEN"]
chk("the opposite signal flips an older TEST-lane position instead of leaving two open",
    len(open_now) == 1 and open_now[0]["side"] == "SELL" and st_legacy["trades"][0]["exit_reason"] == "REVERSED",
    f"trades={[(t['id'], t['side'], t['status'], t.get('exit_reason')) for t in st_legacy['trades']]}")

st_stop = engine.new_state(S)
engine._consider(st_stop, S, mk("Bullish", "Strong close below level (candidate)", -1, minutes_ago=10),
                 BASE, IV, "test", quote)
st_stop["trades"][0]["mode"] = "TEST"
engine.process(st_stop, S, closed, events, source, quote)
_t0 = st_stop["trades"][0]
chk("an older TEST-lane trade is adopted by worker cycle, not killed by the removed test lane",
    _t0["mode"] == "WINDOW" and _t0.get("exit_reason") != "STOPPED",   # live candles may still hit the EMA20
    f"{_t0['mode']} / {_t0['status']} / {_t0.get('exit_reason')}")

# ---------------------------------------------------------------- two automatic trades per IST day
st_cap = engine.new_state(S)
engine._consider(st_cap, S, mk("Bearish", "Strong close above level (candidate)", 1, minutes_ago=10),
                 BASE, IV, "test", quote)
engine._consider(st_cap, S, mk("Bullish", "Strong close below level (candidate)", -1, minutes_ago=6),
                 BASE, IV, "test", quote)
chk("two automatic window trades in one IST day are allowed", len(st_cap["trades"]) == 2,
    len(st_cap["trades"]))
third = mk("Bearish", "Strong close above level (candidate)", 1, minutes_ago=2)
engine._consider(st_cap, S, third, BASE, IV, "test", quote)
chk("a third automatic window trade on the same IST day is refused",
    len(st_cap["trades"]) == 2 and "window trades" in st_cap["signals"][-1]["action"],
    st_cap["signals"][-1]["action"])
chk("Force is not stopped by the cap",
    engine.manual_entry_reason(st_cap, S, third, -1, BASE, IV, quote) is None)

st_force = engine.new_state(S)
st_force["trades"] = [dict(t) for t in st_cap["trades"]]
st_force["next_id"] = 9
st_force["trades"].append(engine.build_trade(
    9, S, -1, third["pattern"], "Bearish", False, pd.Timestamp(third["level_time"]).isoformat(),
    third["level_price"], BASE.isoformat(), BASE.isoformat(), 59000.0, "test", "WINDOW", "manual", ema_entry=59100.0))
chk("a forced trade does not use up the cap",
    engine.auto_trades_on(st_force, engine.ist_date(BASE)) == 2,
    engine.auto_trades_on(st_force, engine.ist_date(BASE)))

next_base = BASE + pd.Timedelta(days=1)              # Wed 20:45 IST
st_next = engine.new_state(S)
st_next["trades"] = [dict(t) for t in st_cap["trades"]]
st_next["next_id"] = 9
engine._consider(st_next, S, mk("Bearish", "Strong close above level (candidate)", 1, base=next_base),
                 next_base, IV, "test", quote)
chk("the cap resets on the next IST day", len(st_next["trades"]) == 3, len(st_next["trades"]))

# ---------------------------------------------------------------- Force guards that still hold
chk("Force refuses without an origin",
    engine.manual_entry_reason(engine.new_state(S), S, mk("Unknown", "Touch / no defined pattern", 1), 0,
                               BASE, IV, quote) is not None)
S_stop = dict(S, window_lane="STOPPED")
chk("Force refuses when trading is stopped",
    engine.manual_entry_reason(engine.new_state(S_stop), S_stop, bear, -1, BASE, IV, quote) is not None)
S_filter = dict(S, allowed_origins="Bullish")
chk("Force still obeys the origin filter",
    engine.manual_entry_reason(engine.new_state(S_filter), S_filter, bear, -1, BASE, IV, quote) is not None)

# ---------------------------------------------------------------- EMA20 exit
def candles(closes, start=pd.Timestamp("2026-11-24T09:00:00Z")):
    idx = pd.date_range(start, periods=len(closes), freq="5min", tz="UTC")
    return pd.DataFrame({"open_time": idx, "open": closes, "high": closes, "low": closes, "close": closes})


def ema_exit(closes, side, entry_idx, entry_price):
    """Build a trade entered at candle `entry_idx` and run the real exit pass over it."""
    f = candles(closes)
    t = engine.build_trade(1, S, side, "Bearish rejection (candidate)" if side < 0 else "Bullish rejection (candidate)",
                           "Bearish" if side < 0 else "Bullish", False, f["open_time"].iloc[0].isoformat(), 100.0,
                           f["open_time"].iloc[entry_idx].isoformat(),
                           (f["open_time"].iloc[entry_idx] + IV).isoformat(), entry_price, "test", "WINDOW")
    t["window_end"] = None
    st = engine.new_state(S)
    st["trades"].append(t)
    engine._process_exits(st, f, IV)
    return t


flat = [100.0] * 25
print(f"     EMA20 with 25 flat 100 candles and one 105 print: "
      f"{signals.add_ema(candles(flat + [105.0]))['ema20'].iloc[-1]:,.2f}")

t = ema_exit(flat + [105.0, 106.0, 95.0], -1, 25, 105.0)
chk("SELL entered ABOVE the EMA20 exits when a candle closes below it",
    t["exit_reason"] == "EMA20" and abs(t["exit_price"] - 95.0) < 1e-9 and t["ema_entry_side"] == 1,
    f"{t['exit_reason']} @ {t['exit_price']} · entry side {t['ema_entry_side']}")

t = ema_exit(flat + [95.0, 96.0, 103.0], -1, 25, 95.0)
chk("SELL entered BELOW the EMA20 (EMA above the price) exits when a candle closes above it",
    t["exit_reason"] == "EMA20" and abs(t["exit_price"] - 103.0) < 1e-9 and t["ema_entry_side"] == -1,
    f"{t['exit_reason']} @ {t['exit_price']} · entry side {t['ema_entry_side']}")

t = ema_exit(flat + [105.0, 104.0, 95.0], 1, 25, 105.0)
chk("BUY entered ABOVE the EMA20 exits when a candle closes below it",
    t["exit_reason"] == "EMA20" and abs(t["exit_price"] - 95.0) < 1e-9 and t["ema_entry_side"] == 1,
    f"{t['exit_reason']} @ {t['exit_price']} · entry side {t['ema_entry_side']}")

t = ema_exit(flat + [95.0, 96.0, 103.0], 1, 25, 95.0)
chk("BUY entered BELOW the EMA20 (EMA above the price) exits when a candle closes above it",
    t["exit_reason"] == "EMA20" and abs(t["exit_price"] - 103.0) < 1e-9 and t["ema_entry_side"] == -1,
    f"{t['exit_reason']} @ {t['exit_price']} · entry side {t['ema_entry_side']}")

t = ema_exit(flat + [105.0, 106.0, 104.0, 103.0], 1, 25, 105.0)
chk("no exit while the price stays on the entry side of the EMA20",
    t["status"] == "OPEN", t.get("exit_reason"))

t = ema_exit(flat + [103.0, 99.0], -1, 25, 103.0)
chk("a SELL is never blocked by the EMA and exits on its first close across",
    t["exit_reason"] == "EMA20", f"{t['exit_reason']} @ {t.get('exit_price')}")

# ---------------------------------------------------------------- fixed-size lots (Delta: 1 lot = 0.001 BTC)
DELTA_SRC = "Delta Exchange India (BTCUSD perp)"
bt = engine.build_trade(1, S, 1, "Touch / no defined pattern", "Bullish", False,
                        "2026-11-24T14:35:00+00:00", 59000.0, "2026-11-24T15:05:00+00:00",
                        "2026-11-24T15:10:00+00:00", 59000.0, DELTA_SRC, "WINDOW")
chk("Delta data sizes the trade in whole 0.001 BTC lots (a $20 setting is one lot)",
    bt["lots"] == 1 and abs(bt["qty"] - 0.001) < 1e-15,
    f"lots={bt['lots']} qty={bt['qty']} notional=${bt['lot_usd']:,.2f}")
chk("the simulated fee follows the lot notional, not the $20 setting",
    abs(bt["lot_usd"] - 0.001 * 59000) < 1e-9 and abs(bt["contract_size"] - 0.001) < 1e-15,
    f"${bt['lot_usd']:,.2f}")
bt2 = engine.build_trade(2, dict(S, lot_usd=500.0), -1, "x", "Bearish", False,
                         "2026-11-24T14:35:00+00:00", 59000.0, "2026-11-24T15:05:00+00:00",
                         "2026-11-24T15:10:00+00:00", 59000.0, DELTA_SRC, "WINDOW")
chk("a larger USD setting becomes the nearest whole lots (8 lots for $500 at $59,000)",
    bt2["lots"] == 8 and abs(bt2["qty"] - 0.008) < 1e-15,
    f"lots={bt2['lots']} qty={bt2['qty']} notional=${bt2['lot_usd']:,.2f}")
bt3 = engine.build_trade(3, S, 1, "x", "Bullish", False, "2026-11-24T14:35:00+00:00", 59000.0,
                         "2026-11-24T15:05:00+00:00", "2026-11-24T15:10:00+00:00", 59000.0,
                         "Binance Spot (data-api.binance.vision)", "WINDOW")
chk("other providers keep the USD-notional sizing",
    bt3["lots"] is None and abs(bt3["qty"] - 20.0 / 59000.0) < 1e-15,
    f"qty={bt3['qty']:.8f} lots={bt3['lots']}")

print(f"\nBUILD: {engine.RULES_BUILD}")
print("ALL PASS" if all(ROWS) else f"SOME FAILED ({ROWS.count(False)}/{len(ROWS)})")
sys.exit(0 if all(ROWS) else 1)
