# Complete V1A.1 source code is being uploaded here.
# Regular-session IEX candidate-pool builder.
# No email, state machine, or trading functionality.

import json
import os
import re
import time as time_module
from collections import deque
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

MIN_MARKET_CAP = 10_000_000
MIN_PRICE = 1.00
UNIVERSE_BATCH_SIZE = 100
TOP_TOTAL_SIZE = 30
TOP_LIQUID_SIZE = 10
LOOKBACK_MINUTES = 40
ROLLING_WINDOW_MINUTES = 10
MAX_LAST_BAR_AGE_MINUTES = 3
MIN_SPIKE_DOLLAR_VOLUME = 100_000.0
MIN_SPIKE_TRADES = 20
NASDAQ_URL = "https://api.nasdaq.com/api/screener/stocks"
ALPACA_BARS_URL = "https://data.alpaca.markets/v2/stocks/bars"
EXCHANGES = ("nasdaq", "nyse", "amex")
UNIVERSE_CACHE_FILE = Path("universe_cache.json")
WATCHLIST_FILE = Path("watchlist.json")
ET = ZoneInfo("America/New_York")
HKT = ZoneInfo("Asia/Hong_Kong")
NASDAQ_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json, text/plain, */*", "Accept-Language": "en-US,en;q=0.9"}
EXCLUDED_WORDS = ("ETF", "FUND", "WARRANT", " RIGHT", " UNIT", "PREFERRED", "DEPOSITARY", " NOTE", " BOND")

def number(value):
    text = re.sub(r"[^0-9.\\-]", "", str(value or ""))
    return float(text) if text not in ("", "-", ".") else None

def parse_timestamp(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None

def save_json_atomic(path, data):
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    tmp.replace(path)

def load_json(path, default):
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default

def http_session(headers=None):
    retry = Retry(total=3, backoff_factor=1, status_forcelist=(429, 500, 502, 503, 504), allowed_methods=("GET",))
    s = requests.Session()
    s.mount("https://", HTTPAdapter(max_retries=retry))
    if headers:
        s.headers.update(headers)
    return s

def regular_session(now):
    return now.weekday() < 5 and time(9, 30) <= now.time() < time(16, 0)

def fetch_universe():
    s = http_session(NASDAQ_HEADERS)
    out = {}
    for exchange in EXCHANGES:
        r = s.get(NASDAQ_URL, params={"tableonly": "true", "limit": 5000, "exchange": exchange, "download": "true"}, timeout=30)
        r.raise_for_status()
        for row in r.json().get("data", {}).get("rows", []):
            symbol = str(row.get("symbol", "")).strip().upper().replace("/", ".")
            name = str(row.get("name", "")).strip()
            cap = number(row.get("marketCap"))
            if symbol and cap is not None and cap >= MIN_MARKET_CAP and not any(w in name.upper() for w in EXCLUDED_WORDS):
                out[symbol] = {"symbol": symbol, "name": name, "exchange": exchange.upper(), "market_cap": cap}
    return out

def get_universe(now):
    cached = load_json(UNIVERSE_CACHE_FILE, {})
    t = parse_timestamp(cached.get("updated_at"))
    if t and cached.get("universe") and now - t < timedelta(hours=24):
        print("Universe source: local cache")
        return cached["universe"]
    try:
        print("Universe source: NASDAQ screener API")
        u = fetch_universe()
        save_json_atomic(UNIVERSE_CACHE_FILE, {"updated_at": now.isoformat(), "universe": u})
        return u
    except requests.RequestException as e:
        if cached.get("universe"):
            print("NASDAQ fetch failed; using local cache:", e)
            return cached["universe"]
        raise SystemExit(f"NASDAQ fetch failed and no cache exists: {e}")

def fetch_batch(s, symbols, start, end):
    result = {}
    token = None
    while True:
        params = {"symbols": ",".join(symbols), "timeframe": "1Min", "start": start.isoformat(), "end": end.isoformat(), "feed": "iex", "adjustment": "raw", "limit": 10000}
        if token:
            params["page_token"] = token
        r = s.get(ALPACA_BARS_URL, params=params, timeout=30)
        r.raise_for_status()
        payload = r.json()
        for symbol, bars in payload.get("bars", {}).items():
            result.setdefault(symbol, []).extend(bars)
        token = payload.get("next_page_token")
        if not token:
            return result

def fetch_bars(symbols, now):
    load_dotenv()
    s = http_session({"APCA-API-KEY-ID": os.getenv("ALPACA_API_KEY", ""), "APCA-API-SECRET-KEY": os.getenv("ALPACA_API_SECRET", "")})
    start = now - timedelta(minutes=LOOKBACK_MINUTES + 1)
    out = {}
    for i in range(0, len(symbols), UNIVERSE_BATCH_SIZE):
        try:
            out.update(fetch_batch(s, symbols[i:i + UNIVERSE_BATCH_SIZE], start, now))
        except requests.RequestException as e:
            print(f"Warning: bars batch {i // UNIVERSE_BATCH_SIZE + 1} failed: {e}")
        time_module.sleep(0.05)
    return out

def metrics(bars, now):
    rows = []
    for b in bars:
        t, c, v, n = parse_timestamp(b.get("t")), number(b.get("c")), number(b.get("v")), number(b.get("n"))
        if t and c is not None and v is not None:
            rows.append({"time": t.astimezone(ET), "close": c, "volume": v, "trades": int(n or 0), "dollar": c * v})
    if not rows:
        return None
    rows.sort(key=lambda x: x["time"])
    latest = rows[-1]
    if now - latest["time"] > timedelta(minutes=MAX_LAST_BAR_AGE_MINUTES) or latest["close"] < MIN_PRICE:
        return None
    recent_cutoff = now - timedelta(minutes=ROLLING_WINDOW_MINUTES)
    history_cutoff = now - timedelta(minutes=LOOKBACK_MINUTES)
    q, dollar, volume, trades = deque(), 0.0, 0.0, 0
    recent, history = [], []
    for row in rows:
        q.append(row); dollar += row["dollar"]; volume += row["volume"]; trades += row["trades"]
        cutoff = row["time"] - timedelta(minutes=ROLLING_WINDOW_MINUTES)
        while q and q[0]["time"] <= cutoff:
            old = q.popleft(); dollar -= old["dollar"]; volume -= old["volume"]; trades -= old["trades"]
        w = {"end": row["time"], "dollar": dollar, "volume": volume, "trades": trades}
        if row["time"] >= recent_cutoff: recent.append(w)
        elif row["time"] >= history_cutoff: history.append(w)
    if not recent:
        return None
    best = max(recent, key=lambda x: x["dollar"])
    avg = sum(x["dollar"] for x in history) / len(history) if history else 0.0
    first = next((x["close"] for x in rows if x["time"] >= recent_cutoff), None)
    return {"price": latest["close"], "latest_bar_time": latest["time"].isoformat(), "recent_10m_dollar_volume": round(best["dollar"], 2), "recent_10m_volume": round(best["volume"], 0), "recent_10m_trade_count": best["trades"], "spike_ratio": best["dollar"] / avg if avg else None, "recent_move_pct": (latest["close"] / first - 1) * 100 if first else None}

def main():
    now = datetime.now(ET)
    print("\\nUS Stock Radar V1A.1 — Candidate Pool Builder")
    print("Generated:", now.astimezone(HKT).strftime("%Y-%m-%d %H:%M:%S HKT"))
    if not regular_session(now):
        print("Status: CLOSED FOR V1A.1"); return
    load_dotenv()
    if not os.getenv("ALPACA_API_KEY") or not os.getenv("ALPACA_API_SECRET"):
        raise SystemExit("Missing ALPACA_API_KEY or ALPACA_API_SECRET in .env")
    started = now; universe = get_universe(now); bars = fetch_bars(list(universe), now); candidates = []
    for symbol, company in universe.items():
        m = metrics(bars.get(symbol, []), now)
        if m: candidates.append({**company, **m})
    liquid = sorted(candidates, key=lambda x: x["recent_10m_dollar_volume"], reverse=True)[:TOP_LIQUID_SIZE]
    used = {x["symbol"] for x in liquid}
    spike = [x for x in candidates if x["symbol"] not in used and x.get("spike_ratio") is not None and x["recent_10m_dollar_volume"] >= MIN_SPIKE_DOLLAR_VOLUME and x["recent_10m_trade_count"] >= MIN_SPIKE_TRADES]
    spike.sort(key=lambda x: x["spike_ratio"], reverse=True)
    watchlist = liquid + spike[:TOP_TOTAL_SIZE - len(liquid)]
    save_json_atomic(WATCHLIST_FILE, {"generated_at_et": started.isoformat(), "session": "REGULAR", "feed": "iex", "universe_size": len(universe), "symbols_with_recent_iex_bars": len(candidates), "tickers": watchlist})
    print("Universe size:", len(universe)); print("Symbols with recent IEX bar:", len(candidates)); print("Watchlist written:", WATCHLIST_FILE); print("Qualifying watchlist:", len(watchlist)); print("Email: disabled"); print("Signals: disabled"); print("State machine: disabled")

if __name__ == "__main__":
    main()
