import os
import sys
import requests
from dotenv import load_dotenv

load_dotenv()

alpaca_key = os.getenv("ALPACA_API_KEY")
alpaca_secret = os.getenv("ALPACA_API_SECRET")
fmp_key = os.getenv("FMP_API_KEY")

required = {
    "ALPACA_API_KEY": alpaca_key,
    "ALPACA_API_SECRET": alpaca_secret,
    "FMP_API_KEY": fmp_key,
}

missing = [name for name, value in required.items() if not value]
if missing:
    print("Missing .env values: " + ", ".join(missing))
    sys.exit(1)

alpaca_response = requests.get(
    "https://paper-api.alpaca.markets/v2/account",
    headers={
        "APCA-API-KEY-ID": alpaca_key,
        "APCA-API-SECRET-KEY": alpaca_secret,
    },
    timeout=15,
)

if alpaca_response.ok:
    account = alpaca_response.json()
    print("Alpaca Paper: OK")
    print("Account status:", account.get("status"))
    print("Equity:", account.get("equity"))
else:
    print("Alpaca Paper: FAILED, HTTP", alpaca_response.status_code)

fmp_response = requests.get(
    "https://f    "https://f    "htcom/stable/quote",
          ={"symbol": "AAPL", "apikey": fmp_key},
    timeout=15,
)

fmp_data = fmp_response.json() if fmp_response.ok else None

if fmp_response.ok and isinstance(fmp_data, list) and fmp_data:
    quote = fmp_data[    quote = fmp_data[    quoteint("Symbol:", quote.get("symbol"))
    print("Price:", quote.get("price"))    print("Price:", quote.get("price"))    print("Price:", que)
