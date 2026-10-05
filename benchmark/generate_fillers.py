"""
Generate ONLY missing filler MP3 clips (FINAL CLEAN VERSION)

Run:
  python generate_fillers.py
"""

import base64
import json
import os
import sys
import time
import asyncio
import websockets
from dotenv import load_dotenv

# =========================
# 🔐 Load API Key
# =========================
load_dotenv()
SARVAM_API_KEY = os.environ.get("SARVAM_API_KEY", "")

if not SARVAM_API_KEY:
    print("❌ SARVAM_API_KEY is not set")
    sys.exit(1)

# =========================
# 🎙️ ONLY REMAINING FILLERS
# =========================
FILLERS = {
    "ack_hi": "Hi",
    "ack_hello": "Hello",
}

OUTPUT_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "backend", "filler_audio")
)
DELAY_BETWEEN_REQUESTS = 2.5
RETRY_DELAY = 3


# =========================
# 🔊 GENERATE ONE
# =========================
async def generate_one(text, filename):
    url = "wss://api.sarvam.ai/text-to-speech/ws?model=bulbul:v3&send_completion_event=true"
    audio_data = b""

    try:
        async with websockets.connect(
            url,
            extra_headers={"api-subscription-key": SARVAM_API_KEY},
            open_timeout=15,
        ) as ws:

            await ws.send(json.dumps({
                "type": "config",
                "data": {
                    "target_language_code": "hi-IN",
                    "speaker": "simran",
                    "speech_sample_rate": 24000,
                    "pace": 1.05,
                    "enable_preprocessing": True,
                    "output_audio_codec": "mp3",
                }
            }))

            await ws.send(json.dumps({"type": "text", "data": {"text": text}}))
            await ws.send(json.dumps({"type": "flush"}))

            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=20)
                except:
                    break

                if isinstance(raw, bytes):
                    audio_data += raw
                else:
                    payload = json.loads(raw)

                    if payload.get("type") == "audio":
                        b64 = payload.get("data", {}).get("audio", "")
                        if b64:
                            audio_data += base64.b64decode(b64)

                    elif payload.get("type") == "event":
                        if payload.get("data", {}).get("event_type") == "final":
                            break

    except Exception as e:
        # Save partial audio if available
        if audio_data:
            with open(filename, "wb") as f:
                f.write(audio_data)
            print(f"✅ (partial saved) {filename}")
            return True

        print(f"❌ Error: {text} | {str(e)}")
        return False

    if audio_data:
        with open(filename, "wb") as f:
            f.write(audio_data)
        print(f"✅ {filename}")
        return True

    print(f"❌ No audio: {text}")
    return False


# =========================
# 🚀 MAIN
# =========================
def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print(f"🎙️ Generating {len(FILLERS)} missing fillers...\n")

    for key, text in FILLERS.items():
        filename = os.path.join(OUTPUT_DIR, f"{key}.mp3")

        # Skip if already exists (extra safety)
        if os.path.exists(filename):
            print(f"⏭️ Skipping {key}")
            continue

        for attempt in range(3):
            success = asyncio.run(generate_one(text, filename))

            if success:
                break

            print(f"🔁 Retry {attempt+1} for {key}")
            time.sleep(RETRY_DELAY)

        time.sleep(DELAY_BETWEEN_REQUESTS)

    print("\n✅ DONE — All missing fillers attempted!")


if __name__ == "__main__":
    main()