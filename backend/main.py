import builtins
import sys
from datetime import datetime

_original_print = builtins.print

def ts_print(*args, **kwargs):
    now = datetime.now()
    ms = int(now.microsecond / 10000)
    ts = f"[{now.strftime('%H:%M:%S')}.{ms:02d}]"
    # Line-buffer for hosted logs (Render, etc.); callers can pass flush=False to opt out.
    kwargs.setdefault("flush", True)
    out = kwargs.get("file", sys.stdout)

    if args and isinstance(args[0], str) and args[0].startswith("\n"):
        new_first = "\n" + ts + " " + args[0][1:]
        _original_print(new_first, *args[1:], **kwargs)
    else:
        _original_print(ts, *args, **kwargs)
    try:
        out.flush()
    except Exception:
        pass

builtins.print = ts_print

# Native Render builds (non-Docker) may not inherit PYTHONUNBUFFERED; force line mode on text streams.
# Windows consoles often default to cp1252; emoji/log lines would crash without UTF-8.
_kw = dict(line_buffering=True)
if sys.platform == "win32":
    _kw.update(encoding="utf-8", errors="replace")
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(**_kw)
    except (OSError, ValueError, AttributeError, TypeError):
        try:
            sys.stdout.reconfigure(line_buffering=True)
        except (OSError, ValueError, AttributeError):
            pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(**_kw)
    except (OSError, ValueError, AttributeError, TypeError):
        try:
            sys.stderr.reconfigure(line_buffering=True)
        except (OSError, ValueError, AttributeError):
            pass

import os
import re
import time
import json
import base64
import asyncio
import websockets
from concurrent.futures import ThreadPoolExecutor
from pydub import AudioSegment
from dotenv import load_dotenv

# Python 3.13+ removes stdlib audioop; the audioop-lts package provides the same `audioop` module.
import audioop

import crm # Local Customer Database
from conversation_log import LOG_DIR, save_call_log
from stt import DeepgramSTTLayer
from llm import GroqLLMLayer
from tts import SarvamTTSLayer
from tzutil import IST

load_dotenv()

# --- 1. Pre-Load Filler Audios in Memory as Raw Twilio MULAW ---
FILLERS = {}
FILLERS_EN = {}
FILLERS_HI = {}
_BASE_DIR = os.path.dirname(__file__)
FILLER_DIR_BACKEND = os.path.abspath(os.path.join(_BASE_DIR, "filler_audio"))
FILLER_DIR_BENCHMARK = os.path.abspath(os.path.join(_BASE_DIR, "..", "benchmark", "filler_audio"))

_LATIN_HINDI_RE = re.compile(
    r"\b("
    r"haan|haanji|hanji|nahi|nahin|naa|nhi|"
    r"theek|thik|accha|acha|achha|"
    r"aap|kya|kab|kahan|kaha|kyun|kaise|kaisi|"
    r"hai|ho|hoon|hain|hum|mein|maine|"
    r"aaj|kal|bas|matlab|bata|batao|bataiye|"
    r"meri|mera|mere|samajh|"
    r"shukriya|dhanyavaad|dhanyawad|alvida|"
    r"koi|kabhi|ji"
    r")\b",
    re.I,
)


def infer_turn_language(text: str) -> str:
    """
    Returns "english" or "hinglish" for the current user utterance.
    We use this to choose the correct filler audio set.
    """
    if not text:
        return "english"
    s = str(text)
    if re.search(r"[\u0900-\u097F]", s):
        return "hinglish"
    if _LATIN_HINDI_RE.search(s):
        return "hinglish"
    return "english"


def _decode_mp3_to_ulaw(path: str) -> bytes:
    audio = AudioSegment.from_file(path, format="mp3")
    audio = audio.set_frame_rate(8000).set_channels(1).set_sample_width(2)
    return audioop.lin2ulaw(audio.raw_data, 2)


def _decode_mp3_dir(dir_path: str) -> dict:
    """Decode all mp3 files in `dir_path` to {"[stem]": μ-law bytes}.
    Each decode is an ffmpeg subprocess, so a thread pool cuts startup time several-fold."""
    files = sorted(f for f in os.listdir(dir_path) if f.endswith(".mp3"))
    paths = [os.path.join(dir_path, f) for f in files]
    with ThreadPoolExecutor(max_workers=min(8, (os.cpu_count() or 2) * 2)) as pool:
        decoded = list(pool.map(_decode_mp3_to_ulaw, paths))
    return {f"[{f[:-4]}]": raw for f, raw in zip(files, decoded)}


def _load_mp3_dir_to_dict(target_dict: dict, dir_path: str) -> None:
    """Load all mp3 files in `dir_path` into `target_dict` as μ-law bytes."""
    target_dict.update(_decode_mp3_dir(dir_path))


def init_fillers():
    """Load every `*.mp3`; prefer backend/filler_audio for deployment."""
    filler_dir = FILLER_DIR_BACKEND if os.path.exists(FILLER_DIR_BACKEND) else FILLER_DIR_BENCHMARK
    print(f"Loading pre-generated fillers from: {filler_dir}")
    if not os.path.exists(filler_dir):
        print(
            "Warning: Filler audio directory not found! "
            "Expected backend/filler_audio (preferred) or benchmark/filler_audio (legacy)."
        )
        return

    # Optional folder layout:
    #   backend/filler_audio/english/*.mp3
    #   backend/filler_audio/hindi/*.mp3
    # If not present, we fall back to filename-based classification using "hindi" in the stem.
    en_dir = os.path.join(filler_dir, "english")
    hi_dir = os.path.join(filler_dir, "hindi")

    FILLERS.clear()
    FILLERS_EN.clear()
    FILLERS_HI.clear()

    if os.path.isdir(en_dir) or os.path.isdir(hi_dir):
        if os.path.isdir(en_dir):
            _load_mp3_dir_to_dict(FILLERS_EN, en_dir)
        if os.path.isdir(hi_dir):
            _load_mp3_dir_to_dict(FILLERS_HI, hi_dir)

        # If mp3s exist directly under filler_dir (legacy/common placement),
        # treat non-hindi stems as common (allowed in both buckets).
        root_mp3_dir = filler_dir
        for key, raw_ulaw in _decode_mp3_dir(root_mp3_dir).items():
            if "hindi" in key.lower():
                FILLERS_HI[key] = raw_ulaw
            else:
                FILLERS_EN[key] = raw_ulaw
                FILLERS_HI[key] = raw_ulaw

        # Union for playback safety / logging.
        FILLERS.update(FILLERS_EN)
        FILLERS.update(FILLERS_HI)
    else:
        # Legacy layout: all mp3s in one directory.
        tmp_all = {}
        _load_mp3_dir_to_dict(tmp_all, filler_dir)
        for key, raw_ulaw in tmp_all.items():
            # Example stems: ack_hindi_..., friendly_hindi_...
            stem = key.strip("[]")
            if "hindi" in stem.lower():
                FILLERS_HI[key] = raw_ulaw
            else:
                # Common acknowledgements/pause tokens are useful in both languages.
                FILLERS_EN[key] = raw_ulaw
                FILLERS_HI[key] = raw_ulaw
        FILLERS.update(FILLERS_EN)
        FILLERS.update(FILLERS_HI)

    # Ensure both buckets are non-empty so prompting & fallback never break.
    if not FILLERS_EN and FILLERS:
        FILLERS_EN.update(FILLERS)
    if not FILLERS_HI and FILLERS:
        FILLERS_HI.update(FILLERS)

    print(
        "Loaded fillers by language: "
        f"EN={len(FILLERS_EN)} HI={len(FILLERS_HI)} TOTAL={len(FILLERS)}"
    )

init_fillers()

# Sarvam output rate; resampled to Twilio's 8 kHz μ-law in-process.
TTS_SAMPLE_RATE = int(os.environ.get("TTS_SAMPLE_RATE", "16000"))

# Sentence ends: ? ! । | newline anywhere; "." only when followed by whitespace (keeps "2.5" intact).
_SENTENCE_BREAK_RE = re.compile(r"[?!।|\n]|\.(?=\s)")
# Clause ends, used only for the first chunk of a reply so the caller hears audio sooner.
_CLAUSE_BREAK_RE = re.compile(r"[,;:—]\s")
FIRST_CLAUSE_MIN_CHARS = 40


def _last_sentence_break(text: str, allow_clause: bool = False) -> int:
    """Index just past the last speakable boundary in `text`, or 0 if none yet."""
    cut = 0
    for m in _SENTENCE_BREAK_RE.finditer(text):
        cut = m.end()
    if not cut and allow_clause and len(text) >= FIRST_CLAUSE_MIN_CHARS:
        for m in _CLAUSE_BREAK_RE.finditer(text):
            cut = m.end()
    return cut


# --- 2. Twilio Connection Handler ---
async def twilio_handler(websocket, path):
    # First line per call — if this never appears in Render logs, Twilio is not opening WSS to this host.
    print("\n📞 [Twilio] Call Connected!")
    try:
        peer = websocket.remote_address
        print(f"   (peer {peer} path={path!r})")
    except Exception:
        pass
    
    stt = DeepgramSTTLayer()
    tts = SarvamTTSLayer()
    llm = None # Will safely hook into caller context dynamically

    stream_sid = None
    caller_phone = None
    call_started_at = None
    crm_at_connect = None
    llm_generator_task = None
    tts_worker_task = None
    tts_queue = asyncio.Queue()  # (turn_id, sentence, audio_q, synth_task) in playback order
    synth_tasks = set()

    is_agent_speaking = False
    turn_id = 0          # bumped on every barge-in / new turn; stale audio is dropped by id
    final_parts = []     # is_final STT segments of the utterance in progress

    async def send_media(mulaw: bytes):
        await websocket.send(json.dumps({
            "event": "media",
            "streamSid": stream_sid,
            "media": {"payload": base64.b64encode(mulaw).decode("utf-8")}
        }))

    async def synthesize(sentence, audio_q):
        """Fetch TTS for one sentence into audio_q as 8 kHz μ-law; None marks the end."""
        t0_ttfb = time.perf_counter()
        header_left = 44  # WAV header bytes still to strip (may span chunks)
        carry = b""       # odd trailing byte so ratecv always sees whole 16-bit frames
        state = None
        first_audio = True
        try:
            async for chunk in tts.speak(sentence, sample_rate=TTS_SAMPLE_RATE, output_codec="wav"):
                if header_left:
                    skip = min(header_left, len(chunk))
                    chunk, header_left = chunk[skip:], header_left - skip
                chunk = carry + chunk
                if len(chunk) % 2:
                    chunk, carry = chunk[:-1], chunk[-1:]
                else:
                    carry = b""
                if not chunk:
                    continue
                if first_audio:
                    first_audio = False
                    ttfb_ms = (time.perf_counter() - t0_ttfb) * 1000
                    print(f"   ⏱️ [TTS Latency | TTFB]: {ttfb_ms:.0f} ms payload arriving for -> '{sentence[:30]}...'")
                pcm_8k, state = audioop.ratecv(chunk, 2, 1, TTS_SAMPLE_RATE, 8000, state)
                audio_q.put_nowait(audioop.lin2ulaw(pcm_8k, 2))
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[TTS Stream Error]: {e}")
        finally:
            audio_q.put_nowait(None)

    def enqueue_sentence(tid, sentence):
        """Start synthesis immediately (in parallel with earlier sentences); playback stays in order."""
        print(f"[Agent -> TTS Queue]: {sentence}")
        audio_q = asyncio.Queue()
        task = asyncio.create_task(synthesize(sentence, audio_q))
        synth_tasks.add(task)
        task.add_done_callback(synth_tasks.discard)
        tts_queue.put_nowait((tid, sentence, audio_q, task))

    async def tts_worker():
        """Streams synthesized sentences to Twilio one after another to prevent overlapping."""
        while True:
            tid, sentence, audio_q, task = await tts_queue.get()
            try:
                # If a barge-in happened while this sentence waited, drop it instead of playing stale audio.
                while tid == turn_id:
                    mulaw = await audio_q.get()
                    if mulaw is None or tid != turn_id:
                        break
                    await send_media(mulaw)
                if tid != turn_id:
                    task.cancel()
            except Exception as e:
                print(f"[TTS Playback Error]: {e}")
            finally:
                tts_queue.task_done()

    # If model omits a leading [token], play default filler once to mask pipeline latency (chars before we give up).
    FILLER_FALLBACK_AFTER_CHARS = 20
    FILLER_GIVEUP_AFTER_CHARS = 36

    async def run_agent_turn(user_transcript, turn_start_time, tid):
        """Pipes User Text -> LLM Stream -> Filler Filter -> Queue -> Twilio"""
        print(f"\n[User]: {user_transcript}")
        turn_lang = infer_turn_language(user_transcript)
        allowed_fillers = (FILLERS_EN if turn_lang == "english" else FILLERS_HI) or FILLERS
        default_key = sorted(allowed_fillers.keys())[0] if allowed_fillers else None
        llm_stream = llm.generate_response_stream(user_transcript)

        buffer = ""
        detecting_filler = True
        filler_fallback_used = False
        first_sentence_sent = False
        ttft_ms = None

        async for chunk in llm_stream:
            if tid != turn_id:
                break # Agent was interrupted while thinking

            if ttft_ms is None:
                ttft_ms = (time.perf_counter() - turn_start_time) * 1000
                print(f"   ⏱️ [LLM Latency | TTFT]: {ttft_ms:.0f} ms")

            buffer += chunk

            # Step A: First token must be a known [filler] — stream pre-recorded μ-law immediately
            if detecting_filler:
                start = buffer.find("[")
                end = buffer.find("]", start + 1) if start != -1 else -1
                if end != -1:
                    key = buffer[start:end+1] # e.g. "[neutral_hmm]"

                    if key in FILLERS:
                        reaction_ms = (time.perf_counter() - turn_start_time) * 1000
                        if key in allowed_fillers:
                            chosen_key = key
                        else:
                            # Model produced a filler from the other language bucket.
                            # To avoid Hindi fillers on English turns, play the allowed default instead.
                            chosen_key = default_key
                            print(
                                f"   ⚠️ [Filler lang mismatch]: {key} not in {turn_lang} set; "
                                f"playing {chosen_key} instead."
                            )
                        print(
                            f"   🪄 [Filler Detected at {reaction_ms:.0f} ms]: {chosen_key} -> Streaming ZERO-LATENCY payload."
                        )
                        await send_media(allowed_fillers[chosen_key])
                    else:
                        print(f"   ⚠️ [Unknown filler token]: {key} (no MP3) — continuing to TTS.")

                    detecting_filler = False
                    buffer = buffer[end+1:].lstrip()
                elif (
                    default_key
                    and not filler_fallback_used
                    and start == -1
                    and len(buffer) > FILLER_FALLBACK_AFTER_CHARS
                ):
                    filler_fallback_used = True
                    reaction_ms = (time.perf_counter() - turn_start_time) * 1000
                    print(f"   🪄 [Filler fallback at {reaction_ms:.0f} ms]: model omitted token — playing {default_key}")
                    await send_media(allowed_fillers[default_key])
                    detecting_filler = False
                elif len(buffer) > FILLER_GIVEUP_AFTER_CHARS and start == -1:
                    detecting_filler = False

            # Step B: Cut complete sentences off the buffer and start their TTS right away
            if not detecting_filler:
                cut = _last_sentence_break(buffer, allow_clause=not first_sentence_sent)
                if cut:
                    sentence = buffer[:cut].strip()
                    buffer = buffer[cut:]
                    if sentence:
                        enqueue_sentence(tid, sentence)
                        first_sentence_sent = True

        if buffer.strip() and not detecting_filler and tid == turn_id:
            enqueue_sentence(tid, buffer.strip())

    def interrupt_agent():
        """Barge-in: stop LLM + TTS for the current turn and flush audio already buffered at Twilio."""
        nonlocal is_agent_speaking, turn_id
        is_agent_speaking = False
        turn_id += 1
        if llm_generator_task and not llm_generator_task.done():
            llm_generator_task.cancel()
        for task in list(synth_tasks):
            task.cancel()
        if stream_sid:
            asyncio.create_task(websocket.send(json.dumps({
                "event": "clear",
                "streamSid": stream_sid
            })))

    def on_stt_event(payload):
        nonlocal is_agent_speaking, llm_generator_task, turn_id

        is_final = payload.get("is_final", False)
        speech_final = payload.get("speech_final", False)
        transcript = ""

        try:
            transcript = payload["channel"]["alternatives"][0]["transcript"]
        except (KeyError, IndexError):
            pass

        # Deepgram splits long utterances into several is_final segments; only the last one carries
        # speech_final. Collect them all so the LLM sees the whole utterance, not just its tail.
        if is_final and transcript:
            final_parts.append(transcript)

        if transcript:
            # INTERRUPT TRIGGER (BARGE-IN)
            # Also fires when a new final utterance lands while the previous turn is still running,
            # otherwise two LLM streams would feed the TTS queue (and the shared history) at once.
            previous_turn_running = llm_generator_task is not None and not llm_generator_task.done()
            if is_agent_speaking and (not speech_final or previous_turn_running):
                print("\n🛑 [Barge-in detected] User is speaking! Cancelling TTS.")
                interrupt_agent()

        # END OF SPEECH TURN TRIGGER
        if speech_final and final_parts:
            utterance = " ".join(final_parts)
            final_parts.clear()
            tts.prewarm()  # open the TTS socket while the LLM is thinking
            is_agent_speaking = True
            turn_id += 1
            turn_start_time = time.perf_counter()
            llm_generator_task = asyncio.create_task(run_agent_turn(utterance, turn_start_time, turn_id))

    # --- Start Twilio Listen Loop ---
    try:
        tts_worker_task = asyncio.create_task(tts_worker())
        
        async for message in websocket:
            data = json.loads(message)
            event = data.get("event")
            
            if event == "start":
                stream_sid = data["start"]["streamSid"]
                
                # CRM IDENTITY INJECTION
                custom_params = data["start"].get("customParameters", {})
                caller_phone = custom_params.get("PhoneNumber")
                print(f"📡 Stream Started: {stream_sid} | Caller Identity: {caller_phone}")
                
                customer_context = crm.get_customer_context(caller_phone)
                crm_at_connect = customer_context
                call_started_at = datetime.now(IST).isoformat()
                if customer_context:
                    last_call = customer_context.get('last_call_dt', 'unknown')
                    summary_preview = customer_context.get('summary', '')[:120]
                    print(f"\n📂 [CRM] Returning customer | Last call: {last_call}\n    Profile: {summary_preview}...\n")
                else:
                    print("📂 [CRM] New caller. No previous context found.")
                    
                # Initialize LLM dynamically with the specific caller's CRM memory
                llm = GroqLLMLayer(
                    customer_context=customer_context,
                    filler_keys_english=sorted(FILLERS_EN.keys()),
                    filler_keys_hinglish=sorted(FILLERS_HI.keys()),
                )

                t0_stt = time.perf_counter()
                success = await stt.connect(on_stt_event, sample_rate=8000, encoding="mulaw")
                if not success:
                    print("❌ FAILED to link STT")
                else:
                    print(f"   ⏱️ [STT Connection Latency]: {(time.perf_counter() - t0_stt) * 1000:.0f} ms")
                # Caller speaks first; Myra replies only after first speech_final from STT (see on_stt_event).
                print("   👂 Waiting for caller to speak first — Myra will respond after their first words.")
                # is_agent_speaking stays False until the user finishes a turn and we run the agent.

            elif event == "media":
                chunk = base64.b64decode(data["media"]["payload"])
                await stt.send_audio(chunk)

            elif event == "stop":
                print("\n🛑 Call Stopped by Twilio.")
                break
                
    except websockets.exceptions.ConnectionClosed:
        print("\n🛑 Twilio WebSocket Disconnected.")
    finally:
        await stt.stop()
        if tts_worker_task:
            tts_worker_task.cancel()
        for task in list(synth_tasks):
            task.cancel()
        await tts.close()

        merged_summary = None
        # CRM merge (must not block saving the transcript below)
        if llm and caller_phone:
            try:
                print(f"📝 [CRM] Call ended. Generating merged post-call summary for {caller_phone}...")
                old_record = crm.get_customer_context(caller_phone)
                prev_summary = None
                prev_last_dt = None
                if old_record:
                    if isinstance(old_record, dict):
                        prev_summary = old_record.get("summary")
                        prev_last_dt = old_record.get("last_call_dt")
                    else:
                        prev_summary = old_record
                merged_summary = await llm.generate_summary(
                    previous_crm_summary=prev_summary,
                    previous_last_call_dt=prev_last_dt,
                )
                if merged_summary:
                    crm.save_call_summary(caller_phone, merged_summary, call_dt=datetime.now(IST))
            except Exception as e:
                print(f"⚠️ [CRM] Post-call summary failed (conversation log will still save): {e}")

        # Always write JSON if we had an LLM session (even if CRM step failed or PhoneNumber was missing)
        if llm:
            try:
                save_call_log(
                    llm,
                    caller_phone,
                    stream_sid=stream_sid,
                    crm_at_connect=crm_at_connect,
                    merged_summary=merged_summary,
                    call_started_at=call_started_at,
                )
            except Exception as e:
                print(f"⚠️ [Conversation log] Failed to save JSON: {e}")

# --- 3. Start Websocket Server ---
async def main():
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "5050"))
    print(f"🚀 Starting PropVox Voice Agent Server on ws://{host}:{port}")
    print(f"📁 Conversation JSON logs: {LOG_DIR.resolve()}")
    ext = (os.environ.get("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
    if ext:
        wss = ext.replace("https://", "wss://", 1).replace("http://", "wss://", 1)
        print(
            f"🌐 Twilio <Stream> url should be: {wss} "
            f"(set VOICE_AGENT_WSS_URL / TwiML to this; no path required)"
        )
    server = await websockets.serve(twilio_handler, host, port)
    await server.wait_closed()

if __name__ == "__main__":
    asyncio.run(main())
