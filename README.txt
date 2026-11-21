BTC Momentum-Level Study + Demo (paper) Trader
==============================================
Simulation only. No real orders, no exchange accounts, no trading credentials.

Files
  main.py       Render entry point: starts the dashboard, the background trader and a keep-alive pinger
  app.py        Streamlit page: Study | Live P&L | Trading activity | Settings
  signals.py    Signal engine (your original logic + the fixes)
  engine.py     Paper-trading rules, P&L, summaries
  providers.py  Public candle APIs: Binance -> Bybit -> OKX -> KuCoin -> (Coinbase, Kraken = USD)
  store.py      Upstash Redis (REST) or local JSON-file fallback
  render.yaml   Render blueprint (free plan, Singapore region)

Trading rules (all editable in Settings)
  * Trading hours only. The trader acts on the historical study's window: Tuesday, Wednesday
    and Thursday, 19:00-23:00 IST, judged by the signal candle's close time. Signals at any
    other time are logged as skipped and never traded. Every still-open position is closed at
    23:00 IST, profit or loss (exit reason WINDOW_END).
  * Origin direction. BUY only when the level's originating impulse was bullish, SELL only when
    it was bearish. A signal pointing against its level's origin is skipped ("against the
    level's origin direction"). This rule is absolute: there is no switch for it and the Force
    button does not lift it, so a BUY is never taken on a bearish-origin level. Under Improved
    rules every "strong close" breakout is counter-origin, so breakouts are always skipped and
    only rejections at the level can trade.
  * One position, skip repeats. The trader holds a single position. A signal in the same
    direction as the last trade it took is skipped, whether that trade is still open or was
    closed by the opposite signal or the 23:00 IST close. The set of skip reasons is shown in
    "Why isn't it trading?".
  * Flip on the opposite signal. An opposite signal closes the open position at that signal's
    price, win or lose (exit reason REVERSED), and immediately opens the other side.
  * Bad-direction cleanup. If an open position's stored level origin points against its own
    side (only possible from an older build or a force entry made before the rule was
    absolute), the worker closes it at the latest price with exit reason ORIGIN_FIX.
  * Sizing and exits. Entry at the signal candle's close (live price for a startup catch-up and
    the Force button). Lot $20 notional, initial balance $5,000. There is no stop-loss and no
    take-profit: a position is held until the opposite signal, the 23:00 IST close, a manual
    Stop, or the optional time exit (Max holding candles, default none).
    Fees default 0 (simulated setting).
  * Start now = trade now. On startup, or when Start is pressed inside the window, the trader
    takes the newest signal if it is at most 2 candles old and passes the rules. It never
    replays older history, and a restart cannot duplicate a trade.
  * future_high_excursion_pct is measured AFTER the signal, so it is analysis only,
    never an entry condition.

Trader controls (Live P&L tab)
  One control: Window trading = signals inside Tue-Thu 19:00-23:00 IST.
  Start = new trades allowed (and the newest fresh signal is taken now).
  Pause = no new trades, an open trade keeps running, the app stays awake.
  Stop  = no new trades AND the open trade is closed at the live price. Never refused.
  When trading is Stopped with no open trade, the app stops pinging itself so Render can sleep
  (saves free instance hours). An outside pinger (cron-job.org) will keep waking it - pause
  that job too.
  Optional (Settings): 'Let Render sleep outside the trade window' + a cron-job.org job that
  pings only Tue-Thu ~18:30-23:30 IST uses roughly 65 instance hours a month instead of ~744.

Force trade now
  Places ONE trade instantly at the live price following the latest signal. It bypasses the
  window and the repeat-direction rules (that is what forcing means) and flips an opposite
  position first, but still uses the lot, fee, data-currency and level-origin rules. If the
  latest signal points against its level's origin it is refused, not placed. It is the only
  way to trade outside the window.

Price provider (sidebar)
  Auto chain, or choose Binance / Bybit / OKX / KuCoin / Coinbase (USD) / Kraken (USD).
  A named provider is tried first; tick "fall back" to let the others take over if it fails.
  'Apply these study settings + provider to the trader' saves them for the background worker.

Event rules (sidebar)
  Improved (default: stricter invalidation, one event per candle, cooldown 6) or Legacy (first version, cooldown 3).

Environment variables (set on Render, never in code)
  APP_PASSWORD               login password for the page
  UPSTASH_REDIS_REST_URL     from the Upstash console (REST API section)
  UPSTASH_REDIS_REST_TOKEN   from the Upstash console

Run locally (Windows 11)
  python -m venv .venv
  .venv\Scripts\activate
  python -m pip install -r requirements.txt
  python main.py            (trader + dashboard)   or   streamlit run app.py   (dashboard only)
