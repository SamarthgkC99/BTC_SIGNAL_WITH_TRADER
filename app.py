import hashlib
import hmac
import os
import time

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import streamlit.components.v1 as components
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
</style>""", unsafe_allow_html=True)

STORE = storemod.Store()


# ---------------------------------------------------------------- login gate
COOKIE, REMEMBER_DAYS = "btc_auth", 7


def _token(pw, exp):
    return f"{exp}.{hmac.new(pw.encode(), str(exp).encode(), hashlib.sha256).hexdigest()}"


def _token_ok(pw, tok):
    try:
        exp, sig = tok.split(".", 1)
        return int(exp) > time.time() and hmac.compare_digest(sig, _token(pw, exp).split(".", 1)[1])
    except Exception:                                           # noqa: BLE001
        return False


def _cookie_js(value, max_age):
    return ("<script>try{window.parent.document.cookie='%s=%s; max-age=%d; path=/; SameSite=Lax; Secure';}"
            "catch(e){}</script>" % (COOKIE, value, max_age))


def gate():
    pw = os.environ.get("APP_PASSWORD")
    if not pw:
        st.warning("No APP_PASSWORD is set: anyone with this page's URL can open it. "
                   "Set APP_PASSWORD in your host's environment variables.")
        return
    if st.session_state.get("authed"):
        return
    try:                                                        # a valid "remember me" cookie skips the login
        if _token_ok(pw, st.context.cookies.get(COOKIE, "")):
            st.session_state["authed"] = True
            return
    except Exception:                                           # noqa: BLE001
        pass
    box = st.empty()
    with box.container():
        st.title("BTC Momentum-Level Study")
        entered = st.text_input("Password", type="password")
        st.caption(f"After you log in, this browser is remembered for {REMEMBER_DAYS} days.")
    if entered and hmac.compare_digest(entered.encode(), pw.encode()):
        st.session_state["authed"] = True
        box.empty()
        components.html(_cookie_js(_token(pw, int(time.time()) + REMEMBER_DAYS * 86400), REMEMBER_DAYS * 86400), height=0)
        return
    if entered:
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


@st.cache_data(ttl=5, show_spinner=False)
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
    st.dataframe(df, width="stretch", hide_index=True)       # all columns always shown (scroll sideways)


S = engine.merge_settings(read_key("settings")[0])


FX_KEY = "usd_inr"                                       # the auto-captured USD -> INR rate


def saved_fx():
    try:
        return STORE.read(FX_KEY) or {}
    except Exception:                                      # noqa: BLE001
        return {}


@st.cache_data(ttl=900, show_spinner=False)
def auto_usd_inr():
    """Auto-captured USD -> INR rate: (rupees per $1, captured UTC ISO, source).

    The background capturer refreshes the stored rate hourly; this also refreshes it when the
    stored one is stale (for example after the host slept), so the rupee figures never use an old rate.
    """
    saved = saved_fx()
    try:
        age = (pd.Timestamp.now(tz="UTC") - pd.Timestamp(saved["captured_utc"])).total_seconds()
    except Exception:                                      # noqa: BLE001
        age = None
    if float(saved.get("rate") or 0) > 0 and age is not None and 0 <= age < 3600:
        return float(saved["rate"]), saved["captured_utc"], saved.get("source") or "saved"
    rate, source, when = providers.usd_inr_quote()
    try:
        STORE.write(FX_KEY, {"rate": rate, "captured_utc": when, "source": source})
    except Exception:                                      # noqa: BLE001
        pass
    return rate, when, source


def fx_ist(when):
    try:
        return pd.Timestamp(when).tz_convert(IST).strftime("%d %b %Y %H:%M IST")
    except Exception:                                      # noqa: BLE001
        return str(when or "-")


FX_RATE, FX_WHEN, FX_SOURCE = auto_usd_inr()
RATE_SET = float(S.get("usd_inr", 0.0) or 0.0)          # 0 = use the auto-captured rate
INR = RATE_SET if RATE_SET > 0 else FX_RATE             # rupees per USD for every money figure
RATE_LABEL = (f"your saved rate of ₹{RATE_SET:,.2f}/$" if RATE_SET > 0 else
              f"the auto-captured rate of ₹{FX_RATE:,.2f}/$, captured {fx_ist(FX_WHEN)} from {FX_SOURCE}")


def _inr_digits(value, dec=2):
    """Indian grouping: 4,64,650.00 instead of 464,650.00."""
    whole, frac = f"{abs(float(value)):.{dec}f}".split(".")
    if len(whole) > 3:
        head, tail, groups = whole[:-3], whole[-3:], []
        while head:
            groups.insert(0, head[-2:])
            head = head[:-2]
        whole = ",".join(groups) + "," + tail
    return whole, frac


def inr(amount, dec=2):
    """A USD amount shown in rupees at this page's rate."""
    whole, frac = _inr_digits(float(amount) * INR, dec)
    return f"₹{whole}.{frac}"


def inr_signed(amount, dec=2):
    v = float(amount) * INR
    whole, frac = _inr_digits(v, dec)
    return f"{'+' if v >= 0 else '-'}₹{whole}.{frac}"

# ---------------------------------------------------------------- sidebar
IVS = ["5m", "15m", "1h"]
with st.sidebar:
    st.header("Study settings")
    if os.environ.get("APP_PASSWORD") and st.button("Log out of this browser"):
        components.html(_cookie_js("", 0), height=0)
        st.session_state["authed"] = False
        st.success("Logged out. Reload the page to log in again.")
        st.stop()
    compact = st.checkbox("Mobile / compact layout", value=False)
    RULES = ["Legacy (first version)", "Improved (fixes)"]
    if "cooldown_bars" not in st.session_state:
        st.session_state["cooldown_bars"] = int(S["cooldown_bars"])

    def _rules_changed():                      # switching rules also switches the cooldown to that version's default
        st.session_state["cooldown_bars"] = 3 if st.session_state["rules_mode"].startswith("Legacy") else 6

    rules_mode = st.selectbox("Event rules", RULES, index=RULES.index(S["rules_mode"]) if S["rules_mode"] in RULES else 0,
                              key="rules_mode", on_change=_rules_changed)
    legacy = rules_mode.startswith("Legacy")
    interval = st.selectbox("Candle timeframe", IVS, index=IVS.index(S["interval"]))
    bars = st.slider("Historical candles", 200, 1000, int(S["bars"]), 100)
    atr_period = st.slider("ATR period", 5, 50, int(S["atr_period"]))
    impulse_atr = st.slider("Impulse body ≥ ATR ×", 0.5, 3.0, float(S["impulse_atr"]), 0.1)
    wick_body_ratio = st.slider("Rejection wick/body ratio ≥", 1.0, 4.0, float(S["wick_body_ratio"]), 0.1)
    close_edge_pct = st.slider("Strong close near candle edge (%)", 50, 95, int(S["close_edge_pct"]), 5)
    min_spacing_atr = st.slider("Custom nearby-level filter: min spacing (ATR)", 0.25, 5.0,
                                float(S["min_spacing_atr"]), 0.25)
    forward_bars = st.slider("Bars to observe after event", 3, 24, int(S["forward_bars"]))
    cooldown_bars = st.slider("Cooldown bars before same level can fire again", 1, 12, key="cooldown_bars")
    confirm_closes = st.slider("Closes beyond a level that invalidate it (Improved rules only)", 2, 8,
                               int(S["confirm_closes"]), disabled=legacy)
    st.divider()
    study_values = {}
    pch = providers.PROVIDER_CHOICES
    provider = st.selectbox("Price provider", pch, index=pch.index(S["provider"]) if S["provider"] in pch else 0)
    provider_fallback = st.checkbox("If it fails, fall back to the other providers",
                                    value=bool(S["provider_fallback"]))
    if provider.startswith("Delta"):
        st.caption("Delta Exchange India BTCUSD perpetual: **0.05% taker / 0.02% maker per side** on the notional, "
                   "plus 18% GST. Set 'Fee per side' to 0.059 (taker) or 0.0236 (maker) to include it in the paper P&L.")
    study_values = dict(interval=interval, bars=bars, atr_period=atr_period, impulse_atr=impulse_atr,
                        wick_body_ratio=wick_body_ratio, close_edge_pct=close_edge_pct,
                        min_spacing_atr=min_spacing_atr, forward_bars=forward_bars, cooldown_bars=cooldown_bars,
                        confirm_closes=confirm_closes, provider=provider, provider_fallback=provider_fallback,
                        rules_mode=rules_mode)
    if st.button("Apply these study settings + provider to the trader"):
        try:
            set_settings(**study_values)
            st.success("Saved: the trader uses these settings from its next cycle.")
        except Exception as exc:                                # noqa: BLE001
            st.error(f"Could not save: {type(exc).__name__}")
    st.divider()
    auto_refresh = st.checkbox("Auto-refresh every 5 seconds", value=True)
    refresh = st.button("Fetch fresh data now", type="primary")
    if auto_refresh:
        st_autorefresh(interval=5_000, key="btc_market_auto_refresh")

params = Params(atr_period=atr_period, impulse_atr=impulse_atr, wick_body_ratio=wick_body_ratio,
                close_edge_pct=close_edge_pct, min_spacing_atr=min_spacing_atr, forward_bars=forward_bars,
                cooldown_bars=cooldown_bars, confirm_closes=confirm_closes, legacy=legacy)

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
        ema_study = (float(dfi["ema20"].iloc[-1])
                     if ("ema20" in dfi and pd.notna(dfi["ema20"].iloc[-1])) else None)
        price_now = float(closed["close"].iloc[-1])
        today_pick, today_why = engine.pick_force_event(events, pd.Timestamp.now(tz=IST).date(),
                                                        price_now, ema_study)
        if today_pick is None:
            st.info(f"**No level to trade today:** {today_why}. Only today's levels are traded, so a signal from an "
                    f"earlier day is history — it is never carried into today.")
        else:
            t_side = SIDE_LABEL.get(engine.side_from_origin(today_pick["origin_direction"])) or "no side"
            t_when = pd.Timestamp(today_pick["event_time"]).tz_convert(IST).strftime("%a %d %b %H:%M IST")
            t_pattern = str(today_pick["pattern"]).replace(" (candidate)", "")
            st.success(
                f"**Today's signal: {t_side}** — today's newest {today_pick['origin_direction']}-origin level at "
                f"{today_pick['level_price']:,.2f} (level {int(today_pick['level_id'])}), {t_pattern.lower()} on "
                f"{t_when}. Price {price_now:,.2f} {quote_currency} is "
                f"{'above' if price_now > ema_study else 'below'} the EMA20 {ema_study:,.2f}, and only a "
                f"{'BUY' if price_now > ema_study else 'SELL'} fits that side, so {t_side} is what the trader takes · "
                f"{'window OPEN' if win_open else 'window closed right now'}")
        st.caption(f"Trade window right now: {'OPEN' if win_open else 'closed'} · signals are shown at any day/time; "
                   "signals inside the window go to the Window lane, all other times to the Test lane (Live P&L tab).")

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
                   f"Candles loaded: {len(df)} · Auto-refresh: {'on (5s)' if auto_refresh else 'off'}")
        if provider != "Auto (fallback chain)" and not data_source.startswith(provider.split()[0]):
            st.info(f"Your chosen provider ({provider}) failed, so data comes from {data_source}.")
        if quote_currency != "USDT":
            st.info(f"{quote_currency}-quoted data from {data_source}. The trader keeps running on it when you picked "
                    f"that provider; on the Auto chain USD data needs 'Allow trading on USD data' in Settings.")
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
        LEGACY_LINES = """- **Event rules: Legacy (first version).** A level is checked only on candles that touch it. Rejection patterns are checked before breaks. A strong break needs the candle to open on one side of the level and close on the other, with a big body near its edge, and ends the level. Cooldown applies after every event. No de-duplication across levels."""
        IMPROVED_LINES = """- **Event rules: Improved.** Strong break is checked before rejection patterns. A level also ends after N consecutive closes beyond it, checked on every candle (touch or not). One event per candle: if several levels fire together, the defined pattern / most recent level is kept."""
        st.markdown("""
- **Impulse candle:** body ≥ the selected multiple of ATR. **Momentum origin:** the impulse candle's open (placeholder definition).
- **Nearby-level filter:** a candidate is kept only if it is at least the selected ATR distance from existing levels.
- **Rejection:** wick/body ratio, candle direction and close location meet the thresholds.
{RULE_LINES}
- **Direction-adjusted columns:** `fwd_return_dir_pct`, `favorable_excursion_pct`, `adverse_excursion_pct` flip the sign for SELL signals. The raw `future_*` columns are kept as-is.
- **`origin_direction` vs `signal`:** `origin_direction` is the direction of the candle that created the level. `signal` (BUY/SELL) comes from the pattern: a breakout always goes against its level's origin, so a SELL after a break below a Bullish-origin level is expected. The trader takes the side from `origin_direction`, never from the pattern: a Bearish-origin level is always traded as a SELL and a Bullish-origin level always as a BUY, and the pattern only decides when that trade happens.
- **`trade` column:** BUY/SELL if the signal falls inside Tue–Thu 7–11pm IST, otherwise "signal only". `future_*` columns are measured after the signal, so they are analysis only, never an entry condition.
""".replace("{RULE_LINES}", LEGACY_LINES if legacy else IMPROVED_LINES))

        st.subheader("Historical event study (IST)")
        if events.empty:
            st.warning("No level-touch events met the current definitions in this sample.")
        else:
            v = events.copy()
            v["in_trade_window"] = v["event_close_time"].apply(in_trade_window)
            v["trade_side_by_origin"] = v["origin_direction"].map(
                lambda o: SIDE_LABEL.get(engine.side_from_origin(o)) or "none")
            # `trade` is the side the trader really takes (the origin side). Every level touch is a
            # signal now, so no row is excluded; outside the window it shows as "signal only".
            v["trade"] = np.where(v["in_trade_window"], v["trade_side_by_origin"], "signal only")
            order = st.radio("Order", ["By level, then time (as in the first version)", "Newest first"],
                             horizontal=True)
            v = (v.sort_values(["level_id", "event_time"]) if order.startswith("By level")
                 else v.sort_values("event_time", ascending=False))
            _pk, _pk_why = engine.pick_force_event(events, pd.Timestamp.now(tz=IST).date(),
                                                   float(closed["close"].iloc[-1]), ema_study)
            if _pk is not None:
                _pt = _pk["event_time"]
                v["trader_pick"] = np.where(v["event_time"] == _pt, "⬅ trader uses this (today's level)", "")
            else:
                v["trader_pick"] = ""
            v["level_time"] = v["level_time"].dt.tz_convert(IST).astype(str)      # e.g. 2026-11-06 11:00:00+05:30
            v["event_time"] = v["event_time"].dt.tz_convert(IST).astype(str)
            cols = ["trader_pick", "level_id", "level_time", "level_price", "origin_direction", "trade_side_by_origin",
                    "event_time", "event_price",
                    "pattern", "signal", "forward_close_change_pct", "future_high_excursion_pct",
                    "future_low_excursion_pct", "level_invalidated",
                    "trade", "in_trade_window", "signal_side", "fwd_return_dir_pct",
                    "favorable_excursion_pct", "adverse_excursion_pct"]
            view = v[cols].round(5)
            if _pk is None:
                st.caption(f"**No level to trade today:** {_pk_why}. Only today's levels are traded, so every row "
                           f"from an earlier day is history.")
            else:
                st.caption(
                    f"**Level the trader uses** (today's newest level on the EMA20's side, not the top row): "
                    f"{SIDE_LABEL.get(engine.side_from_origin(_pk['origin_direction'])) or 'no side'} — level "
                    f"{_pk['level_price']:,.2f}, origin {_pk['origin_direction']}, "
                    f"event {pd.Timestamp(_pk['event_time']).tz_convert(IST).strftime('%a %d %b %H:%M IST')}, "
                    f"level created {pd.Timestamp(_pk['level_time']).tz_convert(IST).strftime('%a %d %b %H:%M IST')}. "
                    f"The row marked '⬅ trader uses this' is the one the Force button takes, and a column sort you "
                    f"clicked can show an older row at the top. **Every level touch is a trade now**: a touch with "
                    f"no strong close or rejection is only labelled 'Level touch (traded, all signals)' and it "
                    f"opens the same origin-side trade as any other signal. "
                    f"It is {engine.signal_age(_pk, closed['open_time'].iloc[-1], iv_td)} candle(s) old. Only "
                    f"TODAY's levels are traded (window trades and Force alike), up to "
                    f"{engine.max_window_trades(S)} automatic window trades per IST day; a row from an earlier day "
                    f"never opens anything.")
            show_df(view)
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
    st.caption(f"Last worker run: {state.get('last_run_utc') or 'never'} · data: {state.get('last_source') or '-'}")
    if state.get("last_error"):
        st.warning(f"Worker's last error: {state['last_error']}")

    LANE_ICON = {"RUNNING": "🟢 RUNNING", "PAUSED": "🟡 PAUSED", "STOPPED": "🔴 STOPPED"}

    def close_lane_now(lane):
        price = live_price or state.get("last_price")
        if not price:
            return None                                          # the trader closes them at its next candle
        fresh = STORE.read("state") or engine.new_state(S)
        n = engine.close_lane_trades(fresh, lane, float(price), pd.Timestamp.now(tz="UTC").isoformat(), "STOPPED")
        if n:
            STORE.write("state", fresh)
        return n

    def lane_set(lane, status):
        try:
            set_settings(**{f"{lane}_lane": status})
            note = f"{lane.capitalize()} trading is now {status}."
            if status == "STOPPED":
                n = close_lane_now(lane)
                note += (" No live price right now: open trades will be closed at the trader's next candle." if n is None
                         else f" Closed {n} open trade(s) at the live price." if n else " No open trades to close.")
            st.session_state["lane_note"] = note
            read_key.clear()
        except Exception as exc:                                # noqa: BLE001
            st.error(f"Could not save: {type(exc).__name__}")
            return
        st.rerun()

    st.subheader("Trader controls")
    note = st.session_state.get("lane_note")
    if note:
        st.success(note)
        del st.session_state["lane_note"]
    cur = S["window_lane"]
    st.markdown(f"**Window trading** — signals inside Tue–Thu 7–11pm IST, nothing outside it  \n{LANE_ICON[cur]}")
    b1, b2, b3 = st.columns(3)
    if b1.button("▶ Start", key="window_start", disabled=cur == "RUNNING", type="primary",
                 help="Allow new trades inside the window."):
        lane_set("window", "RUNNING")
    if b2.button("⏸ Pause", key="window_pause", disabled=cur != "RUNNING",
                 help="No new trades. An open trade keeps running to its exit. The app stays awake."):
        lane_set("window", "PAUSED")
    if b3.button("⏹ Stop", key="window_stop", disabled=cur == "STOPPED",
                 help="No new trades, and the open trade is closed at the live price."):
        lane_set("window", "STOPPED")
    awake = engine.needs_awake(S, state)
    if awake:
        st.info(f"The app stays awake: {awake}.")
    else:
        st.success("😴 Trading is stopped with no open trade: the app stops pinging itself so Render can sleep "
                   "and save free instance hours. If it stays awake, an outside pinger (cron-job.org) is still calling it.")
    st.caption("Start = new window trades allowed (and the newest in-window signal is taken, however old) · Pause = no new "
               "trades, the open one runs on, app stays awake · Stop = no new trades, open trade closed at the live price. "
               f"Automatic entries are capped at {engine.max_window_trades(S)} per IST day"
               + (", and the day's newest signal is carried into the window when it opens."
                  if S.get("carry_in_signal", True) else "."))
    def _res(kind, text):
        st.session_state["test_result"] = (kind, text, pd.Timestamp.now(tz=IST).strftime("%H:%M:%S"))

    if st.button("➕ Force trade now (test lane — unlimited, trades outside the window too)"):
        now_t = pd.Timestamp.now(tz="UTC")
        in_win_now = in_trade_window(now_t)
        ema_pick = (float(dfi["ema20"].iloc[-1])
                    if (data_ok and "ema20" in dfi and pd.notna(dfi["ema20"].iloc[-1])) else None)
        last, why_pick = (engine.pick_force_event(events, pd.Timestamp.now(tz=IST).date(),
                                                  float(live_price), ema_pick)
                          if (data_ok and live_price is not None and ema_pick is not None) else (None, None))
        if not data_ok or live_price is None:
            _res("error", "No live data available right now.")
        elif ema_pick is None:
            _res("warning", "The EMA20 is not ready yet, so the side that fits the market is unknown. "
                             "Try again in a few candles.")
        elif last is None:
            _res("warning", f"Nothing placed — {why_pick}.")
        else:
            side = engine.side_from_origin(last["origin_direction"])
            level_iso = last["level_time"].isoformat()
            try:
                fresh = STORE.read("state") or engine.new_state(S)          # uncached: latest account state
                engine.adopt_legacy_lane(fresh)                             # old TEST-lane trades flip like any other
                # Your click is the consent for the trading hours only. The origin direction, your
                # settings filters and the one-position-at-a-time rule still apply.
                why = engine.manual_entry_reason(fresh, S, last, side, closed["open_time"].iloc[-1],
                                                 iv_td, quote_currency)
                if why:
                    hint = next((h for k, h in {
                        "trading is": "Press ▶ Start to allow new trades, then Force again.",
                        "a SELL is already open": "One position at a time: let the open trade exit on the EMA20, "
                                                 "the opposite signal or Stop before forcing another one.",
                        "a BUY is already open": "One position at a time: let the open trade exit on the EMA20, "
                                                "the opposite signal or Stop before forcing another one.",
                        "the EMA20 trend is against this signal": "A BUY is only taken above the EMA20 and a SELL "
                                                                  "only below it. Wait for a level whose origin "
                                                                  "matches the trend.",
                        "the level has no origin": "The newest signal's level has no Bullish/Bearish origin, so the "
                                                 "origin rule cannot pick a side. Wait for the next signal.",
                        "level-invalidated": "Untick 'Skip events that invalidate their level' in Settings and save.",
                        "origin filter": "Set 'Only trade levels with origin direction' to Both in Settings.",
                        "USD fallback": "Tick 'Allow trading on USD fallback data' in Settings, or pick a USDT provider."}.items()
                        if why.startswith(k)), "")
                    _res("warning", f"Not placed: blocked by your trader rule — {why}. {hint} "
                                    "(Force overrides the trading hours only — your click is that consent — so the "
                                    "origin direction, your settings filters and the one-position-at-a-time rule "
                                    "still block it. It is never counted in the two-per-day window cap.)")
                else:
                    ema_ref = (float(dfi["ema20"].iloc[-1])
                               if ("ema20" in dfi and pd.notna(dfi["ema20"].iloc[-1])) else None)
                    item = engine.build_trade(
                        fresh["next_id"], S, side, last["pattern"], last["origin_direction"],
                        bool(last["level_invalidated"]), level_iso, last["level_price"],
                        chart_df["open_time"].iloc[-1].isoformat(), now_t.isoformat(), live_price,
                        data_source, "WINDOW", "manual", ema_entry=ema_ref)
                    if not in_win_now:                 # forced outside hours: there is no 11pm close to wait for
                        item["window_end"] = None
                    flipped = (engine.close_opposite(fresh, None, side, float(live_price), now_t.isoformat())
                               if engine.is_single(S) else 0)
                    fresh["next_id"] += 1
                    fresh["signal_state"] = {"last_traded_event": last["event_time"].isoformat(),
                                             "last_entry_time": now_t.isoformat(), "last_level_time": level_iso}
                    fresh["trades"].append(item)
                    fresh["signals"].append({
                        "event_time": last["event_time"].isoformat(), "pattern": last["pattern"],
                        "side": SIDE_LABEL[side], "price": float(live_price),
                        "origin_direction": last["origin_direction"], "level_invalidated": bool(last["level_invalidated"]),
                        "in_trade_window": bool(in_win_now), "source": data_source, "action": "TRADED (forced)"})
                    STORE.write("state", fresh)
                    read_key.clear()
                    state = fresh
                    ago = int((closed["open_time"].iloc[-1] - last["event_time"]) / pd.Timedelta(minutes=INTERVAL_MIN[interval]))
                    _kind = last["pattern"].replace(" (candidate)", "").replace("(traded, all signals)", "signal")
                    _res("success", f"Placed {item['mode']} {SIDE_LABEL[side]} at {live_price:,.2f} following today's "
                               f"{_kind} from {ago} candle(s) ago — "
                               f"level {last['level_price']:,.2f} (origin {last['origin_direction']})"
                               f" = {SIDE_LABEL[side]}. Below is the level and origin the side came from."
                               + (f" EMA20 {ema_ref:,.2f} is {'below' if live_price > ema_ref else 'above'} the entry; "
                                  f"this {SIDE_LABEL[side]} exits only when a candle CLOSES "
                                  f"{'above' if side < 0 else 'below'} the EMA20, so a move "
                                  f"{'lower' if side < 0 else 'higher'} through it is held."
                                  if ema_ref else "")
                               + (f" It closed {flipped} opposite position(s) first." if flipped else "")
                               + ("" if in_win_now else " Forced outside the trading hours; it has no 23:00 close "
                                                       "and runs until the EMA20 exit, the opposite signal, Stop, or the time exit."))
            except Exception as exc:                                        # noqa: BLE001
                _res("error", f"Could not place the test trade: {type(exc).__name__}")
    res = st.session_state.get("test_result")
    if res:                                                    # stays visible across the 5-second auto-refresh
        {"success": st.success, "warning": st.warning, "error": st.error}[res[0]](f"{res[2]} IST · {res[1]}")
    with st.expander("🔎 Why isn't it trading? (live diagnostics)"):
        rows, now_u = [], pd.Timestamp.now(tz="UTC")
        lr, iv_min = state.get("last_run_utc"), INTERVAL_MIN[S["interval"]]
        if lr:
            age_min = (now_u - pd.Timestamp(lr)).total_seconds() / 60
            fine = age_min <= iv_min * 2 + 2
            rows.append(("Background trader running", "✅" if fine else "❌",
                         f"last cycle {age_min:.0f} min ago" + ("" if fine else
                         " — too long: the Render service may be asleep or crashed. Check the Render logs and your pinger.")))
        else:
            rows.append(("Background trader running", "❌", "no cycle recorded yet"))
        if state.get("last_error"):
            rows.append(("Trader's last error", "❌", state["last_error"]))
        saved_cfg = read_key("settings")[0]
        rows.append(("Settings loaded from storage", "✅" if saved_cfg else "⚠️",
                     "saved settings found" if saved_cfg else "nothing saved yet: defaults are in use"))
        win_now = in_trade_window(now_u)
        rows.append(("Trading window right now", "✅" if win_now else "ℹ️",
                     f"{'inside' if win_now else 'outside'} Tue–Thu 19:00–23:00 IST · trading is {S['window_lane']}"))
        if data_ok and live_price and "ema20" in dfi and pd.notna(dfi["ema20"].iloc[-1]):
            ema_live = float(dfi["ema20"].iloc[-1])
            rows.append(("EMA20 trend exit", "ℹ️",
                         f"EMA20 {ema_live:,.2f} · price {live_price:,.2f} is "
                         f"{'above' if live_price > ema_live else 'below'} it · a SELL exits only when a candle CLOSES "
                         f"above the EMA20 and a BUY only on a close below it · the crossing that goes with the trade "
                         f"(a SELL falling through) is held · the EMA never blocks an entry"))
        rows.append(("Rules build running", "ℹ️", engine.RULES_BUILD +
                     " · a BUY can never follow a bearish-origin level"))
        rows.append(("Rules the TRADER uses", "ℹ️", f"{S['rules_mode']} · cooldown {S['cooldown_bars']} · provider {S['provider']} · "
                     f"lot ${S['lot_usd']:,.0f}"))
        rows.append(("Rules THIS PAGE uses", "✅" if rules_mode == S["rules_mode"] else "⚠️",
                     f"{rules_mode} · cooldown {cooldown_bars}" + ("" if rules_mode == S["rules_mode"] else
                     " — differs from the trader; press 'Apply these study settings' in the sidebar")))
        dd = events[events["signal_side"] != 0] if (data_ok and not events.empty) else pd.DataFrame()
        if dd.empty:
            rows.append(("Latest directional signal", "❌", "none in the loaded sample, so there is nothing to follow"))
        else:
            ls = dd.iloc[-1]
            ago_c = engine.signal_age(ls, closed["open_time"].iloc[-1], pd.Timedelta(minutes=iv_min))
            used_today = engine.auto_trades_on(state, pd.Timestamp.now(tz=IST).date())
            rows.append(("Window trades today", "✅" if used_today < engine.max_window_trades(S) else "❌ BLOCKS",
                         f"{used_today} of {engine.max_window_trades(S)} automatic window trades used today (IST) · "
                         f"Force/test trades are not counted and have no daily limit · no freshness rule: the newest "
                         f"in-window signal is traded however old it is ({ago_c} candle(s) now); a signal whose "
                         f"side is against the EMA20 is skipped every time"))
            rows.append(("Latest directional signal", "ℹ️",
                         f"{SIDE_LABEL.get(engine.side_from_origin(ls['origin_direction'])) or 'none'} · "
                         f"{ls['pattern'].replace(' (candidate)', '')} · "
                         f"{ls['event_time'].tz_convert(IST).strftime('%a %d %b %H:%M IST')} · {ago_c} candle(s) ago · "
                         f"origin {ls['origin_direction']} · level {ls['level_price']:,.2f} "
                         f"(created {pd.Timestamp(ls['level_time']).tz_convert(IST).strftime('%a %d %b %H:%M IST')}) · "
                         f"level invalidated: {bool(ls['level_invalidated'])}"))
            _nl = events.sort_values("level_time").iloc[-1]
            rows.append(("Newest level (study list top)", "ℹ️",
                         f"{_nl['level_price']:,.2f} · origin {_nl['origin_direction']} → "
                         f"{SIDE_LABEL.get(engine.side_from_origin(_nl['origin_direction'])) or 'none'} · created "
                         f"{pd.Timestamp(_nl['level_time']).tz_convert(IST).strftime('%a %d %b %H:%M IST')} · "
                         f"{_nl['pattern']}" +
                         (" — a level touch is not a signal until a strong close fires"
                          if not _nl["pattern"].startswith("Strong close") else "")))
            checks = [
                ("Origin filter", S["allowed_origins"] == "Both" or ls["origin_direction"] == S["allowed_origins"],
                 f"set to '{S['allowed_origins']}'"),
                ("'Skip events that invalidate their level'", not (S["skip_invalidated_events"] and bool(ls["level_invalidated"])),
                 f"{'ticked' if S['skip_invalidated_events'] else 'not ticked'}; this signal {'does' if ls['level_invalidated'] else 'does not'} end its level"),
                ("Trade side (from the level origin)", engine.side_from_origin(ls["origin_direction"]) != 0,
                 f"level origin {ls['origin_direction']} -> trade {SIDE_LABEL.get(engine.side_from_origin(ls['origin_direction'])) or 'none'}"
                 f" · the pattern ({ls['pattern'].replace(' (candidate)', '')}) only triggers it, it never sets the side"),
                ("Level is from today (IST)",
                 engine.ist_date(ls["event_time"]) == pd.Timestamp.now(tz=IST).date(),
                 f"signal day {engine.ist_date(ls['event_time'])} · today {pd.Timestamp.now(tz=IST).date()} · "
                 f"levels from earlier days are never traded"),
                ("Data currency", engine.usd_ok(S, quote_currency), f"data is {quote_currency}"),
            ]
            for name, passed, detail in checks:
                rows.append((f"Rule: {name}", "✅" if passed else "❌ BLOCKS", detail))
        recent = state.get("signals", [])[-4:][::-1]
        for x in recent:
            rows.append(("Trader's recent decision", "ℹ️", f"{x['side']} {x['pattern'].replace(' (candidate)', '')} → {x['action']}"))
        st.dataframe(pd.DataFrame(rows, columns=["Check", "Result", "Detail"]), width="stretch", hide_index=True)
        st.caption("Read-only: this panel never places trades. A ❌ BLOCKS line is the reason the trader skips the "
               "newest signal — the Force button is blocked by the same lines, except the trading window, which your "
               f"click overrides, and the {engine.max_window_trades(S)}-per-day cap, which does not apply to it.")
    st.caption("A forced trade shows the trigger 'manual'; a worker trade shows 'auto' or 'startup'. "
               "Force trade places one trade immediately at the live price. Your click is the consent for one rule "
               "only — the Tue–Thu 19:00–23:00 IST trading hours — so it trades outside the window too, and that "
               "trade has no 23:00 close. It is never counted in the automatic window cap, so the test lane is "
               "unlimited. Everything else still applies: trading must be RUNNING, the side comes from the level's "
               "origin direction, your settings filters are checked, and only one position is held at a time. "
               "Force works from TODAY's levels only: it takes today's newest level whose origin matches the EMA20 "
               "(a BUY above it, a SELL below it) and enters at the live price, so yesterday's signal is never "
               "carried into today. When nothing from today matches, it places nothing and says why.")
    mode = st.radio("Show", ["All", "WINDOW"], horizontal=True)
    sm = engine.account_summary(state, mode, price_for_pnl)
    a1, a2, a3 = st.columns(3)
    a1.metric("Equity (INR)", inr(sm["equity"]), inr_signed(sm["equity"] - sm["initial"]))
    a2.metric("Balance (realized, INR)", inr(sm["balance"]))
    a3.metric("Unrealized P&L (INR)", inr_signed(sm["unrealized"]))
    b1, b2, b3 = st.columns(3)
    b1.metric("Open / closed trades", f"{sm['open']} / {sm['closed']}")
    b2.metric("Win rate", "-" if sm["win_rate"] is None else f"{sm['win_rate']:.0f}%")
    b3.metric("Realized P&L (INR)", inr_signed(sm["realized"]))
    st.caption(f"Money is shown in INR at ₹{INR:,.2f} per $1 ({RATE_LABEL}). "
               f"USD: equity ${sm['equity']:,.2f} · balance ${sm['balance']:,.2f} · "
               f"unrealized ${sm['unrealized']:+,.2f} · realized ${sm['realized']:+,.2f}. Prices stay in USD.")
    curve = sm["curve"]
    cfig = go.Figure()
    if len(curve):
        xs = [ist_naive(curve["time"]).iloc[0] - pd.Timedelta(minutes=5)] + list(ist_naive(curve["time"]))
        ys = [sm["initial"] * INR] + [b * INR for b in curve["balance"]]
        cfig.add_trace(go.Scatter(x=xs, y=ys, mode="lines+markers", line_shape="hv", name="Balance (INR)"))
    cfig.update_layout(height=260 if compact else 340, yaxis_title="Balance (INR)", xaxis_title="Time (IST)",
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
            opened["unrealized_inr"] = opened["unrealized_usd"] * INR
        opened["entry_ist"] = ist_str(opened["entry_time"])
        opened["closes_at_ist"] = [ist_str(pd.Series([pd.Timestamp(w)]))[0] if isinstance(w, str) and w else "flip or Stop"
                                   for w in opened.get("window_end", [None] * len(opened))]
        show_df(opened[["id", "mode", "trigger", "side", "pattern", "origin_direction", "level_price",
                        "entry_ist", "entry_price", "closes_at_ist", "unrealized_inr", "unrealized_usd"]].round(4),
                ["id", "trigger", "side", "origin_direction", "level_price", "entry_price", "unrealized_inr"],
                compact)

# ================================================================= ACTIVITY TAB
with tab_act:
    st.subheader("Trades")
    td = engine.trades_df(state, "All")
    if td.empty:
        st.caption("No trades yet. Trades appear here once the worker sees a signal inside the trading window.")
    else:
        td = td.sort_values("entry_time", ascending=False).copy()
        td["entry_ist"] = ist_str(td["entry_time"])
        td["exit_ist"] = ist_str(td["exit_time"]).fillna("") if td["exit_time"].notna().any() else ""
        td["pnl_inr"] = td["pnl_usd"] * INR if "pnl_usd" in td.columns else float("nan")
        cols = ["id", "mode", "trigger", "status", "side", "pattern", "origin_direction", "level_invalidated", "entry_ist",
                "entry_price", "lots", "qty", "exit_ist", "exit_price", "exit_reason", "bars_held", "pnl_inr", "pnl_usd",
                "future_high_excursion_pct", "source"]
        out = td[[c for c in cols if c in td.columns]].round(4)
        show_df(out, ["id", "mode", "side", "entry_ist", "exit_reason", "pnl_inr", "pnl_usd",
                      "future_high_excursion_pct"], compact)
        st.download_button("Download trades CSV (IST)", data=out.to_csv(index=False).encode("utf-8"),
                           file_name="paper_trades_ist.csv", mime="text/csv")
    st.subheader("Recent signals seen by the trader")
    sg = pd.DataFrame(state.get("signals", []))
    if sg.empty:
        st.caption("No signals recorded by the worker yet.")
    else:
        sg["event_time"] = pd.to_datetime(sg["event_time"], utc=True, format="ISO8601")
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
    if origin == "env":
        host = (url_now or "").replace("https://", "").split(".")[0]
        st.success(f"Connected via Render environment variables ✓ (database {host[:4]}…{host[-3:]}). "
                   "Nothing to enter here.")
        if st.button("Test connection"):
            try:
                _u, _t, _ = storemod.get_creds()
                storemod.test_connection(_u, _t)
                st.success("Connection works: a test write and read succeeded.")
            except Exception as exc:                            # noqa: BLE001
                st.error(f"Connection failed: {type(exc).__name__}. Check the variables in Render.")
    else:
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
                except Exception as exc:                        # noqa: BLE001
                    st.error(f"Connection failed: {type(exc).__name__}. Check the URL and token.")
    if origin == "saved" and st.button("Remove saved connection"):
        storemod.clear_creds()
        read_key.clear()
        st.rerun()
    st.caption("Tokens are never shown or logged. No exchange or trading credentials are ever needed — market data is public.")

    st.subheader("Demo trader settings")
    with st.form("trade_settings"):
        c1, c2 = st.columns(2)
        lot_usd = c1.number_input("Lot size (USD notional) — other providers", 1.0, 100000.0,
                                  float(S["lot_usd"]), 1.0)
        size_lots = c2.number_input("Quantity (lots) — Delta BTCUSD, 1 lot = 0.001 BTC", 1, 100000,
                                    int(S.get("size_lots", 1)), 1)
        st.caption("Delta data trades the **lot quantity** above, the same way the Delta app takes a quantity: "
                   "1 lot = 0.001 BTC (about $59 at $59,000), 10 lots = 0.01 BTC. The USD figure is used only by "
                   "the other providers, and the fee follows whichever size is actually traded.")
        initial_balance_inr = c2.number_input("Initial balance (₹, applies on account reset)", 1000.0, 1_000_000_000.0,
                                              round(float(S["initial_balance"]) * INR, 0), 1000.0)
        initial_balance = float(initial_balance_inr) / INR
        fee_pct = c1.number_input("Fee per side (%) — simulated", 0.0, 1.0, float(S["fee_pct"]), 0.01)
        usd_inr_set = c2.number_input("USD → INR rate (0 = use the auto-captured rate)", 0.0, 1000.0,
                                      float(S.get("usd_inr", 0.0)), 0.01)
        st.caption(f"The balance is stored as **${initial_balance:,.2f}** at ₹{INR:,.2f}/$ and shown in rupees "
                   f"everywhere. A saved rate above is used instead of the live one, so the rupee figures stay put.")
        max_hold_bars = st.number_input("Max holding candles (0 = hold until the 11pm close or the opposite signal)",
                                        0, 500, int(S["max_hold_bars"]), 1)
        max_window_trades = st.number_input("Max automatic window trades per IST day (Force/test has no limit)",
                                            1, 50, int(engine.max_window_trades(S)), 1)
        carry_in = st.checkbox("When the window opens, also use that day's newest signal from earlier",
                               value=bool(S.get("carry_in_signal", True)))
        st.caption("One position only: it flips on the opposite signal and closes at 11:00pm IST either way. "
                   "A qualifying pattern lands inside the 4-hour window on barely one evening in three, so the "
                   "carry-in uses the newest signal from earlier the same day at the window's first cycle "
                   "(once a day, at the current price, and it still closes at 11:00pm). "
                   "Start / Pause / Stop are on the Live P&L tab.")
        origins = ["Both", "Bullish", "Bearish"]
        allowed_origins = st.selectbox("Only trade levels with origin direction", origins,
                                       index=origins.index(S["allowed_origins"]))
        skip_inv = st.checkbox("Skip events that invalidate their level (breakout events)",
                               value=bool(S["skip_invalidated_events"]))
        allow_usd = st.checkbox("Allow trading on USD data (Delta perp, Coinbase, Kraken)",
                                value=bool(S["allow_usd_fallback"]))
        sleep_outside = st.checkbox("Let Render sleep outside the trade window (saves free hours; needs an outside "
                                    "pinger scheduled for Tue–Thu evenings)", value=bool(S["sleep_outside_window"]))
        use_sidebar = st.checkbox("Also save the current sidebar study settings for the trader", value=True)
        saved = st.form_submit_button("Save settings")
    if saved:
        try:
            new = engine.merge_settings(STORE.read("settings"))              # fresh copy, not the cached one
            new.update(lot_usd=lot_usd, size_lots=int(size_lots), initial_balance=initial_balance,
                       usd_inr=float(usd_inr_set),
                       fee_pct=fee_pct, max_hold_bars=int(max_hold_bars),
                       max_window_trades=int(max_window_trades), carry_in_signal=bool(carry_in),
                       allowed_origins=allowed_origins,
                       skip_invalidated_events=skip_inv, allow_usd_fallback=allow_usd,
                       sleep_outside_window=sleep_outside)
            if use_sidebar:
                new.update(study_values)
            STORE.write("settings", new)
            read_key.clear()
            st.success("Settings saved.")
        except Exception as exc:                                # noqa: BLE001
            st.error(f"Could not save settings: {type(exc).__name__}")
    fc1, fc2 = st.columns([4, 1])
    fc1.caption(f"Auto USD → INR capturer: **₹{FX_RATE:,.2f} per $1**, captured {fx_ist(FX_WHEN)} "
                f"from {FX_SOURCE}. It refreshes itself every hour and is stored with the account. "
                + ("Your saved rate above overrides it." if RATE_SET > 0
                   else "This is the rate used in every rupee figure."))
    if fc2.button("Refresh rate now"):
        rate, source, when = providers.usd_inr_quote()
        try:
            STORE.write(FX_KEY, {"rate": rate, "captured_utc": when, "source": source})
        except Exception as exc:                            # noqa: BLE001
            st.warning(f"Could not store the rate: {type(exc).__name__}")
        auto_usd_inr.clear()
        st.rerun()
    st.caption(f"Trader currently uses: {S['interval']} candles · impulse ≥ {S['impulse_atr']}×ATR · "
               f"provider {S['provider']} · one position, flips on the opposite signal · "
               f"max hold {S['max_hold_bars'] or 'none'} · observe {S['forward_bars']} bars · "
               f"{S.get('size_lots', 1)} lot(s) of 0.001 BTC on Delta · ${S['lot_usd']:,.0f} notional elsewhere · "
               f"₹{INR:,.2f}/$")

    with st.expander("Reset demo account"):
        st.caption(f"Restarts at any starting balance you type below — this only applies to this reset "
                   f"unless you tick the box to save it. The normal saved balance is "
                   f"**{inr(S['initial_balance'])}** (${S['initial_balance']:,.2f} at ₹{INR:,.2f}/$).")
        rc1, rc2 = st.columns(2)
        reset_inr = rc1.number_input("Starting balance for this reset (₹)", 1.0, 1_000_000_000.0,
                                     round(float(S["initial_balance"]) * INR, 0), 1000.0,
                                     key="reset_balance_inr")
        reset_balance = float(reset_inr) / INR
        rc2.caption(f"Stored as **${reset_balance:,.2f}** at ₹{INR:,.2f}/$ "
                    f"({RATE_LABEL}).")
        keep_reset = rc2.checkbox("Also save this as the normal starting balance", value=False)
        sure = st.checkbox("I understand this deletes all trades and restarts from the balance above")
        if st.button("Reset account", disabled=not sure):
            try:
                fresh = engine.merge_settings(STORE.read("settings"))
                if keep_reset:
                    fresh["initial_balance"] = reset_balance
                    STORE.write("settings", fresh)
                STORE.write("state", engine.new_state({**fresh, "initial_balance": reset_balance}))
                read_key.clear()
                st.success(f"Account reset to {inr(reset_balance)} (${reset_balance:,.2f}).")
            except Exception as exc:                            # noqa: BLE001
                st.error(f"Reset failed: {type(exc).__name__}")
    st.caption("App login password: set APP_PASSWORD as an environment variable on your host (it is intentionally not a page field).")
