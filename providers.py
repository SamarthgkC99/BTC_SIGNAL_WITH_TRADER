"""Public BTC candle providers (no API keys). USDT markets first, USD markets last.

Every provider returns the same columns: open_time (UTC), open, high, low, close, volume.
The last row may be a still-forming candle; callers use signals.closed_only().
"""
import time

import pandas as pd
import requests

COLS = ["open_time", "open", "high", "low", "close", "volume"]
MINUTES = {"5m": 5, "15m": 15, "1h": 60}
HEADERS = {"User-Agent": "BTC-Momentum-Level-Study/2.0", "Accept": "application/json"}
BINANCE_BASES = [
    "https://data-api.binance.vision", "https://api.binance.com", "https://api-gcp.binance.com",
    "https://api1.binance.com", "https://api2.binance.com", "https://api3.binance.com",
    "https://api4.binance.com",
]
TIMEOUT = 8


def _finish(df: pd.DataFrame) -> pd.DataFrame:
    df = df[COLS].copy()
    for col in COLS[1:]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=COLS)
    return df.sort_values("open_time").drop_duplicates("open_time").reset_index(drop=True)


def _ms(series):
    return pd.to_datetime(pd.to_numeric(series), unit="ms", utc=True)


def _s(series):
    return pd.to_datetime(pd.to_numeric(series), unit="s", utc=True)


def _get(url, params):
    r = requests.get(url, params=params, timeout=TIMEOUT, headers=HEADERS)
    r.raise_for_status()
    return r.json()


def _binance(base, interval, limit):
    raw = _get(f"{base}/api/v3/klines", {"symbol": "BTCUSDT", "interval": interval, "limit": limit})
    df = pd.DataFrame(raw).iloc[:, :6]
    df.columns = COLS
    df["open_time"] = _ms(df["open_time"])
    return _finish(df)


def _bybit(interval, limit):
    iv = {"5m": "5", "15m": "15", "1h": "60"}[interval]
    data = _get("https://api.bybit.com/v5/market/kline",
                {"category": "spot", "symbol": "BTCUSDT", "interval": iv, "limit": min(limit, 1000)})
    df = pd.DataFrame(data["result"]["list"]).iloc[:, :6]       # newest first: ts,o,h,l,c,vol
    df.columns = COLS
    df["open_time"] = _ms(df["open_time"])
    return _finish(df)


def _okx(interval, limit):
    bar = {"5m": "5m", "15m": "15m", "1h": "1H"}[interval]
    data = _get("https://www.okx.com/api/v5/market/candles",
                {"instId": "BTC-USDT", "bar": bar, "limit": min(limit, 300)})
    df = pd.DataFrame(data["data"]).iloc[:, :6]                  # newest first: ts,o,h,l,c,vol
    df.columns = COLS
    df["open_time"] = _ms(df["open_time"])
    return _finish(df)


def _kucoin(interval, limit):
    typ = {"5m": "5min", "15m": "15min", "1h": "1hour"}[interval]
    end = int(time.time())
    start = end - limit * MINUTES[interval] * 60
    data = _get("https://api.kucoin.com/api/v1/market/candles",
                {"type": typ, "symbol": "BTC-USDT", "startAt": start, "endAt": end})
    df = pd.DataFrame(data["data"]).iloc[:, :6]                  # time,open,close,high,low,vol
    df.columns = ["open_time", "open", "close", "high", "low", "volume"]
    df["open_time"] = _s(df["open_time"])
    return _finish(df)


def _coinbase(interval, limit):
    sec = MINUTES[interval] * 60
    raw = _get("https://api.exchange.coinbase.com/products/BTC-USD/candles", {"granularity": sec})
    df = pd.DataFrame(raw, columns=["open_time", "low", "high", "open", "close", "volume"])
    df["open_time"] = _s(df["open_time"])
    return _finish(df)


def _kraken(interval, limit):
    payload = _get("https://api.kraken.com/0/public/OHLC", {"pair": "XBTUSD", "interval": MINUTES[interval]})
    if payload.get("error"):
        raise ValueError("; ".join(payload["error"]))
    res = payload["result"]
    key = next(k for k in res if k != "last")
    df = pd.DataFrame(res[key]).iloc[:, [0, 1, 2, 3, 4, 6]]
    df.columns = COLS
    df["open_time"] = _s(df["open_time"])
    return _finish(df)


PROVIDER_CHOICES = ["Auto (fallback chain)", "Binance", "Bybit", "OKX", "KuCoin", "Coinbase (USD)", "Kraken (USD)"]
USD_PROVIDERS = {"Coinbase (USD)", "Kraken (USD)"}


def _groups(interval, limit):
    return {
        "Binance": [(f"Binance Spot ({b.split('//')[1]})", "USDT", lambda b=b: _binance(b, interval, limit))
                    for b in BINANCE_BASES],
        "Bybit": [("Bybit Spot", "USDT", lambda: _bybit(interval, limit))],
        "OKX": [("OKX Spot", "USDT", lambda: _okx(interval, limit))],
        "KuCoin": [("KuCoin Spot", "USDT", lambda: _kucoin(interval, limit))],
        "Coinbase (USD)": [("Coinbase Exchange", "USD", lambda: _coinbase(interval, limit))],
        "Kraken (USD)": [("Kraken Spot", "USD", lambda: _kraken(interval, limit))],
    }


def fetch_candles(interval: str, limit: int, allow_usd: bool = True,
                  provider: str = "Auto (fallback chain)", fallback: bool = True):
    """Return (df, source_label, quote_currency, errors). Raises RuntimeError if all fail.

    provider: "Auto (fallback chain)" or one name from PROVIDER_CHOICES. A named provider is
    tried first; the rest follow only if `fallback` is True. USD providers are used as a
    fallback only when `allow_usd` is True, but a USD provider chosen by name is always tried.
    """
    groups = _groups(interval, limit)
    if provider in groups:
        chain = [provider] + ([g for g in groups if g != provider] if fallback else [])
    else:
        chain = list(groups)
    attempts = []
    for name in chain:
        if name in USD_PROVIDERS and not allow_usd and name != provider:
            continue
        attempts += groups[name]
    errors, skip_binance = [], False
    for label, quote, fn in attempts:
        if skip_binance and label.startswith("Binance"):
            continue
        try:
            df = fn()
            if len(df) < 50:
                raise ValueError(f"only {len(df)} usable candles")
            return df.tail(limit).reset_index(drop=True), label, quote, errors
        except Exception as exc:                                  # noqa: BLE001 - report and fall through
            errors.append(f"{label}: {type(exc).__name__}: {exc}")
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if label.startswith("Binance") and status in (403, 451):
                skip_binance = True                               # region block applies to every Binance host
    raise RuntimeError("All selected public candle providers failed. " + " | ".join(errors[-10:]))
