import asyncio
import os
import sys
import time
import socket
from dataclasses import dataclass, field
from typing import List, Optional

from dotenv import load_dotenv

# We use the openai package for both OpenAI and Groq (Groq has an OpenAI-compatible API)
try:
    from openai import AsyncOpenAI
except ImportError:
    print("❌ 'openai' package is required. Run: pip install openai")
    sys.exit(1)

load_dotenv()

# Ensure emoji/box drawing output works in Windows terminals.
if os.name == "nt":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ─── Config ────────────────────────────────────────────────────────────────────
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")

# We will test multiple models for a fair comparison
TEST_MODELS = [
    {"provider": "Groq", "model": "llama-3.3-70b-versatile", "client": "groq"},
]

# Simple and complex prompts to see if TTFT scales with prompt size
TEST_PROMPTS = [
    {"name": "Short Greeting", "text": "Hello! Please reply with a short greeting."},
    {"name": "Complex Scenario", "text": "You are a customer service AI for a real estate agency in India. A user has just asked 'Do you have any 3BHK flats available in Andheri West under 2.5 Crores?' Write a helpful, polite, and concise response in english."},
]

NUM_ROUNDS = 3

# ─── Data classes ──────────────────────────────────────────────────────────────
@dataclass
class LLMResult:
    provider: str
    model: str
    prompt_name: str
    round_num: int
    ttft_ms: float          # Time To First Token
    total_ms: float         # Time to complete generation
    tokens_generated: int
    error: Optional[str] = None
    response_text: Optional[str] = None

@dataclass
class ModelSummary:
    provider: str
    model: str
    results: List[LLMResult] = field(default_factory=list)

    @property
    def valid(self):
        return [r for r in self.results if r.error is None]

    def avg(self, attr):
        vals = [getattr(r, attr) for r in self.valid]
        return sum(vals) / len(vals) if vals else float("inf")

    def min_val(self, attr):
        vals = [getattr(r, attr) for r in self.valid]
        return min(vals) if vals else float("inf")

# ─── Benchmark Logic ───────────────────────────────────────────────────────────

async def benchmark_llm(provider: str, model_id: str, client_type: str, prompt_text: str, prompt_name: str, round_num: int, openai_client: AsyncOpenAI, groq_client: AsyncOpenAI) -> LLMResult:
    client = openai_client if client_type == "openai" else groq_client
    if client is None:
        return LLMResult(provider, model_id, prompt_name, round_num, -1, -1, 0, f"API Key for {provider} not set")
    
    t_start = time.perf_counter()
    ttft_ms = None
    tokens_generated = 0
    error = None
    response_parts: List[str] = []

    try:
        # Create streaming request
        stream = await client.chat.completions.create(
            model=model_id,
            messages=[{"role": "user", "content": prompt_text}],
            stream=True,
            max_tokens=150,
            temperature=0.7
        )

        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta.content
            if delta:
                response_parts.append(delta)
                if ttft_ms is None:
                    # First token received
                    ttft_ms = (time.perf_counter() - t_start) * 1000
                tokens_generated += 1
                
        total_ms = (time.perf_counter() - t_start) * 1000

    except Exception as e:
        total_ms = (time.perf_counter() - t_start) * 1000
        error = str(e)

    response_text = "".join(response_parts) if response_parts else None

    return LLMResult(
        provider=provider,
        model=model_id,
        prompt_name=prompt_name,
        round_num=round_num,
        ttft_ms=ttft_ms if ttft_ms is not None else -1,
        total_ms=total_ms,
        tokens_generated=tokens_generated,
        error=error,
        response_text=response_text,
    )

def color(text, code): return f"\033[{code}m{text}\033[0m"
GREEN  = lambda t: color(t, "92")
RED    = lambda t: color(t, "91")
YELLOW = lambda t: color(t, "93")
CYAN   = lambda t: color(t, "96")
BOLD   = lambda t: color(t, "1")

def print_result(r: LLMResult):
    status = GREEN("✅ OK") if r.error is None else RED(f"❌ {r.error}")
    ttft   = f"{r.ttft_ms:.1f}ms" if r.ttft_ms >= 0 else "N/A"
    print(
        f"  [{r.provider} - {r.model:<22}] Round {r.round_num} | TTFT: {ttft:>8} | "
        f"Total: {r.total_ms:>7.1f}ms | Tokens: {r.tokens_generated:>3} | {status}"
    )
    if r.error is None and r.response_text:
        # Indent model output so it stays readable under the metrics line
        for line in r.response_text.splitlines() or [r.response_text]:
            print(f"    {CYAN(line)}")
    elif r.error is None and not r.response_text:
        print(f"    {YELLOW('(empty response)')}")

# ─── Main ──────────────────────────────────────────────────────────────────────

def measure_tcp_ping(host="api.groq.com", port=443, pings=3):
    rtts = []
    for _ in range(pings):
        try:
            t_start = time.perf_counter()
            with socket.create_connection((host, port), timeout=2.0):
                rtts.append((time.perf_counter() - t_start) * 1000)
        except Exception:
            pass
    return sum(rtts) / len(rtts) if rtts else -1

async def run_benchmark():
    print(BOLD("\n=== Measuring Network Connection ==="))
    ping_ms = measure_tcp_ping()
    if ping_ms > 0:
        print(f"  🌐 Base Network TCP Latency to Groq: {CYAN(f'{ping_ms:.1f} ms')}")
        print(f"     (This means ~{ping_ms:.1f}ms of your TTFT is just pure network travel data overhead!)")

    print(BOLD("\n=== LLM streaming TTFT Benchmark: OpenAI vs Groq ===\n"))

    openai_client = None
    groq_client = None

    if OPENAI_API_KEY:
        openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
    else:
        print(RED("❌ OPENAI_API_KEY is not set in .env — skipping OpenAI tests."))

    if GROQ_API_KEY:
        groq_client = AsyncOpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1")
    else:
        print(RED("❌ GROQ_API_KEY is not set in .env — skipping Groq tests."))

    if not openai_client and not groq_client:
        print("No API keys configured. Exiting.")
        return

    summaries = {}
    for m in TEST_MODELS:
        key = f"{m['provider']}:{m['model']}"
        summaries[key] = ModelSummary(provider=m["provider"], model=m["model"])

    for prompt in TEST_PROMPTS:
        print(CYAN(f"\n📝 Prompt: {prompt['name']}"))
        
        for round_num in range(1, NUM_ROUNDS + 1):
            tasks = []
            for m in TEST_MODELS:
                tasks.append(
                    benchmark_llm(
                        provider=m["provider"], 
                        model_id=m["model"], 
                        client_type=m["client"], 
                        prompt_text=prompt["text"], 
                        prompt_name=prompt["name"], 
                        round_num=round_num, 
                        openai_client=openai_client, 
                        groq_client=groq_client
                    )
                )

            # Run sequentially to avoid rate-limiting issues on Groq free tier
            # But we can run OpenAI and Groq concurrently if we want. Let's run completely sequentially to get the most accurate, un-congested TTFT
            for task in tasks:
                res = await task
                key = f"{res.provider}:{res.model}"
                summaries[key].results.append(res)
                print_result(res)
                await asyncio.sleep(0.5)

            print() # newline after round

    # ── Overall Summary ────────────────────────────────────────────────────────
    print(BOLD("\n" + "="*60))
    print(BOLD("   📊 OVERALL TTFT SUMMARY"))
    print(BOLD("="*60))

    valid_summaries = [s for s in summaries.values() if s.valid]
    
    if not valid_summaries:
        print("No valid results to summarize.")
        return

    print(f"  {'Provider':<10} | {'Model':<25} | {'Min TTFT':<10} | {'Avg TTFT':<10} | {'Gen Speed'}")
    print(f"  {'-'*75}")
    for s in sorted(valid_summaries, key=lambda x: x.avg('ttft_ms')):
        avg_ttft = s.avg("ttft_ms")
        min_ttft = s.min_val("ttft_ms")
        
        # Calculate tokens per second (excluding TTFT)
        tps_list = []
        for r in s.valid:
            gen_time = r.total_ms - r.ttft_ms
            if gen_time > 0 and r.tokens_generated > 1:
                tps = (r.tokens_generated - 1) / (gen_time / 1000)
                tps_list.append(tps)
        
        avg_tps = sum(tps_list)/len(tps_list) if tps_list else 0
        
        provider_colored = GREEN(s.provider) if s.provider == "Groq" else CYAN(s.provider)
        
        print(f"  {provider_colored:<19} | {s.model:<25} | {min_ttft:>8.1f}ms | {avg_ttft:>8.1f}ms | {avg_tps:>6.1f} t/s")

    print()
    winner = sorted(valid_summaries, key=lambda x: x.avg('ttft_ms'))[0]
    print(BOLD(f"  🏆 FASTEST TTFT: {winner.provider} ({winner.model}) at {winner.avg('ttft_ms'):.1f}ms average!"))
    print()

if __name__ == "__main__":
    asyncio.run(run_benchmark())
