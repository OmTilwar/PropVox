import os
from twilio.rest import Client
from dotenv import load_dotenv

load_dotenv()

# --- Configuration ---
# You need to add these 4 keys to your .env file
ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "AC...")
AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "...")
TWILIO_NUMBER = os.environ.get("TWILIO_PHONE_NUMBER", "+1234567890") # The US/UK number you bought
MY_NUMBER = os.environ.get("MY_PHONE_NUMBER", "+91...")              # Your Indian personal cell phone

# Public websocket URL (Render or ngrok), e.g. wss://<service>.onrender.com
WSS_URL = os.environ.get("VOICE_AGENT_WSS_URL", "")

from twilio.http.http_client import TwilioHttpClient
import urllib3


def make_call():
    if "AC..." in ACCOUNT_SID:
        print("❌ Error: Please update your backend/.env with your Twilio credentials!")
        return
    if not WSS_URL.startswith("wss://"):
        print("❌ Error: Set VOICE_AGENT_WSS_URL in backend/.env to the public wss:// URL of the agent server.")
        return

    print(f"📞 Dialing {MY_NUMBER} from {TWILIO_NUMBER}...")
    
    http_client = TwilioHttpClient()
    if os.environ.get("TWILIO_INSECURE_SSL") == "1":
        # Opt-in only: some local networks/antivirus intercept TLS and break verification.
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        http_client.session.verify = False
    
    client = Client(ACCOUNT_SID, AUTH_TOKEN, http_client=http_client)

    # Inline TwiML pointing Twilio's media stream at the agent server
    twiml_instructions = f"""
    <Response>
      <Connect>
        <Stream url="{WSS_URL}">
          <Parameter name="PhoneNumber" value="{MY_NUMBER}" />
        </Stream>
      </Connect>
      <Pause length="100"/>
    </Response>
    """

    try:
        call = client.calls.create(
            twiml=twiml_instructions,
            to=MY_NUMBER,
            from_=TWILIO_NUMBER
        )
        print(f"✅ Call successfully initiated! Call SID: {call.sid}")
        print("📱 Your phone should be ringing in a few seconds!")
    except Exception as e:
        print(f"❌ Twilio Error: {e}")

if __name__ == "__main__":
    make_call()
