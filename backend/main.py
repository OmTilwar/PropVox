import os
from dotenv import load_dotenv
load_dotenv(override=True)

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from modules.speech_to_text import stt_engine
from modules.conversation_engine import conv_engine
from modules.text_to_speech import tts_engine
from modules.memory_manager import memory
import json
import base64
import asyncio
import time
import re
from datetime import datetime

app = FastAPI(title="Riverwood AI Voice Agent")

import os
_raw_origins = os.environ.get("ALLOWED_ORIGINS", "*")
_allowed_origins = [o.strip() for o in _raw_origins.split(",")] if _raw_origins != "*" else ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.websocket("/ws/voice")
async def websocket_endpoint(websocket: WebSocket, language: str = "en"):
    await websocket.accept()
    session_id = str(id(websocket))
    
    loop = asyncio.get_running_loop()
    
    dg_socket = None
    dg_session_active = False   # True while user mic is open (audio_start → audio_stop)
    user_text_buffer = ""
    stt_start_time = 0
    current_processing_task = None
    volatile_user_text = ""
    interrupt_triggered_for_current_utterance = False
    interrupt_timeout_task = None
    last_word_timestamp = None
    last_query_cache = ""
    last_response_cache = ""
    last_audio_cache: list = []
    last_task_cancel_time: float = 0.0  # Debounce: timestamp of last task cancellation

    def _handle_dg_close():
        """Fired by Deepgram's Close event — nulls dg_socket to stop the send() flood."""
        nonlocal dg_socket
        def _null():
            nonlocal dg_socket
            dg_socket = None
            print("[Deepgram] 🔴 Connection lost — socket cleared to stop send() flood.")
        loop.call_soon_threadsafe(_null)

    def handle_transcript(transcript: str, is_final: bool, speech_final: bool):
        nonlocal user_text_buffer, current_processing_task, volatile_user_text, interrupt_triggered_for_current_utterance, interrupt_timeout_task, last_word_timestamp, last_task_cancel_time
        
        # 1. As soon as the user says their first 2 letters, mark that they started speaking
        if len(transcript.strip()) > 1 and not interrupt_triggered_for_current_utterance:
            interrupt_triggered_for_current_utterance = True
            current_time_str = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            print(f"[{current_time_str}] 🎙️ User Started Speaking...")
            
            # TRIGGER BARGE-IN INTERRUPT *ONLY* IF AI IS CURRENTLY TALKING
            if current_processing_task and not current_processing_task.done():
                print(f"[{current_time_str}] 🔴 AI Interrupted by User: '{transcript.strip()}'...")
                
                # Send silence command to frontend instantly
                def _send_interrupt_to_frontend():
                    loop.create_task(websocket.send_text(json.dumps({"type": "interrupt"})))
                loop.call_soon_threadsafe(_send_interrupt_to_frontend)

                def _cancel_task():
                    nonlocal current_processing_task, last_task_cancel_time
                    if current_processing_task and not current_processing_task.done():
                        current_processing_task.cancel()
                        current_processing_task = None
                        last_task_cancel_time = time.time()
                loop.call_soon_threadsafe(_cancel_task)
                
                if volatile_user_text:
                    # Context Clubbing: Merge previous sentence with new interruption
                    memory.pop_last_message(session_id, role="assistant")
                    user_text_buffer = volatile_user_text + " "
                    volatile_user_text = ""

        # ── LIVE DISPLAY: forward every Deepgram result to frontend immediately ───────────
        # Sends even non-final results so the user sees words as they speak.
        # The LLM buffer is NOT updated here — only on is_final below.
        if transcript:
            live_text = (user_text_buffer + " " + transcript).strip()
            def _send_live(txt):
                loop.create_task(websocket.send_text(json.dumps({
                    "type": "transcript",
                    "subtype": "live",
                    "role": "user",
                    "text": txt
                })))
            loop.call_soon_threadsafe(_send_live, live_text)

        # 2. Lock in finalized chunks (updates LLM buffer)
        if is_final:
            last_word_timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            user_text_buffer += transcript + " "

            # --- 0.5s FORCE TIMEOUT LOGIC (above Deepgram's 0.3s threshold to avoid race condition) ---
            def _reset_fallback_timer():
                nonlocal interrupt_timeout_task
                if interrupt_timeout_task and not interrupt_timeout_task.done():
                    interrupt_timeout_task.cancel()
                    
                async def fallback_trigger():
                    await asyncio.sleep(0.3)   # hung timeout (was 0.5s) — fires if speech_final delayed
                    # If 0.3s passes without new words or speech_final, force process
                    nonlocal user_text_buffer, volatile_user_text, current_processing_task, interrupt_triggered_for_current_utterance
                    
                    # ── GUARD: bail out if speech_final already started a task ──
                    # Task.cancel() cannot stop code after sleep() completes — so we
                    # check explicitly here to prevent a duplicate LLM call.
                    if current_processing_task and not current_processing_task.done():
                        return
                    
                    final_user_text = user_text_buffer.strip()
                    if len(final_user_text) > 1:  # accept 2+ char words ("no", "ok", "हाँ")
                        current_time_str = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                        print(f"[{current_time_str}] ⚠️ Deepgram Hung (0.5s Timeout) - Forcing Query.")
                        print(f"[{current_time_str}] 🎙️ User Finished Speaking. (Last word at {last_word_timestamp})")
                        print(f"[{current_time_str}] 🟢 Query Processing: '{final_user_text}'")
                        
                        volatile_user_text = final_user_text
                        user_text_buffer = ""
                        interrupt_triggered_for_current_utterance = False
                        
                        interaction_time = time.time()
                        current_processing_task = loop.create_task(process_llm_and_tts(final_user_text, interaction_time))
                    else:
                        user_text_buffer = ""
                        interrupt_triggered_for_current_utterance = False
                        
                interrupt_timeout_task = loop.create_task(fallback_trigger())
                
            loop.call_soon_threadsafe(_reset_fallback_timer)

        # 3. Pause detected - fire LLM if there's actual content
        if speech_final and user_text_buffer.strip():
            
            # Cancel the fallback timer since Deepgram fired naturally
            def _cancel_timer():
                nonlocal interrupt_timeout_task
                if interrupt_timeout_task and not interrupt_timeout_task.done():
                    interrupt_timeout_task.cancel()
            loop.call_soon_threadsafe(_cancel_timer)
            
            final_user_text = user_text_buffer.strip()
            
            # Ignore ghost audio/noise (single character only)
            if len(final_user_text) > 1:  # accept 2+ char words ("no", "ok", "हाँ")
                current_time_str = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                print(f"[{current_time_str}] 🎙️ STT Finalized & User Finished Speaking. (Last word at {last_word_timestamp})")
                
                volatile_user_text = final_user_text
                user_text_buffer = ""
                interrupt_triggered_for_current_utterance = False
                
                # Start LLM pipeline — only if not already running (fallback timer may have beaten us)
                interaction_time = time.time()
                
                def _start_llm(txt, it):
                    nonlocal current_processing_task
                    # Guard: bail out if fallback timer already started a task
                    if current_processing_task and not current_processing_task.done():
                        return
                    current_processing_task = loop.create_task(process_llm_and_tts(txt, it))
                    
                loop.call_soon_threadsafe(_start_llm, final_user_text, interaction_time)
            else:
                # Discard ghost query
                user_text_buffer = ""
                interrupt_triggered_for_current_utterance = False

    async def process_llm_and_tts(user_text, interaction_start_time=0.0):
        nonlocal last_query_cache, last_response_cache, last_audio_cache
        current_time_str = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        print(f"[{current_time_str}] 🧠 LLM Query Inserted: '{user_text}'")
        try:
            # 1. Provide User Transcript Feedback (final — replaces the interim bubble)
            await websocket.send_text(json.dumps({
                "type": "transcript",
                "subtype": "final",
                "role": "user",
                "text": user_text
            }))

            # ── QUERY DEDUPLICATION — replay cache if same query ────────────────────────
            if user_text.strip() == last_query_cache.strip() and last_audio_cache:
                current_time_str = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                print(f"[{current_time_str}] ♻️ Same query — replaying full audio cache (0 LLM + 0 TTS cost).")
                first_frame = True
                for cached_bytes in last_audio_cache:
                    if cached_bytes:
                        if first_frame:
                            effective = time.time() - interaction_start_time
                            ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                            print(f"[{ts}] ⚡ Audio Cache TTFB: in {effective:.3f}s (pure WebSocket speed)")
                            first_frame = False
                        await websocket.send_bytes(cached_bytes)
                        await asyncio.sleep(0.01)
                await websocket.send_text(json.dumps({
                    "type": "transcript",
                    "role": "assistant",
                    "text": last_response_cache.strip()
                }))
                return

            # ── WEBSOCKET TTS + LLM STREAMING PIPELINE ──────────────────────────────────
            # One WS connection per utterance — config sent once, reused for all sentences.
            # Audio chunks stream back in real-time, forwarded to frontend immediately.
            ai_response_text = ""
            audio_frames: list = []        # Per-sentence audio bytes for cache
            first_audio_sent = False
            first_chunk_latency = None
            first_sentence = True

            async with tts_engine.create_session(language) as tts_session:
                async for sentence in conv_engine.stream_response(session_id, user_text, language):
                    
                    if first_sentence:
                        fs_time = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                        print(f"[{fs_time}] 🔊 First Sentence yielded from LLM: '{sentence}'")
                        print(f"[{fs_time}] 🎵 Streaming into TTS WebSocket...")
                        first_sentence = False

                    ai_response_text += sentence + " "
                    sentence_audio = bytearray()

                    # Collect all audio chunks for this sentence — partial MP3 fragments
                    # cannot be decoded by the browser. We send the complete sentence as one frame.
                    async for audio_chunk in tts_session.synthesize_stream(sentence):
                        # ── FAST CANCEL CHECK ────────────────────────────────────────────────
                        # asyncio.CancelledError is only raised at await points.
                        # Insert a zero-cost yield here so cancellation is noticed
                        # immediately inside the tight inner loop, rather than waiting
                        # until the next sentence's TTS recv() call.
                        await asyncio.sleep(0)
                        sentence_audio.extend(audio_chunk)

                    # Only send if we have a valid complete audio blob (min 1000 bytes guard)
                    if len(sentence_audio) >= 1000:
                        audio_bytes = bytes(sentence_audio)
                        audio_frames.append(audio_bytes)   # Cache for replay

                        # Measure TTFB on the first complete sentence
                        if not first_audio_sent:
                            effective = time.time() - interaction_start_time
                            first_chunk_latency = effective
                            current_time_str = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                            print(f"[{current_time_str}] ⚡ First Byte Sent for Query: '{sentence}' in {effective:.2f}s (TTFB)")
                            first_audio_sent = True

                        # Send complete, decodable MP3 to frontend
                        await websocket.send_bytes(audio_bytes)
                        await asyncio.sleep(0.01)   # Yield to event loop


            # ── UPDATE QUERY CACHE ────────────────────────────────────────────────────────
            last_query_cache = user_text.strip()
            last_response_cache = ai_response_text.strip()
            last_audio_cache = audio_frames   # Full audio ready for instant replay

            # Log conversation to file
            def _log_convo():
                try:
                    with open("convo.log", "a", encoding="utf-8") as f:
                        f.write(f"USER: {user_text}\n")
                        f.write(f"MYRA: {ai_response_text.strip()}\n\n")
                except Exception:
                    pass
            loop.call_soon_threadsafe(_log_convo)

            await websocket.send_text(json.dumps({
                "type": "transcript",
                "role": "assistant",
                "text": ai_response_text.strip(),
                "latency": round(first_chunk_latency, 2) if first_chunk_latency else None
            }))

        except asyncio.CancelledError:
            print("🛑 [Interrupt] Task successfully cancelled. Clearing queue.")
            last_audio_cache = []   # Clear partial audio — don't replay an interrupted response
            raise

    try:
        while True:
            data = await websocket.receive_text()
            payload = json.loads(data)
            
            p_type = payload.get("type")
            
            if p_type == "interrupt":
                # Handle forced interrupt directly
                if current_processing_task and not current_processing_task.done():
                    current_processing_task.cancel()
                    current_processing_task = None
            
            elif p_type == "audio_start":
                # User pressed mic button
                user_text_buffer = ""
                stt_start_time = time.time()
                dg_session_active = True
                
                # Create Deepgram stream — pass close handler to stop send() flood on drop
                dg_socket = stt_engine.create_live_stream(language, handle_transcript, _handle_dg_close)
                
            elif p_type == "audio_chunk":
                # Continuous stream from MediaRecorder
                if payload.get("data"):
                    audio_bytes = base64.b64decode(payload["data"])
                    
                    # Auto-reconnect if Deepgram dropped mid-session
                    if dg_socket is None and dg_session_active:
                        print("[Deepgram] 🔄 Auto-reconnecting mid-session...")
                        dg_socket = stt_engine.create_live_stream(language, handle_transcript, _handle_dg_close)
                    
                    if dg_socket:
                        try:
                            dg_socket.send(audio_bytes)
                        except Exception as e:
                            print(f"[Deepgram] ⚠️ Send failed after reconnect: {e}")
                            dg_socket = None
                    
            elif p_type == "audio_stop":
                # User released mic button
                dg_session_active = False
                if dg_socket:
                    interaction_start_time = time.time()
                    loop2 = asyncio.get_event_loop()
                    await loop2.run_in_executor(None, dg_socket.finish)
                    final_user_text = user_text_buffer.strip()
                    dg_socket = None
                    
                    if final_user_text:
                        # Only fire LLM from audio_stop if handle_transcript hasn't already dispatched it.
                        # This prevents the echo bug: two audio streams playing simultaneously.
                        if not current_processing_task or current_processing_task.done():
                            current_processing_task = asyncio.create_task(process_llm_and_tts(final_user_text, interaction_start_time))
                    else:
                        # Clear frontend processing flag if call ends with no speech
                        if not current_processing_task or current_processing_task.done():
                            await websocket.send_text(json.dumps({"type": "interrupt"}))

            elif p_type == "text" and payload.get("text"):
                # Handle direct text input
                current_processing_task = asyncio.create_task(process_llm_and_tts(payload["text"]))

    except WebSocketDisconnect:
        print(f"Client disconnected: {session_id}")
    except Exception as e:
        print(f"WebSocket Error: {e}")

