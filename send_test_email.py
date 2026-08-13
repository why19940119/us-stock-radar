import os
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))

sender = os.getenv("ALERT_EMAIL_FROM", "").strip()
password = os.getenv("GMAIL_APP_PASSWORD", "").replace(" ", "")
recipients = [item.strip() for item in os.getenv("ALERT_EMAIL_TO", "").split(",") if item.strip()]

if not sender or not password or not recipients:
    raise SystemExit("Email configuration is incomplete.")

message = EmailMessage()
message["Subject"] = "[Stock Radar V2] Email delivery test"
message["From"] = sender
message["To"] = ", ".join(recipients)
message.set_content("Email alerts are configured successfully.\n\nThis is a one-time delivery test from Stock Radar V2.")

context = ssl.create_default_context()
with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
    server.login(sender, password)
    server.send_message(message, from_addr=sender, to_addrs=recipients)

print(f"Test email sent to {len(recipients)} recipient(s).")
