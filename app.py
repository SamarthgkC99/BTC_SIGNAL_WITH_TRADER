import hmac
import os

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from streamlit_autorefresh import st_autorefresh

import engine
import providers
import signals
import store as storemod
from signals import IST, INTERVAL_MIN, SIDE_LABEL, Params, in_trade_window

st.set_page_config(page_title="BTC Momentum-Level Study", layout="wide", initial_sidebar_state="auto")
st.markdown("""
<style>
@media (max-width: 640px) {
  .block-container {padding: 0.8rem 0.6rem 3rem 0.6rem;}
  h1 {font-size: 1.45rem !important;}
  [data-testid="stMetricValue"] {font-size: 1.2rem;}
  [data-testid="stMetricLabel"] {font-size: 0.8rem;}
}
[data-testid="stDataFrame"] {overflow-x: auto;}
</style>""", unsafe_allow_html=True)

STORE = storemod.Store()


# ---------------------------------------------------------------- login gate
def gate():
    pw = os.environ.get("APP_PASSWORD")
    if not pw:
        st.warning("No APP_PASSWORD is set: anyone with this page's URL can open it. "
                   "Set APP_PASSWORD in your host's environment variables.")
        return
    if st.session_state.get("authed"):
        return
    st.title("BTC Momentum-Level Study")
    entered = st.text_input("Password", type="password")
    if entered and hmac.compare_digest(entered.encode(), pw.encode()):
        st.session_state["authed"] = True
        st.rerun()
    elif entered:
        st.error("Wrong password.")
    st.stop()


gate()


# ---------------------------------------------------------------- helpers
@st.cache_data(ttl=15, show_spinner=False)
def read_key(key):
    try:
        return STORE.read(key), None
    except Exception as exc:                                    # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"


@st.cache_data(ttl=20, show_spinner=False)
def fetch_klines(interval: str, limit: int, provider: str, fallback: bool):
    return providers.fetch_candles(interval, limit, allow_usd=True, provider=provider, fallback=fallback)


def set_settings(**changes):
    """Merge changes into the saved settings (used by the quick buttons)."""
    current = engine.merge_settings(STORE.read("settings"))
    current.update(changes)
    STORE.write("settings", current)
    read_key.clear()


def ist_str(series):
    return series.dt.tz_convert(IST).dt.strftime("%Y-%m-%d %H:%M")


def ist_naive(series):
    return series.dt.tz_convert(IST).dt.tz_localize(None)


def next_window_start(now_utc):
    t = now_utc.tz_convert(IST)
    for d in range(0, 9):
        cand = (t + pd.Timedelta(days=d)).normalize() + pd.Timedelta(hours=19)
        if cand.weekday() in (1, 2, 3) and cand > t:
            return cand
    return None


def show_df(df, compact_cols=None, compact=False):
    if compact and compact_cols:
        df = df[[c for c in compact_cols if c in df.columns]]
    st.dataframe(df, width="stretch", hide_index=True)


S = engine.merge_settings(read_key("settings")[0])

# ---------------------------------------------------------------- sidebar
IVS = ["5m", "15m", "1h"]
with st.sidebar:
    st.header("Study settings")
    compact = st.checkbox("Mobile / compact layout", value=False)
    interval = st.selectbox("Candle timeframe", IVS, index=IVS.index(S["interval"]))
    bars = st.slider("Historical candles", 200, 1000, int(S["bars"]), 100)
    atr_period = st.slider("ATR period", 5, 50, int(S["atr_period"]))
    impulse_atr = st.slider("Impulse body ≥ ATR ×", 0.5, 3.0, float(S["impulse_atr"]), 0.1)
    wick_body_ratio = st.slider("Rejection wick/body ratio ≥", 1.0, 4.0, float(S["wick_body_ratio"]), 0.1)
    close_edge_pct = st.slider("Strong close near candle edge (%)", 50, 95, int(S["close_edge_pct"]), 5)
    min_spacing_atr = st.slider("Custom nearby-level filter: min spacing (ATR)", 0.25, 5.0,
                                float(S["min_spacing_atr"]), 0.25)
    forward_bars = st.slider("Bars to observe after event", 3, 24, int(S["forward_bars"]))
    cooldown_bars = st.slider("Cooldown bars before same level can fire again", 1, 12, int(S["cooldown_bars"]))
    confirm_closes = st.slider("Closes beyond a level that invalidate it", 2, 8, int(S["confirm_closes"]))
    st.divider()
    pch = providers.PROVIDER_CHOICES
    provider = st.selectbox("Price provider", pch, index=pch.index(S["provider"]) if S["provider"] in pch else 0)
    provider_fallback = st.checkbox("If it fails, fall back to the other providers",
                                    value=bool(S["provider_fallback"]))
    if st.button("Use this provider for the trader too"):
        try:
            set_settings(provider=provider, provider_fallback=provider_fallback)
            st.success("Saved: the trader uses this provider from its next cycle.")
        except Exception as exc:                                # noqa: BLE001
            st.error(f"Could not save: {type(exc).__name__}")
    st.divider()
    auto_refresh = st.checkbox("Auto-refresh every 30 seconds", value=True)
    refresh = st.button("Fetch fresh data now", type="primary")
    if auto_refresh:
        st_autorefresh(interval=30_000, key="btc_market_auto_refresh")

study_values = dict(interval=interval, bars=bars, atr_period=atr_period, impulse_atr=impulse_atr,
                    wick_body_ratio=wick_body_ratio, close_edge_pct=close_edge_pct,
                    min_spacing_atr=min_spacing_atr, forward_bars=forward_bars,
                    cooldown_bars=cooldown_bars, confirm_closes=confirm_closes,
                    provider=provider, provider_fallback=provider_fallback)
params = Params(atr_period=atr_period, impulse_atr=impulse_atr, wick_body_ratio=wick_body_ratio,
                close_edge_pct=close_edge_pct, min_spacing_atr=min_spacing_atr, forward_bars=forward_bars,
                cooldown_bars=cooldown_bars, confirm_closes=confirm_closes)

# ---------------------------------------------------------------- data + signals
if refresh:
    fetch_klines.clear()
    read_key.clear()

data_ok, fetch_error = True, None
try:
    with st.spinner("Loading public BTC candles with API fallback…"):
        df, data_source, quote_currency, provider_errors = fetch_klines(interval, bars, provider, provider_fallback)
except Exception as exc:                                        # noqa: BLE001
    data_ok, fetch_error = False, str(exc)

if data_ok:
    chart_df = df.copy()
    closed, forming = signals.closed_only(df, interval)
    if len(closed) < 50:
        data_ok, fetch_error = False, "Not enough fully closed candles after excluding the current candle."

if data_ok:
    dfi, levels, events = signals.run_study(closed, params, interval)
    live_price = float(chart_df["close"].iloc[-1])
else:
    events, levels, live_price = pd.DataFrame(), [], None

st.title("BTC Momentum-Level Historical Study")
st.caption("Educational research + demo (paper) trading only • No real orders • No credentials for any exchange • Public market data")

tab_study, tab_pnl, tab_act, tab_set = st.tabs(["📈 Study", "💰 Live P&L", "🧾 Trading activity", "⚙️ Settings"])

# ================================================================= STUDY TAB
with tab_study:
    if not data_ok:
        st.error(f"Could not load public data: {fetch_error}")
        st.info("Check your connection. The app tried several public candle endpoints.")
    else:
        iv_td = pd.Timedelta(minutes=INTERVAL_MIN[interval])
        directional = events[events["signal_side"] != 0] if not events.empty else events
        now_utc = pd.Timestamp.now(tz="UTC")
        win_open = in_trade_window(now_utc)
        if not directional.empty:
            last = directional.iloc[-1]
            ago = int((closed["open_time"].iloc[-1] - last["event_time"]) / iv_td)
            label = SIDE_LABEL[int(last["signal_side"])]
            when = last["event_time"].tz_convert(IST).strftime("%a %d %b %H:%M IST")
            took = in_trade_window(last["event_close_time"])
            msg = (f"**Latest signal: {label}** · {last['pattern'].replace(' (candidate)', '')} · "
                   f"candle {when} · {ago} candle(s) ago · price {last['event_price']:,.2f} {quote_currency} · "
                   f"{'inside' if took else 'outside'} the Tue–Thu 7–11pm IST trade window")
            (st.success if ago <= 3 else st.info)(msg)
        else:
            st.info("No directional signal in the loaded sample yet.")
        st.caption(f"Trade window right now: {'OPEN' if win_open else 'closed'} · signals are shown at any day/time; "
                   "the demo trader only trades inside the window unless Test mode is on (Settings).")

        latest = closed.iloc[-1]
        m1, m2, m3 = st.columns(3)
        m1.metric("Last closed candle (IST)", latest["open_time"].tz_convert(IST).strftime("%Y-%m-%d %H:%M"))
        m2.metric(f"Last closed close ({quote_currency})", f'{latest["close"]:,.2f}')
        m3.metric("Candidate momentum levels", f"{len(levels)}")

        if forming:
            live = chart_df.iloc[-1]
            st.info(
                f"A candle is currently forming (shown on the chart, excluded from calculations). "
                f"Open {live['open_time'].tz_convert(IST).strftime('%H:%M:%S IST')} · "
                f"O {live['open']:,.2f} / H {live['high']:,.2f} / L {live['low']:,.2f} / "
                f"latest {live['close']:,.2f} {quote_currency}. Provisional until the interval ends.")
        st.caption(f"Active data source: {data_source} · Quote currency: {quote_currency} · "
                   f"Candles loaded: {len(df)} · Auto-refresh: {'on (30s)' if auto_refresh else 'off'}")
        if provider != "Auto (fallback chain)" and not data_source.startswith(provider.split()[0]):
            st.info(f"Your chosen provider ({provider}) failed, so data comes from {data_source}.")
        if quote_currency != "USDT":
            st.warning("USD fallback data is being displayed. The demo trader pauses on USD data unless you allow it in Settings.")
        if provider_errors:
            with st.expander("API fallback diagnostics"):
                for item in provider_errors:
                    st.text(item)

        st.subheader("Historical price and candidate momentum-origin levels (IST)")
        x_all = ist_naive(chart_df["open_time"])
        fig = go.Figure(data=[go.Candlestick(
            x=x_all, open=chart_df["open"], high=chart_df["high"], low=chart_df["low"],
            close=chart_df["close"], name=f"BTC/{quote_currency}")])
        x_end = x_all.iloc[-1]
        for n, lv in enumerate(levels, start=1):
            x0 = lv["time"].tz_convert(IST).tz_localize(None)
            inv = bool(lv.get("invalidated"))
            fig.add_shape(type="line", x0=x0, x1=x_end, y0=lv["price"], y1=lv["price"],
                          line=dict(dash="dot", width=1, color="rgba(120,120,120,%s)" % (0.35 if inv else 0.8)))
            if not compact:
                fig.add_annotation(x=x_end, y=lv["price"], xanchor="left", showarrow=False, font=dict(size=9),
                                   text=f"L{n} {lv['direction']}" + (" • invalidated" if inv else ""))
        if not events.empty:
            style = {1: ("#16a34a", "triangle-up"), -1: ("#dc2626", "triangle-down"), 0: ("#9ca3af", "circle")}
            for pat, grp in events.groupby("pattern"):
                col, sym = style[int(grp["signal_side"].iloc[0])]
                fig.add_trace(go.Scatter(
                    x=ist_naive(grp["event_time"]), y=grp["event_price"], mode="markers",
                    marker=dict(size=9, color=col, symbol=sym), name=pat,
                    hovertemplate="%{x}<br>Price: %{y:.2f}<br>" + pat + "<extra></extra>"))
        fig.update_layout(height=380 if compact else 650, xaxis_rangeslider_visible=False,
                          xaxis_title="Time (IST)", yaxis_title=f"Price ({quote_currency})",
                          legend_orientation="h", margin=dict(l=10, r=10, t=30, b=10))
        st.plotly_chart(fig, width="stretch")

        st.subheader("Method definition used in this prototype")
        st.markdown("""
- **Impulse candle:** body ≥ the selected multiple of ATR. **Momentum origin:** the impulse candle's open (placeholder definition).
- **Nearby-level filter:** a candidate is kept only if it is at least the selected ATR distance from existing levels.
- **Rejection:** wick/body ratio, candle direction and close location meet the thresholds.
- **Strong break:** a candle that closes on the far side of the level with a large body near its edge. Checked **before** rejection patterns.
- **Invalidation:** a strong break, or N consecutive closes beyond the level, ends the level (checked on every candle, touch or not).
- **One event per candle:** if several levels fire on the same candle, the defined pattern / most recent level is kept. Cooldown defaults to the forward horizon so measurement windows don't overlap.
- **Direction-adjusted columns:** `fwd_return_dir_pct`, `favorable_excursion_pct`, `adverse_excursion_pct` flip the sign for SELL signals. The raw `future_*` columns are kept as-is.
- **`trade` column:** BUY/SELL if the signal falls inside Tue–Thu 7–11pm IST, otherwise "signal only". `future_*` columns are measured after the signal, so they are analysis only, never an entry condition.
""")

        st.subheader("Historical event study (IST)")
        if events.empty:
            st.warning("No level-touch events met the current definitions in this sample.")
        else:
            v = events.copy()
            v["in_trade_window"] = v["event_close_time"].apply(in_trade_window)
            side_lbl = v["signal_side"].map(SIDE_LABEL)
            v["trade"] = np.where(v["signal_side"] == 0, "", np.where(v["in_trade_window"], side_lbl, "signal only"))
            v["event_time_ist"] = ist_str(v["event_time"])
            v["level_time_ist"] = ist_str(v["level_time"])
            v = v.sort_values("event_time", ascending=False)
            cols = ["event_time_ist", "pattern", "trade", "origin_direction", "level_invalidated",
                    "event_price", "level_price", "level_id", "level_time_ist", "in_trade_window",
                    "forward_close_change_pct", "future_high_excursion_pct", "future_low_excursion_pct",
                    "fwd_return_dir_pct", "favorable_excursion_pct", "adverse_excursion_pct"]
            view = v[cols].round(4)
            show_df(view, ["event_time_ist", "pattern", "trade", "origin_direction", "level_invalidated",
                           "future_high_excursion_pct", "fwd_return_dir_pct"], compact)
            st.download_button("Download event-study CSV (IST)", data=view.to_csv(index=False).encode("utf-8"),
                               file_name="btc_momentum_level_events_ist.csv", mime="text/csv")
            st.subheader("Descriptive outcomes by observed pattern")
            show_df(signals.summarize(events).round(4),
                    ["pattern", "events", "avg_dir_return_pct", "dir_win_rate_pct", "avg_future_high_excursion_pct"],
                    compact)
        st.warning("Research prototype. Outcomes are descriptive, exclude fees/slippage/funding, and do not prove an edge. "
                   "Fallbacks may switch from BTC/USDT to BTC/USD, which is labelled.")

# ================================================================= LIVE P&L TAB
state, state_err = read_key("state")
state = state or engine.new_state(S)
price_for_pnl = live_price if live_price else state.get("last_price")

with tab_pnl:
    if state_err:
        st.error(f"Storage read problem: {state_err}")
    st.caption(f"Storage: {STORE.backend()}" + (" — ⚠️ data is lost on restart; connect Upstash in Settings"
               if not storemod.get_creds()[0] else ""))
    now_utc = pd.Timestamp.now(tz="UTC")
    if in_trade_window(now_utc):
        st.success("Trade window OPEN (Tue/Wed/Thu 7–11pm IST)")
    else:
        nxt = next_window_start(now_utc)
        st.info("Trade window closed" + (f" · next opens {nxt.strftime('%a %d %b %H:%M IST')}" if nxt is not None else ""))
    st.caption(f"Test mode: {'ON — signals outside the window are also traded and tagged TEST' if S['test_mode'] else 'OFF — window trades only'}"
               f" · last worker run: {state.get('last_run_utc') or 'never'} · data: {state.get('last_source') or '-'}")
    if state.get("last_error"):
        st.warning(f"Worker's last error: {state['last_error']}")
    st.subheader("Test controls")
    tc1, tc2 = st.columns(2)
    if S["test_mode"]:
        if tc1.button("⏹ Stop test mode (enforce the window)"):
            try:
                set_settings(test_mode=False)
                st.rerun()
            except Exception as exc:                            # noqa: BLE001
                st.error(f"Could not save: {type(exc).__name__}")
    else:
        if tc1.button("▶ Start test mode (bypass the window)", type="primary"):
            try:
                set_settings(test_mode=True)
                st.rerun()
            except Exception as exc:                            # noqa: BLE001
                st.error(f"Could not save: {type(exc).__name__}")
    if tc2.button("➕ Test trade now (follows the current signal)"):
        now_t = pd.Timestamp.now(tz="UTC")
        in_win_now = in_trade_window(now_t)
        dsig = events[events["signal_side"] != 0] if (data_ok and not events.empty) else pd.DataFrame()
        if not data_ok or live_price is None:
            st.error("No live data available right now.")
        elif not (in_win_now or S["test_mode"]):
            st.warning("Outside the Tue–Thu 7–11pm IST window and test mode is stopped. "
                       "Press 'Start test mode' first to force a test trade.")
        elif dsig.empty:
            st.warning("There is no directional signal in the loaded sample to follow.")
        else:
            last = dsig.iloc[-1]
            side = int(last["signal_side"])
            level_iso = last["level_time"].isoformat()
            try:
                fresh = STORE.read("state") or engine.new_state(S)          # uncached: latest account state
                why = engine.block_reason(fresh, S, last["origin_direction"], bool(last["level_invalidated"]),
                                          level_iso, quote_currency)
                if why:
                    st.warning(f"Blocked by your trader rules: {why}. (Only the timing window is bypassed in a test.)")
                else:
                    item = engine.build_trade(
                        fresh["next_id"], S, side, last["pattern"], last["origin_direction"],
                        bool(last["level_invalidated"]), level_iso, last["level_price"],
                        chart_df["open_time"].iloc[-1].isoformat(), now_t.isoformat(), live_price,
                        data_source, "WINDOW" if in_win_now else "TEST", "manual")
                    fresh["next_id"] += 1
                    fresh["trades"].append(item)
                    fresh["signals"].append({
                        "event_time": last["event_time"].isoformat(), "pattern": last["pattern"],
                        "side": SIDE_LABEL[side], "price": float(live_price),
                        "origin_direction": last["origin_direction"], "level_invalidated": bool(last["level_invalidated"]),
                        "in_trade_window": bool(in_win_now), "source": data_source, "action": "TRADED (manual test)"})
                    STORE.write("state", fresh)
                    read_key.clear()
                    state = fresh
                    ago = int((closed["open_time"].iloc[-1] - last["event_time"]) / pd.Timedelta(minutes=INTERVAL_MIN[interval]))
                    st.success(f"Placed {item['mode']} {SIDE_LABEL[side]} at {live_price:,.2f} following the "
                               f"{last['pattern'].replace(' (candidate)', '')} signal from {ago} candle(s) ago.")
            except Exception as exc:                                        # noqa: BLE001
                st.error(f"Could not place the test trade: {type(exc).__name__}")
    st.caption("A test trade is placed instantly when the window is open or test mode is started. It uses your Demo "
               "trader settings (lot, TP/SL, fee, filters, max open trades).")
    mode = st.radio("Show", ["All", "WINDOW", "TEST"], horizontal=True)
    sm = engine.account_summary(state, mode, price_for_pnl)
    a1, a2, a3 = st.columns(3)
    a1.metric("Equity (USD)", f"{sm['equity']:,.2f}", f"{sm['equity'] - sm['initial']:+,.4f}")
    a2.metric("Balance (realized)", f"{sm['balance']:,.2f}")
    a3.metric("Unrealized P&L", f"{sm['unrealized']:+,.4f}")
    b1, b2, b3 = st.columns(3)
    b1.metric("Open / closed trades", f"{sm['open']} / {sm['closed']}")
    b2.metric("Win rate", "-" if sm["win_rate"] is None else f"{sm['win_rate']:.0f}%")
    b3.metric("Realized P&L", f"{sm['realized']:+,.4f}")
    curve = sm["curve"]
    cfig = go.Figure()
    if len(curve):
        xs = [ist_naive(curve["time"]).iloc[0] - pd.Timedelta(minutes=5)] + list(ist_naive(curve["time"]))
        ys = [sm["initial"]] + list(curve["balance"])
        cfig.add_trace(go.Scatter(x=xs, y=ys, mode="lines+markers", line_shape="hv", name="Balance"))
    cfig.update_layout(height=260 if compact else 340, yaxis_title="Balance (USD)", xaxis_title="Time (IST)",
                       margin=dict(l=10, r=10, t=20, b=10))
    st.plotly_chart(cfig, width="stretch")
    td = engine.trades_df(state, mode)
    st.subheader("Open positions")
    opened = td[td["status"] == "OPEN"].copy() if not td.empty else td
    if opened is None or len(opened) == 0:
        st.caption("No open positions.")
    else:
        if price_for_pnl:
            opened["unrealized_usd"] = opened["qty"] * (price_for_pnl - opened["entry_price"]) * opened["side_num"]
        opened["entry_ist"] = ist_str(opened["entry_time"])
        show_df(opened[["id", "mode", "side", "pattern", "entry_ist", "entry_price", "tp", "sl", "unrealized_usd"]].round(4),
                ["id", "side", "entry_price", "unrealized_usd"], compact)

# ================================================================= ACTIVITY TAB
with tab_act:
    st.subheader("Trades")
    td = engine.trades_df(state, "All")
    if td.empty:
        st.caption("No trades yet. Trades appear here once the worker sees a signal "
                   "(inside the window, or anytime with Test mode on).")
    else:
        td = td.sort_values("entry_time", ascending=False).copy()
        td["entry_ist"] = ist_str(td["entry_time"])
        td["exit_ist"] = ist_str(td["exit_time"]).fillna("") if td["exit_time"].notna().any() else ""
        cols = ["id", "mode", "trigger", "status", "side", "pattern", "origin_direction", "level_invalidated", "entry_ist",
                "entry_price", "exit_ist", "exit_price", "exit_reason", "bars_held", "pnl_usd",
                "future_high_excursion_pct", "source"]
        out = td[[c for c in cols if c in td.columns]].round(4)
        show_df(out, ["id", "mode", "side", "entry_ist", "exit_reason", "pnl_usd", "future_high_excursion_pct"], compact)
        st.download_button("Download trades CSV (IST)", data=out.to_csv(index=False).encode("utf-8"),
                           file_name="paper_trades_ist.csv", mime="text/csv")
    st.subheader("Recent signals seen by the trader")
    sg = pd.DataFrame(state.get("signals", []))
    if sg.empty:
        st.caption("No signals recorded by the worker yet.")
    else:
        sg["event_time"] = pd.to_datetime(sg["event_time"], utc=True)
        sg["time_ist"] = ist_str(sg["event_time"])
        sg = sg.sort_values("event_time", ascending=False)
        show_df(sg[["time_ist", "side", "pattern", "price", "origin_direction", "level_invalidated",
                    "in_trade_window", "action"]].round(4), ["time_ist", "side", "in_trade_window", "action"], compact)

# ================================================================= SETTINGS TAB
with tab_set:
    st.subheader("Storage connection (Upstash Redis)")
    url_now, _, origin = storemod.get_creds()
    st.caption({"env": "Connected via environment variables (permanent).",
                "saved": "Connected via credentials saved on this server (lost if the server is rebuilt — "
                         "use environment variables to make it permanent).",
                None: "Not connected: using a local file (lost on restart on free hosting)."}[origin])
    with st.form("conn"):
        up_url = st.text_input("Upstash REST URL", placeholder="https://xxxx.upstash.io")
        up_token = st.text_input("Upstash REST token", type="password")
        go_save = st.form_submit_button("Save & test connection")
    if go_save:
        if not up_url or not up_token:
            st.error("Enter both the REST URL and the token.")
        else:
            try:
                storemod.test_connection(up_url, up_token)
                storemod.save_creds(up_url, up_token)
                read_key.clear()
                st.success("Connected and saved. The background trader will use it on its next cycle. "
                           "For permanence also add them as UPSTASH_REDIS_REST_URL / UPSTASH_REDIS_REST_TOKEN on your host.")
            except Exception as exc:                            # noqa: BLE001
                st.error(f"Connection failed: {type(exc).__name__}. Check the URL and token.")
    if origin == "saved" and st.button("Remove saved connection"):
        storemod.clear_creds()
        read_key.clear()
        st.rerun()
    st.caption("Tokens are never shown or logged. No exchange or trading credentials are ever needed — market data is public.")

    st.subheader("Demo trader settings")
    with st.form("trade_settings"):
        c1, c2 = st.columns(2)
        lot_usd = c1.number_input("Lot size (USD notional per trade)", 1.0, 100000.0, float(S["lot_usd"]), 1.0)
        initial_balance = c2.number_input("Initial balance (USD, applies on account reset)", 100.0, 10_000_000.0,
                                          float(S["initial_balance"]), 100.0)
        tp_pct = c1.number_input("Take-profit (%)", 0.05, 10.0, float(S["tp_pct"]), 0.05)
        sl_pct = c2.number_input("Stop-loss (%)", 0.05, 10.0, float(S["sl_pct"]), 0.05)
        fee_pct = c1.number_input("Fee per side (%) — simulated", 0.0, 1.0, float(S["fee_pct"]), 0.01)
        max_open = c2.number_input("Max open trades", 1, 20, int(S["max_open"]), 1)
        st.caption("Test mode start/stop is on the Live P&L tab.")
        origins = ["Both", "Bullish", "Bearish"]
        allowed_origins = st.selectbox("Only trade levels with origin direction", origins,
                                       index=origins.index(S["allowed_origins"]))
        skip_inv = st.checkbox("Skip events that invalidate their level (breakout events)",
                               value=bool(S["skip_invalidated_events"]))
        allow_usd = st.checkbox("Allow trading on USD fallback data (Coinbase/Kraken)",
                                value=bool(S["allow_usd_fallback"]))
        use_sidebar = st.checkbox("Also save the current sidebar study settings for the trader", value=True)
        saved = st.form_submit_button("Save settings")
    if saved:
        new = dict(S, lot_usd=lot_usd, initial_balance=initial_balance, tp_pct=tp_pct, sl_pct=sl_pct,
                   fee_pct=fee_pct, max_open=int(max_open), allowed_origins=allowed_origins,
                   skip_invalidated_events=skip_inv, allow_usd_fallback=allow_usd)
        if use_sidebar:
            new.update(study_values)
        try:
            STORE.write("settings", new)
            read_key.clear()
            st.success("Settings saved.")
        except Exception as exc:                                # noqa: BLE001
            st.error(f"Could not save settings: {type(exc).__name__}")
    st.caption(f"Trader currently uses: {S['interval']} candles · impulse ≥ {S['impulse_atr']}×ATR · "
               f"provider {S['provider']} · horizon {S['forward_bars']} bars · TP/SL {S['tp_pct']}% / {S['sl_pct']}% · lot ${S['lot_usd']:,.0f}")

    with st.expander("Reset demo account"):
        sure = st.checkbox("I understand this deletes all trades and restarts from the initial balance")
        if st.button("Reset account", disabled=not sure):
            try:
                STORE.write("state", engine.new_state(S))
                read_key.clear()
                st.success("Account reset.")
            except Exception as exc:                            # noqa: BLE001
                st.error(f"Reset failed: {type(exc).__name__}")
    st.caption("App login password: set APP_PASSWORD as an environment variable on your host (it is intentionally not a page field).")
