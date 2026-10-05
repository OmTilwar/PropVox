import os
import json
import asyncio
import websockets
from urllib.parse import urlencode
from dotenv import load_dotenv

load_dotenv()

class DeepgramSTTLayer:
    """
    Deepgram streaming STT over WebSockets (no SDK).

    Uses Nova-3 with language=multi for English + Hindi (and natural Hinglish code-switching).
    See: https://developers.deepgram.com/docs/multilingual-code-switching
    """
    def __init__(self, api_key=None):
        self.api_key = api_key or os.environ.get("DEEPGRAM_API_KEY")
        if not self.api_key:
            raise ValueError("DEEPGRAM_API_KEY is not set.")
        self.ws = None

    async def connect(self, on_message_callback, sample_rate=16000, encoding="linear16"):
        """
        Connects asynchronously to Deepgram.
        `on_message_callback(json_dict)` will be fired whenever Deepgram returns an event.
        """
        params = {
            # Override with DEEPGRAM_MODEL / DEEPGRAM_LANGUAGE if needed (e.g. rollback to nova-2 + hi).
            "model": os.environ.get("DEEPGRAM_MODEL", "nova-3"),
            "language": os.environ.get("DEEPGRAM_LANGUAGE", "multi"),
            "encoding": encoding,
            "sample_rate": sample_rate,
            "channels": 1,
            "interim_results": "true",
            "smart_format": "true",
            "punctuate": "true",
            "endpointing": "100",  # ms; Deepgram recommends ~100 for code-switching
        }
        url = f"wss://api.deepgram.com/v1/listen?{urlencode(params)}"
        try:
            self.ws = await websockets.connect(
                url, 
                extra_headers={"Authorization": f"Token {self.api_key}"},
                open_timeout=10
            )
            # Spin up the background listener
            asyncio.create_task(self._receive_loop(on_message_callback))
            return True
        except Exception as e:
            print(f"[Deepgram Connection Error]: {e}")
            return False

    async def _receive_loop(self, callback):
        try:
            while self.ws and not self.ws.closed:
                raw = await self.ws.recv()
                if isinstance(raw, str):
                    payload = json.loads(raw)
                    callback(payload)
        except websockets.exceptions.ConnectionClosed:
            pass
        except Exception as e:
            print(f"[Deepgram Receive Error]: {e}")

    async def send_audio(self, audio_bytes: bytes):
        """Send chunk of raw audio byte data to Deepgram"""
        if self.ws and not self.ws.closed:
            await self.ws.send(audio_bytes)

    async def stop(self):
        if self.ws:
            # Deepgram may already have closed the socket (e.g. idle timeout); don't let that
            # raise into the caller's cleanup, which still has to save the CRM summary + call log.
            try:
                await self.ws.send(json.dumps({"type": "CloseStream"}))
                await asyncio.sleep(0.1) # allow time to close
                await self.ws.close()
            except Exception as e:
                print(f"[Deepgram Close Warning]: {e}")
