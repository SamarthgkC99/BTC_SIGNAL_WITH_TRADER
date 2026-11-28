BTC Momentum-Level Study + Demo (paper) Trader
==============================================
Simulation only. No real orders, no exchange accounts, no trading credentials.

Files
  main.py       Render entry point: starts the dashboard, the background trader and a keep-alive pinger
  app.py        Streamlit page: Study | Live P&L | Trading activity | Settings
  signals.py    Signal engine (your original logic + the fixes)
  engine.py     Paper-trading rules, P&L, summaries
  providers.py  Public candle APIs: Binance -> (Delta India BTCUSD perp, Coinbase, Kraken = USD) -> Bybit -> OKX -> KuCoin
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
  * Only a defined pattern is a signal. A strong close through the level or a rejection off it
    makes the event a signal; a bare level touch with no pattern is shown on the Study tab as
    "not a signal (no pattern)", its trade column is empty, and the trader never acts on it -
    not in the window, not on a restart, and not through the Force button.
  * Today's levels only. The trader enters only on a level from the current IST day, in the
    window and in the Force button alike. A restart, a catch-up or a carry-in can no longer
    carry yesterday's signal into today: rows from earlier days are history. A restart still
    cannot trade the same signal twice, a same-direction signal after the previous trade closed
    is a normal new trade, and while a position of that direction is still open it is simply
    kept (one position at a time).
  * Today's newest signal, both sides. The Force button takes TODAY's newest SIGNAL, whatever
    side its origin gives - a Bullish-origin level places a BUY and a Bearish-origin level places
    a SELL, so a BUY is never refused just because the price sits below the EMA20. The EMA20 is
    never consulted for an entry; it only closes trades (a position exits when a candle CLOSES
    through it). A bare level touch is not a signal and cannot be forced. A level from an earlier
    day is never carried into today.
  * Automatic window trades per IST day: a Settings field (1-50, default 4). The worker's
    window entries are capped at that many per IST date, flips included. The Force button is the
    test lane: it is never counted and has no daily limit.
  * Carry-in when the window opens (Settings, on by default). The window is only four hours,
    and a qualifying pattern lands inside it on barely one evening in three, so at the window's
    first cycle the trader also takes that IST day's newest signal even if its candle closed
    before 19:00 (e.g. 14:15). It enters at the current price, keeps the 23:00 IST close, counts
        toward the day's cap, and happens at most once a day - only when no automatic trade has been
    made that day yet. Untick it to trade only signals whose own candle closed inside the
    window.
  * Force trading hours (Live P&L tab). Start a 30 min / 1 hour / 2 hour / 4 hour session and the
    clock stops mattering: for as long as it runs the trader takes TODAY's signals automatically,
    exactly as it does inside the window, with NO limit on how many. On Start it also takes
    today's newest signal straight away at the live price, so the session begins with a trade
    instead of waiting for the next one. Only the hours change: today's levels, a defined
    pattern, the origin side, one position at a time, your settings filters and the EMA20 exit
    all work exactly as usual, and Window trading must be RUNNING (Stop also ends the session).
    When the session ends, any position it opened is closed at the market (exit reason
    FORCE_END); those trades are tagged trigger 'force-hours', never count toward the window's
    daily cap, and keep the app awake so the trader can manage them. Inside the real Tue-Thu
    window the normal rules win (daily cap + the 23:00 close). The end time is saved, so a
    restart can neither extend nor cancel a session, and a forgotten session switches itself off
    when its end time passes. A checkbox in Settings ('Allow the Force trading hours buttons')
    can hide the buttons.
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
    the Force button). Lot $20 notional, initial balance $5,000. On Delta Exchange India data
    (BTCUSD perpetual) the trade is sized by a lot quantity instead, the same as the exchange's
    own quantity box: 1 lot = 0.001 BTC (about $59 at $59,000), 10 lots = 0.01 BTC. The simulated
    fee and P&L use that real notional. Other providers keep the USD-notional size. There is no stop-loss and no
    take-profit: a position is held until the EMA20 exit, the opposite signal, the 23:00 IST
    close, a manual Stop, or the optional time exit (Max holding candles, default none).
    Fees default 0 (simulated setting).
  * EMA20 trend exit (exit only). A 20-candle EMA on the trader's own interval. A SELL is held
    while the price stays below the EMA20 and exits (reason EMA20) the first time a candle
    CLOSES above it. A BUY is the mirror: held above the EMA20, exits on a candle close below it.
    The crossing that goes with the trade - a SELL falling through the EMA, a BUY rising through
    it - is the move the trader wants, so it never closes the trade; the trade keeps running as
    the price carries on lower (for a SELL) or higher (for a BUY). What closes it is the price
    turning back against the position and closing on the other side of the EMA20.
    Where the EMA sits at entry never blocks a trade: the signal alone decides the entry. A trade
    entered on the wrong side of the EMA (a SELL signalled above it, a BUY below it) is managed
    once a candle has first closed on its own side, so the entry itself cannot close it.
    A touch alone is never enough - the candle has to close across the EMA.
  * Start now = trade now. On startup, or when Start is pressed, the trader takes the newest
    signal if its own candle closed inside the trading window, however old it is, and it passes
    the rules (the day's cap included). It never replays older history, and a restart
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
  Below it, 'Force trading hours' turns the click into a session: Start 30 min / 1 hour / 2 hours /
  4 hours (buttons to extend by 30 min or an hour, and 'End force hours now'), and for as long as
  the session runs the trader takes today's signals by itself, with no limit, then closes any
  position still open at the market when the session ends. That is the way to let it trade
  outside Tue-Thu 19:00-23:00 IST without pressing Force for every trade.
  A manual test entry, so YOU are the consent for the one thing a human click should decide:
  the trading hours. It places ONE trade instantly at the live price following TODAY's newest
  SIGNAL (a strong close or a rejection), whichever side its origin gives - a Bullish-origin
  level is a BUY, a Bearish-origin one a SELL. The EMA20 is not consulted when entering; it only
  closes the trade. A bare level touch is not a signal, so it is never forced. A level from an
  earlier day is never carried into today: yesterday's bullish level cannot put on today's BUY.
  It works inside or outside Tue-Thu 19:00-23:00 IST, and a trade forced outside the hours has
  no 23:00 close (it runs until the EMA20 exit, the opposite signal, Stop, or the time exit).
  Forced trades are never counted in the automatic window cap, so the test lane is unlimited.
  Everything else still applies: Window trading must be RUNNING, the side comes from the
  level's origin direction, your level-invalidated / origin-filter / data-currency settings are
  checked, and only one position is held at a time (an opposite one is flipped first). Anything
  blocked shows the reason and places nothing.

Price provider (sidebar)
  Auto chain, or choose Binance / Delta Exchange India (BTCUSD perp) / Bybit / OKX / KuCoin /
  Coinbase (USD) / Kraken (USD).
  A named provider is tried first; tick "fall back" to let the others take over if it fails.
  'Apply these study settings + provider to the trader' saves them for the background worker.
  Delta Exchange India (BTCUSD perpetual, public candles, no API key or account): USD-quoted and
  INR-margined, up to 2000 candles per request. Fees are 0.05% taker and 0.02% maker per side on
  the notional, plus 18% GST (0.059% / 0.0236% per side) - enter that in 'Fee per side (%)' in
  Settings if you want Delta's costs in the paper P&L.

Event rules (sidebar)
  Improved (default: stricter invalidation, one event per candle, cooldown 6) or Legacy (first version, cooldown 3).

INR display + automatic USD -> INR capturer
  Equity, balance, unrealized and realized P&L, the equity curve, the open-position P&L and the
  trades table (including its CSV) are shown in rupees.
  The USD -> INR rate is captured automatically from a public no-key source (open.er-api.com,
  then frankfurter.dev, then fxratesapi.com; a built-in 88 is used only if all three fail).
  The background worker captures it once an hour and stores it with the account data, and the
  page re-captures it if the stored rate is more than an hour old. Settings shows the rate and
  when it was captured, with a 'Refresh rate now' button.
  Pin a rate in Settings ('USD -> INR rate', 0 = auto-captured) so the rupees stop moving.
  BTC prices, entry/exit prices and fees stay in USD.

Reset the demo account to any balance
  Settings -> 'Reset demo account': type any starting balance in rupees for that reset. The
  balance applies to this reset only; tick 'Also save this as the normal starting balance' to
  make it the saved default too. Resetting deletes all trades and restarts from that balance
  (stored as USD at the rate shown).

Environment variables (set on Render, never in code)
  APP_PASSWORD               login password for the page
  UPSTASH_REDIS_REST_URL     from the Upstash console (REST API section)
  UPSTASH_REDIS_REST_TOKEN   from the Upstash console

Run locally (Windows 11)
  python -m venv .venv
  .venv\Scripts\activate
  python -m pip install -r requirements.txt
  python main.py            (trader + dashboard)   or   streamlit run app.py   (dashboard only)
