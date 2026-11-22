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
    it was bearish. The trade side comes from the level's origin_direction, exactly as the
    Historical event study table shows it: Bullish-origin level -> BUY, Bearish-origin level
    -> SELL. The event pattern only decides WHEN that trade happens; it never sets the side,
    so a bearish-origin level can never produce a BUY. This is absolute: there is no switch
    for it and the Force button does not lift it. If a level has no Bullish/Bearish origin at
    all, the trade is skipped - the pattern's own side is never used as a fallback, so a signal
    can never sneak in as a BUY on a bearish setup. The event study shows the side the origin
    gives each row in its trade_side_by_origin column.
  * No freshness rule and no repeat-direction rule. However old the newest signal is, if its
    own candle closed inside a trading window the trader acts on it; a restart cannot trade the
    same signal twice. A same-direction signal after the previous trade closed is a normal new
    trade. While a position of that direction is still open it is simply kept (one position at
    a time).
  * Two automatic window trades per IST day. The worker's window entries are capped at two per
    IST date, flips included. The Force button is the test lane: it is never counted and has no
    daily limit.
  * Flip on the opposite signal. An opposite signal closes the open position at that signal's
    price, win or lose (exit reason REVERSED), and immediately opens the other side. The Force
    button flips the same way, so you never have to close the old trade by hand first. A trade
    carried over from an older build (it may still say mode TEST, the removed second lane) is
    adopted into the one lane, so it is flipped and managed by the EMA20 like every other
    position instead of being ignored.
  * Bad-direction cleanup. If an open position's stored level origin points against its own
    side (only possible from an older build or a force entry made before the rule was
    absolute), the worker closes it at the latest price with exit reason ORIGIN_FIX.
  * Sizing and exits. Entry at the signal candle's close (live price for a startup catch-up and
    the Force button). Lot $20 notional, initial balance $5,000. There is no stop-loss and no
    take-profit: a position is held until the EMA20 exit, the opposite signal, the 23:00 IST
    close, a manual Stop, or the optional time exit (Max holding candles, default none).
    Fees default 0 (simulated setting).
  * EMA20 protection (exit only). A 20-candle EMA on the trader's own interval. At entry the
    trader records whether the price is above or below the EMA20 - that never blocks the trade,
    the signal alone decides the entry. The trade then exits (reason EMA20) on the first candle
    that CLOSES on the other side of the EMA20 than the entry side:
      - SELL entered while price was above the EMA: exits when a candle closes below it.
      - SELL entered while price was below the EMA (EMA above the price): exits when a candle
        closes above it.
      - BUY is the mirror: entered above the EMA it exits on a close below; entered below, on a
        close above.
    A touch alone is not enough - the candle has to close across the EMA.
  * Start now = trade now. On startup, or when Start is pressed, the trader takes the newest
    signal if its own candle closed inside the trading window, however old it is, and it passes
    the rules (the two-per-day cap included). It never replays older history, and a restart
    cannot duplicate a trade. If the window is already shut at that moment the trade has no
    23:00 close and runs on the EMA20 exit.
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

Force trade now (the unlimited test lane)
  A manual test entry, so YOU are the consent for the one thing a human click should decide:
  the trading hours. It places ONE trade instantly at the live price following the latest
  signal, inside or outside Tue-Thu 19:00-23:00 IST, and a trade forced outside the hours has
  no 23:00 close (it runs until the EMA20 exit, the opposite signal, Stop, or the time exit).
  Forced trades are never counted in the two-per-day window cap, so the test lane is unlimited.
  Everything else still applies: Window trading must be RUNNING, the side comes from the
  level's origin direction, your level-invalidated / origin-filter / data-currency settings are
  checked, and only one position is held at a time (an opposite one is flipped first). Anything
  blocked shows the reason and places nothing.

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
