"""Render entry point: python main.py

  * starts the Streamlit dashboard as a child process on $PORT,
  * runs the paper-trading worker in a background thread (works even if nobody
    has the page open), and
  * pings the app's own public URL every 10 minutes so Render's free plan is
    less likely to put it to sleep (also set up an external pinger, see README).
"""
import os
import signal
import subprocess
import sys
import threading
import time

import pandas as pd
import requests

import engine
from signals import INTERVAL_MIN
from store import Store

STORE = Store()


def start_streamlit():
    port = os.environ.get("PORT", "8501")
    cmd = [sys.executable, "-m", "streamlit", "run", "app.py", "--server.port", port,
           "--server.address", "0.0.0.0", "--server.headless", "true",
           "--browser.gatherUsageStats", "false"]
    return subprocess.Popen(cmd)


def pinger():
    url = os.environ.get("RENDER_EXTERNAL_URL")
    while url:
        time.sleep(600)
        try:
            requests.get(url, timeout=20)
        except requests.RequestException as exc:
            print(f"[pinger] {exc}", flush=True)


def worker():
    """Run one cycle right after each candle closes (plus once at start-up)."""
    last_done, retries, last_queue = None, 0, 0.0
    while True:
        if time.time() - last_queue >= 30:                        # manual test trades from the page's button
            last_queue = time.time()
            engine.absorb_queue(STORE)
        try:
            settings = engine.merge_settings(STORE.read("settings"))
            minutes = INTERVAL_MIN[settings["interval"]]
        except Exception as exc:                                  # noqa: BLE001
            print(f"[worker] cannot read settings: {exc}", flush=True)
            minutes = 5
        now = pd.Timestamp.now(tz="UTC")
        boundary = now.floor(f"{minutes}min")
        if last_done != boundary and now >= boundary + pd.Timedelta(seconds=4):
            ok = engine.run_cycle(STORE)
            retries = 0 if ok else retries + 1
            if ok or retries >= 6:                                # give up on this candle after ~6 tries
                last_done, retries = boundary, 0
            time.sleep(10 if not ok else 1)
        time.sleep(10)


def main():
    proc = start_streamlit()
    for target in (worker, pinger):
        threading.Thread(target=target, daemon=True).start()

    def stop(*_):
        proc.terminate()
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while proc.poll() is None:
        time.sleep(5)
    sys.exit(proc.returncode or 1)


if __name__ == "__main__":
    main()
