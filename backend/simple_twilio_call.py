"""
Minimal outbound call with Twilio (inline TwiML — no ngrok).

Requires in backend/.env:
  TWILIO_ACCOUNT_SID
  TWILIO_AUTH_TOKEN
  TWILIO_PHONE_NUMBER   (your Twilio caller ID, E.164)

Usage:
  python simple_twilio_call.py +15551234567
  python simple_twilio_call.py +15551234567 "Custom message here"
"""
import sys
import xml.sax.saxutils

from twilio.rest import Client

from dotenv import load_dotenv

load_dotenv()


def main():
    import os

    def env(k, default=""):
        return (os.environ.get(k, default) or "").strip()

    sid = env("TWILIO_ACCOUNT_SID")
    token = env("TWILIO_AUTH_TOKEN")
    from_num = env("TWILIO_PHONE_NUMBER")

    if len(sys.argv) < 2:
        print("Usage: python simple_twilio_call.py +15551234567 [optional message]")
        sys.exit(1)

    to_num = sys.argv[1].strip()
    message = (
        sys.argv[2]
        if len(sys.argv) > 2
        else "Hello. This is a test call from Twilio."
    )

    if not sid.startswith("AC") or not token:
        print("Set TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN in backend/.env")
        sys.exit(1)
    if not from_num.startswith("+"):
        print("Set TWILIO_PHONE_NUMBER in backend/.env (E.164, e.g. +16505551234)")
        sys.exit(1)

    client = Client(sid, token)

    safe = xml.sax.saxutils.escape(message)
    # Inline TwiML: Twilio speaks text (no webhook URL).
    twiml = f"<Response><Say>{safe}</Say></Response>"

    call = client.calls.create(to=to_num, from_=from_num, twiml=twiml)
    print(f"OK — Call SID: {call.sid}")
    print("Check the phone you dialed; it should ring shortly.")


if __name__ == "__main__":
    main()
