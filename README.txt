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
  BUY on bullish patterns, SELL (short) on bearish patterns; plain touches skipped.
  Window: Tue/Wed/Thu 19:00-23:00 IST, judged by the signal candle's close time.
  Test mode (default ON): signals outside the window are also traded and tagged TEST.
  Lot $20 notional, initial balance $5,000, TP 0.5% / SL 0.5%, time exit after the
  forward horizon (default 6 candles). If TP and SL are both inside one candle, SL is assumed.
  Entry = signal candle close. Fees default 0 (simulated setting).
  future_high_excursion_pct is measured AFTER the signal, so it is analysis only,
  never an entry condition.

Test controls (Live P&L tab)
  Start/Stop test mode: bypasses ONLY the Tue-Thu 7-11pm IST window (trades tagged TEST).
  Test trade now: places ONE trade instantly in the direction of the latest signal at the live price,
  when the trade window is open or test mode has been started. It follows every trader rule (lot,
  TP/SL, fee, origin filter, skip-invalidated filter, max open trades, one open trade per level,
  USD-data rule). Outside the window with test mode stopped, it asks you to start test mode first.

Price provider (sidebar)
  Auto chain, or choose Binance / Bybit / OKX / KuCoin / Coinbase (USD) / Kraken (USD).
  A named provider is tried first; tick "fall back" to let the others take over if it fails.
  'Use this provider for the trader too' saves the choice for the background worker.

Environment variables (set on Render, never in code)
  APP_PASSWORD               login password for the page
  UPSTASH_REDIS_REST_URL     from the Upstash console (REST API section)
  UPSTASH_REDIS_REST_TOKEN   from the Upstash console

Run locally (Windows 11)
  python -m venv .venv
  .venv\Scripts\activate
  python -m pip install -r requirements.txt
  python main.py            (trader + dashboard)   or   streamlit run app.py   (dashboard only)
