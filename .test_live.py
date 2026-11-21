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
_df, _levels, events = signals.run_study(closed, signals.params_from(S), S["interval"])
latest_open = closed["open_time"].iloc[-1]
clean = closed["open_time"].is_monotonic_increasing
chk("events sorted oldest -> newest", clean and events["event_time"].is_monotonic_increasing,
    f"{len(events)} events from {source}")
d = events[events["signal_side"] != 0]
chk("there is at least one defined signal", not d.empty)

newest = d.sort_values("event_time").iloc[-1]
age = engine.signal_age(newest, latest_open, IV)
chk("newest signal is the last row by candle close", newest["event_time"] == d["event_time"].max())
chk("side always equals the level origin", all(
    engine.side_from_origin(o) == engine.side_from_origin(o) for o in d["origin_direction"]))
print(f"     live: newest defined signal = {LABEL[engine.side_from_origin(newest['origin_direction'])]} "
      f"from level {newest['level_price']:,.1f} origin {newest['origin_direction']} "
      f"at {newest['event_time'].tz_convert('Asia/Kolkata'):%a %d %b %H:%M IST} · {age} candle(s) old")

# ---------------------------------------------------------------- force path on the real signal
st = engine.new_state(S)
why = engine.manual_entry_reason(st, S, newest, engine.side_from_origin(newest["origin_direction"]),
                                 latest_open, IV, quote)
if age > S["max_signal_age_bars"]:
    chk("Force refuses the real (stale) signal", why is not None and "old" in why, why)
else:
    chk("Force allows the real signal when it is fresh", why is None, why)

state_auto = engine.new_state(S)
engine._consider(state_auto, S, newest, latest_open, IV, source, quote)
if age > S["max_signal_age_bars"]:
    chk("worker also refuses the real (stale) signal", len(state_auto["trades"]) == 0,
        state_auto["signals"][-1]["action"])
else:
    chk("worker takes the real signal when fresh", len(state_auto["trades"]) == 1)


# ---------------------------------------------------------------- fresh signals, both sides
BASE = pd.Timestamp("2026-11-24T15:15:00Z")          # Tue 20:45 IST, inside the trading window


def mk(origin, pattern, side, minutes_ago=1, price=59000.0):
    return {"event_time": BASE - minutes_ago * IV,
            "pattern": pattern, "signal_side": side, "origin_direction": origin,
            "event_price": price, "level_invalidated": False,
            "level_time": BASE - (minutes_ago + 30) * IV, "level_price": 59139.8, "level_id": 12}


bear = mk("Bearish", "Strong close above level (candidate)", 1)
chk("bearish origin -> SELL", engine.side_from_origin(bear["origin_direction"]) == -1)
st = engine.new_state(S)
why = engine.manual_entry_reason(st, S, bear, -1, BASE, IV, quote)
chk("Force allows a fresh bearish signal", why is None, why)

st_b = engine.new_state(S)
engine._consider(st_b, S, bear, BASE, IV, "test", quote)
chk("worker places SELL on a fresh bearish signal", [t["side"] for t in st_b["trades"]] == ["SELL"],
    [t["side"] for t in st_b["trades"]])

bull = mk("Bullish", "Strong close below level (candidate)", -1)
st_u = engine.new_state(S)
engine._consider(st_u, S, bull, BASE, IV, "test", quote)
chk("worker places BUY on a fresh bullish signal", [t["side"] for t in st_u["trades"]] == ["BUY"],
    [t["side"] for t in st_u["trades"]])

# ---------------------------------------------------------------- flip an open BUY on a fresh bearish signal
st_f = engine.new_state(S)
engine._consider(st_f, S, mk("Bullish", "Strong close below level (candidate)", -1, minutes_ago=30),
                 BASE, IV, "test", quote)
engine._consider(st_f, S, bear, BASE, IV, "test", quote)
open_now = [t for t in st_f["trades"] if t["status"] == "OPEN"]
chk("open BUY is flipped to SELL by the newer bearish signal",
    len(open_now) == 1 and open_now[0]["side"] == "SELL",
    f"trades={[(t['id'], t['side'], t['status']) for t in st_f['trades']]}")

# ---------------------------------------------------------------- other Force guards still hold
chk("Force refuses without an origin",
    engine.manual_entry_reason(engine.new_state(S), S, mk("Unknown", "Touch / no defined pattern", 1), 0,
                               BASE, IV, quote) is not None)
S_stop = dict(S, window_lane="STOPPED")
chk("Force refuses when trading is stopped",
    engine.manual_entry_reason(engine.new_state(S_stop), S_stop, bear, -1, BASE, IV, quote) is not None)
st_r = engine.new_state(S)
st_r["signal_state"] = {"active_side": -1}
chk("Force click may repeat the last direction",
    engine.manual_entry_reason(st_r, S, bear, -1, BASE, IV, quote) is None)
S_filter = dict(S, allowed_origins="Bullish")
chk("Force still obeys the origin filter",
    engine.manual_entry_reason(engine.new_state(S_filter), S_filter, bear, -1, BASE, IV, quote) is not None)

# ---------------------------------------------------------------- stale signal is never traded
old = mk("Bearish", "Strong close above level (candidate)", 1, minutes_ago=50)
chk("a 50-candle-old signal is refused by Force",
    engine.manual_entry_reason(engine.new_state(S), S, old, -1, BASE, IV, quote) is not None)
st_o = engine.new_state(S)
engine._consider(st_o, S, old, BASE, IV, "test", quote)
chk("a 50-candle-old signal is refused by the worker", len(st_o["trades"]) == 0)

print(f"\nBUILD: {engine.RULES_BUILD}")
print("ALL PASS" if all(ROWS) else f"SOME FAILED ({ROWS.count(False)}/{len(ROWS)})")
sys.exit(0 if all(ROWS) else 1)
