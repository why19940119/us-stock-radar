# US Stock Radar

Finished V1 tool: builds a US equity **candidate pool** (watchlist) from Alpaca **IEX** 1-minute bars during the US regular session. It writes `watchlist.json`. It does **not** place trades, send email, or run a signal/state machine.

## What it does (V1A.1)

- Runs only during US regular hours (Mon–Fri 09:30–16:00 America/New_York), unless you use dry-run (below).
- Loads a NASDAQ/NYSE/AMEX universe (cached locally for 24h in `universe_cache.json`).
- Pulls recent IEX 1-minute bars from Alpaca.
- Ranks candidates by recent dollar volume and spike ratio.
- Writes `watchlist.json` (top liquid + spike names, capped size).

## Setup

1. Python 3.10+ recommended.
2. Create a virtualenv and install deps:

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

3. Copy env template and add your Alpaca **data** API keys (IEX feed):

```bash
cp .env.example .env
# edit .env — set ALPACA_API_KEY and ALPACA_API_SECRET
```

Never commit `.env`. It is gitignored.

## Run once (live)

During US regular hours, with valid Alpaca keys in `.env`:

```bash
python stock_radar_v1.py
```

Expected console lines include universe size, count of symbols with recent IEX bars, and `Watchlist written: watchlist.json`. Outside regular hours the script prints `Status: CLOSED FOR V1A.1` and exits without writing.

### Output

`watchlist.json` (gitignored) includes metadata (`generated_at_et`, `session`, `feed`, sizes) and a `tickers` array of candidate rows (symbol, name, exchange, market_cap, price, volume/spike metrics).

## Acceptance without live market (dry-run)

When the market is closed or you do not want to call Alpaca:

```bash
python stock_radar_v1.py --dry-run
```

This uses a built-in fixture universe and synthetic 1-minute bars (no `.env` / Alpaca required), still writes `watchlist.json`, and prints the same summary style. Use this for smoke verification any time.

## Hard non-goals (out of scope for V1)

- No order placement or brokerage trading.
- No email alerts (other scripts in the repo are experimental and not part of this product path).
- No AI / LLM features.
- No signal engine or state machine.

## Other files in this repo

| File | Role |
|------|------|
| `stock_radar_v1.py` | **Product entrypoint** (this README) |
| `stock_radar_v2_extended.py` | Experimental extended radar — not the V1 product path |
| `email_alerts.py` / `send_test_email.py` | Experimental — not enabled for V1 |
| `check_connections.py` | Utility to probe credentials |

## Requirements

See `requirements.txt`: `requests`, `python-dotenv`, `pandas`.
