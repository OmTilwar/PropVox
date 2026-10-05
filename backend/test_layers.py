import os
import time
import asyncio
from dotenv import load_dotenv

from stt import DeepgramSTTLayer
from llm import GroqLLMLayer
from tts import SarvamTTSLayer

load_dotenv()

async def test_all():
    print("=== Testing Latency of Modular Layers (3 Rounds) ===\n")
    
    for round_idx in range(1, 4):
        print(f"━━━ Round {round_idx}/3 ━━━")
        
        # 1. Test LLM Latency
        print("  [1] LLM Layer (Groq)...")
        llm = GroqLLMLayer()
        
        t0_llm = time.perf_counter()
        stream = llm.generate_response_stream("Hello! Please give a very short greeting.")
        
        ttft_ms = None
        response_text = ""
        async for chunk in stream:
            if ttft_ms is None:
                ttft_ms = (time.perf_counter() - t0_llm) * 1000
            response_text += chunk
            
        total_llm_ms = (time.perf_counter() - t0_llm) * 1000
        print(f"      ✅ TTFT: {ttft_ms:.1f} ms | Total Time: {total_llm_ms:.1f} ms | Response: '{response_text.strip()}'")

        # 2. Test TTS Latency
        test_phrase = response_text.strip()[:60]
        print(f"  [2] TTS Layer (Sarvam) with phrase: '{test_phrase}'")
        tts = SarvamTTSLayer()
        
        t0_tts = time.perf_counter()
        ttfb_ms = None
        audio_data = b""
        
        iterator = tts.speak(test_phrase)
        async for chunk in iterator:
            if ttfb_ms is None:
                ttfb_ms = (time.perf_counter() - t0_tts) * 1000
            audio_data += chunk
            
        total_tts_ms = (time.perf_counter() - t0_tts) * 1000
        if audio_data:
            print(f"      ✅ TTFB: {ttfb_ms:.1f} ms | Total Fetch: {total_tts_ms:.1f} ms | Audio: {len(audio_data)} bytes")
        else:
            print("      ❌ TTS Failed.")

        # 3. Test STT Connection Latency
        print("  [3] STT Layer (Deepgram)...")
        stt = DeepgramSTTLayer()
        
        def dummy_callback(result):
            pass
        
        t0_stt = time.perf_counter()
        success = await stt.connect(dummy_callback)
        stt_conn_ms = (time.perf_counter() - t0_stt) * 1000
        
        if success:
            print(f"      ✅ WebSocket Connect Time: {stt_conn_ms:.1f} ms")
            await stt.stop()
        else:
            print("      ❌ STT Connection Failed.")
            
        print() # New line spacing

if __name__ == "__main__":
    asyncio.run(test_all())
