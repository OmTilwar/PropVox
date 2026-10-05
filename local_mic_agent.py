import asyncio
import base64
import json
import os
import sys
import threading
import time
from typing import Optional, List
import pyaudio
import websockets
from dotenv import load_dotenv
import io
import random
import re
from openai import AsyncOpenAI
from deepgram import DeepgramClient, LiveOptions, LiveTranscriptionEvents

# Attempt to load pydub for MP3 playback. If not present, we will gracefully print instructions.
try:
    from pydub import AudioSegment
    from pydub.utils import make_chunks
    PYDUB_AVAILABLE = True
except ImportError:
    PYDUB_AVAILABLE = False
    print("Warning: pydub is missing. MP3 playback (fillers & TTS) requires pydub.")
    print("Run: pip install pydub")

load_dotenv()

# --- Configuration ---
DEEPGRAM_API_KEY = os.environ.get("DEEPGRAM_API_KEY", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
SARVAM_API_KEY = os.environ.get("SARVAM_API_KEY", "")

# Hardware settings
SAMPLE_RATE = 16000
CHANNELS = 1
CHUNK_SIZE = 4096

# VAD / Logic settings
UTTERANCE_END_MS = "600" # Wait 600ms of silence before deciding user is done

# --- State & Queues ---
stt_queue = asyncio.Queue()       # Deepgram -> LLM
tts_queue = asyncio.Queue()       # LLM -> Sarvam TTS
audio_queue = asyncio.Queue()     # TTS -> Speaker

# Global stop event for Barge-in (interruptions)
# Set when user starts speaking; cleared when user stops.
stop_playback_event = threading.Event()

# Keep track of conversation history
conversation_history = [
    {
        "role": "system",
        "content": (
            "You are an AI voice agent for PropVox, an Indian real estate company. "
            "You are extremely brief, conversational, and polite. "
            "Talk in Hindi or English (Hinglish). "
            "Never use emojis or special characters, just plain text."
        )
    }
]

# Provide filler mapping. Prefer backend/filler_audio so backend-only deploys work.
_BASE_DIR = os.path.dirname(__file__)
_FILLER_DIR_BACKEND = os.path.join(_BASE_DIR, "backend", "filler_audio")
_FILLER_DIR_BENCHMARK = os.path.join(_BASE_DIR, "benchmark", "filler_audio")
FILLER_DIR = _FILLER_DIR_BACKEND if os.path.exists(_FILLER_DIR_BACKEND) else _FILLER_DIR_BENCHMARK
FILLER_FILES = {
    "affirmative": ["ack_haan_samajh_gayi.mp3", "ack_sahi_kaha.mp3", "ack_ji.mp3", "ack_bilkul.mp3", "ack_got_it.mp3", "ack_perfect.mp3"],
    "calculating": ["wait_one_sec.mp3", "wait_let_me_see.mp3", "wait_ek_minute.mp3", "wait_let_me_check.mp3"],
    "neutral": ["neutral_hmm.mp3", "neutral_achha.mp3", "neutral_theek.mp3", "neutral_okay.mp3", "neutral_right.mp3", "neutral_alright.mp3", "neutral_i_see.mp3"]
}

def load_filler_audio(filepath: str) -> Optional[bytes]:
    """Loads an MP3 filler file, converts to raw PCM 16kHz for PyAudio playback."""
    if not PYDUB_AVAILABLE or not os.path.exists(filepath):
        return None
    try:
        audio = AudioSegment.from_mp3(filepath)
        audio = audio.set_frame_rate(SAMPLE_RATE).set_channels(CHANNELS)
        return audio.raw_data
    except Exception as e:
        print(f"Failed to load filler {filepath}: {e}")
        return None

# Pre-cache fillers into memory
PRECACHED_FILLERS = {k: [load_filler_audio(os.path.join(FILLER_DIR, f)) for f in v] for k, v in FILLER_FILES.items()}

def get_random_filler(intent: str) -> Optional[bytes]:
    files = PRECACHED_FILLERS.get(intent, PRECACHED_FILLERS["neutral"])
    valid_files = [f for f in files if f is not None]
    if not valid_files:
        return None
    return random.choice(valid_files)

# --- Components ---

async def microphone_task(dg_conn):
    """Reads from mic and pushes bytes directly to Deepgram."""
    pa = pyaudio.PyAudio()
    stream = pa.open(format=pyaudio.paInt16, channels=CHANNELS,
                     rate=SAMPLE_RATE, input=True, frames_per_buffer=CHUNK_SIZE)
    print("\n🎤 Microphone active. Start speaking...")
    
    try:
        while True:
            data = stream.read(CHUNK_SIZE, exception_on_overflow=False)
            # Only send to Deepgram if we are not speaking to avoid echo loop
            # BUT for barge-in to work, we must listen always!
            # Note: Software echo cancellation is highly recommended here in prod.
            dg_conn.send(data)
            await asyncio.sleep(0.001)
    finally:
        stream.stop_stream()
        stream.close()
        pa.terminate()

def setup_deepgram_stt():
    client = DeepgramClient(DEEPGRAM_API_KEY)
    dg_conn = client.listen.live.v("1")

    def on_message(self, result, **kwargs):
        sentence = result.channel.alternatives[0].transcript.strip()
        speech_final = getattr(result, "speech_final", False)

        if not sentence:
            return

        # VAD Interruption Logic - User is speaking!
        if getattr(result, "is_final", False) is False:
            if not stop_playback_event.is_set():
                print("\n[VAD Trigger: Stopping AI Audio Playback (Barge-in!)]")
                stop_playback_event.set()

        # Transcript Final Logic
        if speech_final:
            print(f"\n🗣️ User: {sentence}")
            # The exact moment they stop speaking, push to LLM queue
            asyncio.run_coroutine_threadsafe(stt_queue.put(sentence), asyncio.get_running_loop())
            
            # Heuristics for Filler Words (Illusionist Hack)
            lower_s = sentence.lower()
            if any(w in lower_s for w in ["yes", "samajh", "haan", "agree"]):
                filler = get_random_filler("affirmative")
            elif any(w in lower_s for w in ["how", "what", "where", "kyu", "kahan"]):
                filler = get_random_filler("calculating")
            else:
                filler = get_random_filler("neutral")
            
            if filler:
                # Instantly play the filler sound BEFORE the LLM even answers!
                asyncio.run_coroutine_threadsafe(audio_queue.put(filler), asyncio.get_running_loop())

    dg_conn.on(LiveTranscriptionEvents.Transcript, on_message)
    options = LiveOptions(
        model="nova-2",
        language="hi", # Allows Hinglish
        encoding="linear16",
        sample_rate=SAMPLE_RATE,
        channels=CHANNELS,
        interim_results=True,
        utterance_end_ms=UTTERANCE_END_MS,
        vad_events=True,
    )
    if not dg_conn.start(options):
        print("❌ Failed to start Deepgram STT.")
        sys.exit(1)
    
    return dg_conn

async def groq_llm_task():
    """Reads STT queue, fetches LLM streaming response, chunks to sentences, pushes to TTS."""
    client = AsyncOpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1")
    
    while True:
        user_text = await stt_queue.get()
        conversation_history.append({"role": "user", "content": user_text})
        
        # Clear any stop event since we are about to start responding
        stop_playback_event.clear()
        
        print("\n🧠 Groq: Generating...", end="", flush=True)

        try:
            stream = await client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=conversation_history,
                stream=True,
                max_tokens=250,
                temperature=0.7
            )

            current_sentence = ""
            full_response = ""
            
            async for chunk in stream:
                if stop_playback_event.is_set():
                    # User interrupted! Abort generation early to save tokens.
                    print(" [Interrupted!]")
                    break

                if not chunk.choices:
                    continue
                
                delta = chunk.choices[0].delta.content
                if delta:
                    current_sentence += delta
                    full_response += delta
                    
                    # Sentence Chunking: flush on punctuation
                    if any(p in current_sentence for p in ['.', '?', '!', '।']):
                        # Found a boundary. Clean it.
                        clean_phrase = current_sentence.strip()
                        if clean_phrase:
                            # Send this phrase immediately to TTS!
                            await tts_queue.put(clean_phrase)
                        current_sentence = ""
            
            # Flush whatever is left
            if current_sentence.strip() and not stop_playback_event.is_set():
                await tts_queue.put(current_sentence.strip())

            # Add to history
            if full_response:
                conversation_history.append({"role": "assistant", "content": full_response.strip()})
                print(f" -> Done. ({len(full_response)} chars)")

        except Exception as e:
            print(f"LLM Error: {e}")

async def sarvam_tts_task():
    """Reads TTS string chunks, sends to Sarvam, pushes audio bytes to Audio queue."""
    url = "wss://api.sarvam.ai/text-to-speech/ws?model=bulbul:v3&send_completion_event=true"
    
    while True:
        phrase = await tts_queue.get()
        if not phrase or stop_playback_event.is_set():
            continue

        try:
            async with websockets.connect(
                url,
                extra_headers={"api-subscription-key": SARVAM_API_KEY},
                open_timeout=5,
            ) as ws:
                # Send config (Same as filler format to ensure match)
                await ws.send(json.dumps({
                    "type": "config",
                    "data": {
                        "target_language_code": "hi-IN",
                        "speaker": "simran",
                        "speech_sample_rate": SAMPLE_RATE,
                        "pace": 1.1,
                        "enable_preprocessing": True,
                        "output_audio_codec": "mp3", 
                    }
                }))

                await ws.send(json.dumps({"type": "text", "data": {"text": phrase}}))
                await ws.send(json.dumps({"type": "flush"}))

                audio_buffer = io.BytesIO()
                
                # Receive loop
                while not stop_playback_event.is_set():
                    raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
                    if isinstance(raw, bytes):
                        audio_buffer.write(raw)
                    else:
                        payload = json.loads(raw)
                        msg_type = payload.get("type", "")
                        if msg_type == "audio":
                            b64 = payload.get("data", {}).get("audio", "")
                            if b64:
                                audio_buffer.write(base64.b64decode(b64))
                        elif msg_type == "event" and payload.get("data", {}).get("event_type", "") == "final":
                            break
                        elif msg_type == "error":
                            print(f"[TTS Error] {payload.get('data', {}).get('message', '')}")
                            break
                
                # Audio is in MP3 format inside audio_buffer. We must convert it to PCM for PyAudio.
                if not stop_playback_event.is_set() and audio_buffer.tell() > 0 and PYDUB_AVAILABLE:
                    audio_buffer.seek(0)
                    try:
                        segment = AudioSegment.from_mp3(audio_buffer)
                        segment = segment.set_frame_rate(SAMPLE_RATE).set_channels(CHANNELS)
                        pcm_bytes = segment.raw_data
                        
                        # Send PCM bytes to speaker
                        await audio_queue.put(pcm_bytes)
                        print(f" 🔊 TTS Queued: '{phrase}'")
                    except Exception as e:
                        print(f"Failed to decode MP3 stream: {e}")

        except asyncio.TimeoutError:
             print("TTS Timeout.")
        except Exception as e:
             print(f"TTS Exception: {e}")

def speaker_playback_task():
    """Runs securely in a thread. Dequeues PCM bytes and plays out loud."""
    pa = pyaudio.PyAudio()
    stream = pa.open(format=pyaudio.paInt16, channels=CHANNELS,
                     rate=SAMPLE_RATE, output=True)
    
    print("🔊 Speaker active.")
    
    # We use a non-async loop here since PyAudio blocking writes are safer in standard threads
    while True:
        if stop_playback_event.is_set():
            # Barge-in: immediately clear all pending audio chunks!
            while not audio_queue.empty():
                try:
                    audio_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            # Give short sleep avoiding 100% CPU lock while interrupted
            time.sleep(0.05)
            continue
        
        # Pull audio off the queue
        try:
            # We use a threadsafe check on the async queue using run_coroutine_threadsafe, 
            # Or just check emptiness. Using simple non-blocking grab is better.
            pcm_bytes = None
            try:
                pcm_bytes = audio_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            
            if pcm_bytes and not stop_playback_event.is_set():
                # Stream out bytes in tiny 1024-byte chunks so we can interrupt it MID-WORD
                for i in range(0, len(pcm_bytes), 1024):
                    if stop_playback_event.is_set():
                        print(" [Cut off audio mid-playback]")
                        break # Interrupt!!
                    chunk = pcm_bytes[i:i+1024]
                    stream.write(chunk)
            
            time.sleep(0.01)
        except Exception as e:
            print(f"Speaker Error: {e}")
            time.sleep(0.1)

async def main():
    if not PYDUB_AVAILABLE:
        print("Cannot start. Please install pydub and ensure ffmpeg is available on your PATH.")
        return

    print("--- Starting PropVox Local Mic Agent ---")
    
    # 1. Start STT (Deepgram SDK handles its own WS thread)
    dg_conn = setup_deepgram_stt()

    # 2. Start independent speaker output thread
    t_speaker = threading.Thread(target=speaker_playback_task, daemon=True)
    t_speaker.start()

    # 3. Start async tasks
    tasks = [
        asyncio.create_task(microphone_task(dg_conn)),
        asyncio.create_task(groq_llm_task()),
        asyncio.create_task(sarvam_tts_task()),
    ]

    try:
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        print("Shutting down...")
    finally:
        dg_conn.finish()


if __name__ == "__main__":
    asyncio.run(main())
