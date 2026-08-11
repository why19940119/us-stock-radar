import json
import os
import re
import time as sleep_time
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from email_alerts import send_spike_email_alerts
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

MIN_MARKET_CAP = 10_000_000
MIN_PRICE = 1.00

BATCH_SIZE = 100
TOP_TOTAL = 30
TOP_LIQUID = 10

LOOKBACK_MINUTES = 40
CURRENT_WINDOW_MINUTES = 10
BASELINE_WINDOW_MINUTES = 30
MIN_CURRENT_BARS = 3
MIN_BASELINE_BARS = 6

MAX_IEX_BAR_AGE_MINUTES = 15
MAX_OVERNIGHT_BAR_AGE_MINUTES = 30

MIN_SPIKE_DOLLAR_VOLUME = 100_000.0
MIN_SPIKE_TRADES = 20
SIP_DELAY_MINUTES = 16

NASDAQ_URL = "https://api.nasdaq.com/api/screener/stocks"
BARS_URL = "https://data.alpaca.markets/v2/stocks/bars"

EXCHANGES = ("nasdaq", "nyse", "amex")

UNIVERSE_CACHE = Path("universe_cache.json")
WATCHLIST_FILE = Path("watchlist.json")

ET = ZoneInfo("America/New_York")
HKT = ZoneInfo("Asia/Hong_Kong")

NASDAQ_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

EXCLUDED_WORDS = (
    "ETF",
    "FUND",
    "WARRANT",
    " RIGHT",
    " UNIT",
    "PREFERRED",
    "DEPOSITARY",
    " NOTE",
    " BOND",
)


def number(value):
    text = re.sub(r"[^0-9.\-]", "", str(value or ""))
    return float(text) if text not in ("", "-", ".") else None


def parse_time(value):
    if not value:
        return None

    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def load_json(path, default):
    try:
        with path.open(encoding="utf-8") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError):
        return default


def save_json_atomic(path, data):
    temporary = path.with_suffix(".tmp")

    with temporary.open("w", encoding="utf-8") as file:
        json.dump(data, file, indent=2)

    temporary.replace(path)


def http_session(headers=None):
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )

    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry))

    if headers:
        session.headers.update(headers)

    return session


def session_info(now):
    current = now.time()

    if now.weekday() >= 5:
        return "CLOSED", None, now, False, 0

    if time(4, 0) <= current < time(9, 30):
        start = now.replace(hour=4, minute=0, second=0, microsecond=0)
        return "PRE-MARKET", "iex", start, False, 0

    if time(9, 30) <= current < time(16, 0):
        start = now.replace(hour=9, minute=30, second=0, microsecond=0)
        return "REGULAR", "iex", start, False, 0

    if time(16, 0) <= current < time(20, 0):
        start = now.replace(hour=16, minute=0, second=0, microsecond=0)
        return "AFTER-HOURS", "iex", start, False, 0

    if current < time(4, 0):
        start = (now - timedelta(days=1)).replace(
            hour=20,
            minute=0,
            second=0,
            microsecond=0,
        )
    else:
        start = now.replace(
            hour=20,
            minute=0,
            second=0,
            microsecond=0,
        )

    return "OVERNIGHT", "overnight", start, True, 15


def fetch_universe():
    session = http_session(NASDAQ_HEADERS)
    universe = {}

    for exchange in EXCHANGES:
        response = session.get(
            NASDAQ_URL,
            params={
                "tableonly": "true",
                "limit": 5000,
                "exchange": exchange,
                "download": "true",
            },
            timeout=30,
        )
        response.raise_for_status()

        rows = response.json().get("data", {}).get("rows", [])

        for row in rows:
            symbol = str(row.get("symbol", "")).strip().upper().replace("/", ".")
            name = str(row.get("name", "")).strip()
            market_cap = number(row.get("marketCap"))

            if not symbol or market_cap is None:
                continue

            if market_cap < MIN_MARKET_CAP:
                continue

            if any(word in name.upper() for word in EXCLUDED_WORDS):
                continue

            universe[symbol] = {
                "symbol": symbol,
                "name": name,
                "exchange": exchange.upper(),
                "market_cap": market_cap,
            }

    return universe


def get_universe(now):
    cached = load_json(UNIVERSE_CACHE, {})
    cached_at = parse_time(cached.get("updated_at"))

    if cached_at and cached.get("universe"):
        age = now.astimezone(cached_at.tzinfo) - cached_at

        if age < timedelta(hours=24):
            print("Universe source: local cache")
            return cached["universe"]

    try:
        print("Universe source: NASDAQ screener API")
        universe = fetch_universe()

        save_json_atomic(
            UNIVERSE_CACHE,
            {
                "updated_at": now.isoformat(),
                "universe": universe,
            },
        )

        return universe

    except requests.RequestException as error:
        if cached.get("universe"):
            print("NASDAQ fetch failed; using local cache:", error)
            return cached["universe"]

        raise SystemExit(
            f"NASDAQ fetch failed and no local cache exists: {error}"
        )


def fetch_batch(session, symbols, start, end, feed):
    result = {}
    page_token = None

    while True:
        params = {
            "symbols": ",".join(symbols),
            "timeframe": "1Min",
            "start": start.isoformat(),
            "end": end.isoformat(),
            "feed": feed,
            "adjustment": "raw",
            "limit": 10000,
        }

        if page_token:
            params["page_token"] = page_token

        response = session.get(BARS_URL, params=params, timeout=30)
        response.raise_for_status()

        payload = response.json()

        for symbol, bars in payload.get("bars", {}).items():
            result.setdefault(symbol, []).extend(bars)

        page_token = payload.get("next_page_token")

        if not page_token:
            return result


def fetch_bars(symbols, now, session_start, feed):
    headers = {
        "APCA-API-KEY-ID": os.getenv("ALPACA_API_KEY", ""),
        "APCA-API-SECRET-KEY": os.getenv("ALPACA_API_SECRET", ""),
    }

    session = http_session(headers)

    start = max(
        now - timedelta(minutes=LOOKBACK_MINUTES + 1),
        session_start,
    )

    all_bars = {}
    failed_batches = 0

    for index in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[index:index + BATCH_SIZE]

        try:
            all_bars.update(
                fetch_batch(session, batch, start, now, feed)
            )
        except requests.RequestException as error:
            failed_batches += 1
            print(
                f"Warning: bars batch "
                f"{index // BATCH_SIZE + 1} failed: {error}"
            )

        sleep_time.sleep(0.05)

    return all_bars, failed_batches


def parse_bars(bars):
    rows = []

    for bar in bars:
        timestamp = parse_time(bar.get("t"))
        close = number(bar.get("c"))
        volume = number(bar.get("v"))
        trades = number(bar.get("n"))

        if timestamp is None or close is None or volume is None:
            continue

        rows.append({
            "time": timestamp.astimezone(ET),
            "close": close,
            "volume": volume,
            "trades": int(trades or 0),
            "dollar": close * volume,
        })

    return sorted(rows, key=lambda row: row["time"])


def calculate_metrics(bars, now, session_start, delayed):
    rows = [
        row for row in parse_bars(bars)
        if row["time"] >= session_start
    ]

    if not rows:
        return None

    latest = rows[-1]

    max_age = (
        MAX_OVERNIGHT_BAR_AGE_MINUTES
        if delayed
        else MAX_IEX_BAR_AGE_MINUTES
    )

    if now - latest["time"] > timedelta(minutes=max_age):
        return None

    if latest["close"] < MIN_PRICE:
        return None

    end = latest["time"] + timedelta(minutes=1)
    current_start = end - timedelta(minutes=CURRENT_WINDOW_MINUTES)
    baseline_start = current_start - timedelta(minutes=BASELINE_WINDOW_MINUTES)

    current = [
        row for row in rows
        if current_start <= row["time"] < end
    ]

    baseline = [
        row for row in rows
        if baseline_start <= row["time"] < current_start
    ]

    current_dollar = sum(row["dollar"] for row in current)
    current_volume = sum(row["volume"] for row in current)
    current_trades = sum(row["trades"] for row in current)
    baseline_dollar = sum(row["dollar"] for row in baseline)

    session_has_enough_history = baseline_start >= session_start

    spike_ratio = None

    if (
        session_has_enough_history
        and len(current) >= MIN_CURRENT_BARS
        and len(baseline) >= MIN_BASELINE_BARS
        and baseline_dollar > 0
    ):
        spike_ratio = current_dollar / (baseline_dollar / 3.0)

    first_price = current[0]["close"] if current else None

    move_pct = None
    if first_price and first_price > 0:
        move_pct = (latest["close"] / first_price - 1) * 100

    return {
        "price": latest["close"],
        "latest_bar_time": latest["time"].isoformat(),
        "current_10m_dollar_volume": round(current_dollar, 2),
        "current_10m_volume": round(current_volume, 0),
        "current_10m_trade_count": current_trades,
        "baseline_30m_dollar_volume": round(baseline_dollar, 2),
        "current_bar_count": len(current),
        "baseline_bar_count": len(baseline),
        "spike_ratio": spike_ratio,
        "current_10m_move_pct": move_pct,
        "spike_eligible": spike_ratio is not None,
    }


def build_watchlist(
    universe,
    bars_by_symbol,
    now,
    session_start,
    delayed,
):
    candidates = []

    for symbol, company in universe.items():
        metrics = calculate_metrics(
            bars_by_symbol.get(symbol, []),
            now,
            session_start,
            delayed,
        )

        if metrics:
            candidates.append({**company, **metrics})

    liquid = sorted(
        candidates,
        key=lambda item: item["current_10m_dollar_volume"],
        reverse=True,
    )

    top_liquid = liquid[:TOP_LIQUID]
    used_symbols = {item["symbol"] for item in top_liquid}

    spike_pool = [
        item for item in candidates
        if item["symbol"] not in used_symbols
        and item["spike_eligible"]
        and item["current_10m_dollar_volume"]
        >= MIN_SPIKE_DOLLAR_VOLUME
        and item["current_10m_trade_count"] >= MIN_SPIKE_TRADES
    ]

    spike_pool.sort(
        key=lambda item: item["spike_ratio"],
        reverse=True,
    )

    for item in top_liquid:
        item["selection_type"] = "LIQUIDITY"

    selected_spikes = spike_pool[:TOP_TOTAL - len(top_liquid)]

    for item in selected_spikes:
        item["selection_type"] = "SPIKE"

    return top_liquid + selected_spikes, len(candidates)


def main():
    load_dotenv()

    if not os.getenv("ALPACA_API_KEY") or not os.getenv("ALPACA_API_SECRET"):
        raise SystemExit(
            "Missing ALPACA_API_KEY or ALPACA_API_SECRET in .env"
        )

    started = datetime.now(ET)

    # Free SIP requests must end at least 15 minutes in the past.
    market_now = started - timedelta(minutes=SIP_DELAY_MINUTES)

    session_name, _, session_start, _, _ = session_info(market_now)
    feed = "sip"
    delayed = True
    delay_minutes = SIP_DELAY_MINUTES

    print("\nUS Stock Radar V2 — Extended-Hours Candidate Pool")
    print(
        "Generated:",
        started.astimezone(HKT).strftime("%Y-%m-%d %H:%M:%S HKT"),
    )

    if session_name == "CLOSED":
        print("Status: CLOSED — weekend")
        return

    print("Session:", session_name)
    print("Feed:", feed)

    if delayed:
        print(f"Data delayed: True ({delay_minutes} minutes)")
    else:
        print("Data delayed: False")

    print("Session start ET:", session_start.strftime("%Y-%m-%d %H:%M:%S"))
    print("Market data as of ET:", market_now.strftime("%Y-%m-%d %H:%M:%S"))

    universe = get_universe(started)

    print("Universe size:", len(universe))
    print("Windows: current 10 minutes vs preceding 30 minutes")
    print("Fetching bars...")

    bars_by_symbol, failed_batches = fetch_bars(
        list(universe),
        market_now,
        session_start,
        feed,
    )

    watchlist, eligible_count = build_watchlist(
        universe,
        bars_by_symbol,
        market_now,
        session_start,
        delayed,
    )

    output = {
        "version": "V2",
        "generated_at_et": started.isoformat(),
        "generated_at_hkt": started.astimezone(HKT).isoformat(),
        "market_data_as_of_et": market_now.isoformat(),
        "session": session_name,
        "feed": feed,
        "data_delayed": delayed,
        "delay_minutes": delay_minutes,
        "session_start_et": session_start.isoformat(),
        "universe_size": len(universe),
        "symbols_with_recent_session_bars": eligible_count,
        "failed_batches": failed_batches,
        "current_window_minutes": CURRENT_WINDOW_MINUTES,
        "baseline_window_minutes": BASELINE_WINDOW_MINUTES,
        "tickers": watchlist,
    }

    save_json_atomic(WATCHLIST_FILE, output)
    email_status = send_spike_email_alerts(
        watchlist,
        started,
        session_name,
        HKT,
    )

    print("-" * 155)
    print(
        f"{'Rank':>4} {'Type':<10} {'Ticker':<8} {'Price':>10} "
        f"{'10m $Vol':>16} {'Spike':>9} {'10m Move':>10} Name"
    )
    print("-" * 155)

    for rank, item in enumerate(watchlist, start=1):
        spike_text = (
            f"{item['spike_ratio']:.2f}x"
            if item["spike_ratio"] is not None
            else "N/A"
        )

        move_text = (
            f"{item['current_10m_move_pct']:+.2f}%"
            if item["current_10m_move_pct"] is not None
            else "N/A"
        )

        print(
            f"{rank:>4} "
            f"{item['selection_type']:<10} "
            f"{item['symbol']:<8} "
            f"${item['price']:>8.2f} "
            f"${item['current_10m_dollar_volume']:>14,.0f} "
            f"{spike_text:>9} "
            f"{move_text:>10} "
            f"{item['name']}"
        )

    print("-" * 155)
    print("Symbols with recent session bars:", eligible_count)
    print("Failed API batches:", failed_batches)
    print("Watchlist written:", WATCHLIST_FILE)
    print("Email:", email_status)
    print("Signals: disabled")
    print("State machine: disabled")


if __name__ == "__main__":
    main()
