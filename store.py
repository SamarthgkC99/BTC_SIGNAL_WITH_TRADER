"""Tiny key/value store shared by the worker and the dashboard.

Backend is chosen on every call:
  1. Upstash Redis REST, if credentials exist (env vars first, then data/creds.json
     saved from the Settings tab);
  2. otherwise a local JSON file (data/<key>.json) - fine for testing, but NOT
     persistent on Render's free plan.
Secrets are never logged or returned to the page.
"""
import json
import os
import time

import requests

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
CREDS_FILE = os.path.join(DATA_DIR, "creds.json")
PREFIX = "btc:"


def _norm_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if url and not url.startswith("http"):
        url = "https://" + url
    return url


def get_creds():
    """Return (url, token, origin). origin is 'env', 'saved' or None."""
    url, token = os.environ.get("UPSTASH_REDIS_REST_URL"), os.environ.get("UPSTASH_REDIS_REST_TOKEN")
    if url and token:
        return _norm_url(url), token.strip(), "env"
    try:
        with open(CREDS_FILE, encoding="utf-8") as fh:
            d = json.load(fh)
        if d.get("url") and d.get("token"):
            return _norm_url(d["url"]), d["token"].strip(), "saved"
    except (OSError, ValueError):
        pass
    return None, None, None


def save_creds(url: str, token: str):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CREDS_FILE, "w", encoding="utf-8") as fh:
        json.dump({"url": _norm_url(url), "token": token.strip()}, fh)
    try:
        os.chmod(CREDS_FILE, 0o600)
    except OSError:
        pass


def clear_creds():
    try:
        os.remove(CREDS_FILE)
    except OSError:
        pass


def _redis(url, token, *cmd):
    r = requests.post(url, json=list(cmd), headers={"Authorization": f"Bearer {token}"}, timeout=10)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(str(data["error"]))
    return data.get("result")


def test_connection(url: str, token: str) -> str:
    """Raises on failure; returns 'ok'."""
    url = _norm_url(url)
    stamp = str(int(time.time()))
    _redis(url, token.strip(), "SET", PREFIX + "ping", stamp)
    if _redis(url, token.strip(), "GET", PREFIX + "ping") != stamp:
        raise RuntimeError("write/read check did not match")
    return "ok"


class Store:
    def backend(self) -> str:
        return "Upstash Redis" if get_creds()[0] else "Local file (not persistent on free hosting)"

    def origin(self):
        return get_creds()[2]

    def read(self, key):
        """Return the stored object, or None if the key does not exist. Raises on errors."""
        url, token, _ = get_creds()
        if url:
            raw = _redis(url, token, "GET", PREFIX + key)
            return json.loads(raw) if raw else None
        path = os.path.join(DATA_DIR, f"{key}.json")
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def write(self, key, obj):
        payload = json.dumps(obj, default=str)
        url, token, _ = get_creds()
        if url:
            _redis(url, token, "SET", PREFIX + key, payload)
            return
        os.makedirs(DATA_DIR, exist_ok=True)
        path = os.path.join(DATA_DIR, f"{key}.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, path)
