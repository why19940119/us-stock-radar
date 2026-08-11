import json
import os
import smtplib
import ssl
from datetime import datetime
from email.mime.text import MIMEText
from pathlib import Path

EMAIL_STATE_FILE = Path("email_alert_state.json")
MIN_EMAIL_SPIKE_RATIO = 3.0
MIN_EMAIL_DOLLAR_VOLUME = 100_000.0
MIN_EMAIL_TRADES = 20
MIN_EMAIL_ABS_MOVE_PCT = 5.0
EMAIL_COOLDOWN_MINUTES = 60


def _load_state():
    try:
        return json.loads(EMAIL_STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"alerts": {}}


def _save_state(state):
    temporary = EMAIL_STATE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True))
    temporary.replace(EMAIL_STATE_FILE)


def _recipients():
    return [item.strip() for item in os.getenv("ALERT_EMAIL_TO", "").split(",") if item.strip()]


def _qualifying_signals(watchlist):
    return [
        item for item in watchlist
        if item.get("selection_type") == "SPIKE"
        and item.get("spike_ratio") is not None
        and item["spike_ratio"] >= MIN_EMAIL_SPIKE_RATIO
        and item["current_10m_dollar_volume"] >= MIN_EMAIL_DOLLAR_VOLUME
        and item["current_10m_trade_count"] >= MIN_EMAIL_TRADES
        and item.get("current_10m_move_pct") is not None
        and abs(item["current_10m_move_pct"]) >= MIN_EMAIL_ABS_MOVE_PCT
    ]


def send_spike_email_alerts(watchlist, started, session_name, hkt):
    sender = os.getenv("ALERT_EMAIL_FROM", "").strip()
    password = os.getenv("GMAIL_APP_PASSWORD", "").replace(" ", "")
    recipients = _recipients()

    if not sender or not password or not recipients:
        return "disabled (missing email configuration)"

    signals = _qualifying_signals(watchlist)
    if not signals:
        return "no qualifying SPIKE signals"

    state = _load_state()
    alerts = state.get("alerts", {})
    to_send = []

    for item in signals:
        key = f"{session_name}:{item['symbol']}"
        previous = alerts.get(key)
        if previous:
            try:
                age = (started - datetime.fromisoformat(previous)).total_seconds() / 60
                if age < EMAIL_COOLDOWN_MINUTES:
                    continue
            except (TypeError, ValueError):
                pass
        to_send.append(item)

    if not to_send:
        return f"cooldown ({len(signals)} qualifying SPIKE signal(s))"

    lines = [
        "US Stock Radar V2 — Extended-Hours SPIKE Alert",
        "",
        f"Generated: {started.astimezone(hkt).strftime('%Y-%m-%d %H:%M:%S HKT')}",
        f"Session: {session_name}",
        f"Qualified signals: {len(to_send)}",
        "",
    ]

    for item in to_send:
        move = item.get("current_10m_move_pct")
        move_text = f"{move:+.2f}%" if move is not None else "N/A"
        lines.extend([
            f"{item['symbol']} — {item['name']}",
            f"Price: ${item['price']:.2f}",
            f"10m dollar volume: ${item['current_10m_dollar_volume']:,.0f}",
            f"Spike ratio: {item['spike_ratio']:.2f}x",
            f"10m move: {move_text}",
            f"10m trades: {item['current_10m_trade_count']:,}",
            "",
        ])

    message = MIMEText("\n".join(lines), "plain", "utf-8")
    message["Subject"] = f"[Stock Radar V2] {session_name}: {len(to_send)} SPIKE alert(s)"
    message["From"] = sender
    message["To"] = ", ".join(recipients)

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
            server.login(sender, password)
            server.sendmail(sender, recipients, message.as_string())
    except Exception as error:
        return f"failed ({type(error).__name__}: {error})"

    sent_at = started.isoformat()
    for item in to_send:
        alerts[f"{session_name}:{item['symbol']}"] = sent_at
    state["alerts"] = alerts
    _save_state(state)
    return f"sent ({len(to_send)} signal(s) to {len(recipients)} recipient(s))"
