import asyncio
import os
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from dotenv import load_dotenv
from openai import AsyncOpenAI


ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from llm import GroqLLMLayer  # noqa: E402


load_dotenv(BACKEND_DIR / ".env")


DEFAULT_MODELS = [
    {"provider": "groq", "model": "llama-3.1-8b-instant"},
    {"provider": "openai", "model": "gpt-4o-mini"},
    {"provider": "openai", "model": "gpt-4.1-mini"},
]

ROUNDS = int(os.environ.get("LLM_COMPARE_ROUNDS", "3"))
REPORT_PATH = ROOT / "benchmark" / "model_latency_comparison.md"


@dataclass
class RunResult:
    provider: str
    model: str
    scenario: str
    round_num: int
    ttft_ms: float
    total_ms: float
    response_text: str
    error: Optional[str] = None


def _parse_models() -> List[Dict[str, str]]:
    """
    Optional override via env:
    COMPARE_MODELS="groq:llama-3.3-70b-versatile,openai:gpt-4o-mini,openai:gpt-4.1-mini"
    """
    raw = (os.environ.get("COMPARE_MODELS") or "").strip()
    if not raw:
        return DEFAULT_MODELS

    parsed: List[Dict[str, str]] = []
    for item in raw.split(","):
        item = item.strip()
        if not item or ":" not in item:
            continue
        provider, model = item.split(":", 1)
        provider = provider.strip().lower()
        model = model.strip()
        if provider in {"groq", "openai"} and model:
            parsed.append({"provider": provider, "model": model})
    return parsed or DEFAULT_MODELS


def _pick_filler_keys() -> List[str]:
    filler_dir = BACKEND_DIR / "filler_audio"
    if not filler_dir.exists():
        return ["[neutral_hmm]", "[ack_okay]", "[friendly_of_course]"]
    keys = []
    for mp3 in sorted(filler_dir.glob("*.mp3"))[:12]:
        keys.append(f"[{mp3.stem}]")
    return keys or ["[neutral_hmm]", "[ack_okay]", "[friendly_of_course]"]


def _build_scenarios() -> List[Dict[str, str]]:
    """
    Pull the same prompt-building logic the app uses so benchmark inputs match production style.
    """
    layer = GroqLLMLayer(customer_context=None, filler_keys=_pick_filler_keys())

    # Scenario 1: exactly what you asked for (user says hello).
    layer.conversation_history.append({"role": "user", "content": "hello"})
    hello_system = layer._build_system_for_request()
    hello_messages = [
        {"role": "system", "content": hello_system},
        {"role": "user", "content": "hello"},
    ]

    # Scenario 2: Hindi first turn, to test language-switch overhead.
    layer_hi = GroqLLMLayer(customer_context=None, filler_keys=_pick_filler_keys())
    layer_hi.conversation_history.append({"role": "user", "content": "हेलो, कौन बोल रहा है?"})
    hindi_system = layer_hi._build_system_for_request()
    hindi_messages = [
        {"role": "system", "content": hindi_system},
        {"role": "user", "content": "हेलो, कौन बोल रहा है?"},
    ]

    # Scenario 3: Mid-call follow-up with existing context.
    layer_mid = GroqLLMLayer(customer_context=None, filler_keys=_pick_filler_keys())
    layer_mid.conversation_history.extend(
        [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "[ack_hello] Hi, this is Myra from PropVox Estate. Is this a good time?"},
            {"role": "user", "content": "yes tell me price"},
        ]
    )
    layer_mid._past_first_assistant_reply = True
    mid_system = layer_mid._build_system_for_request()
    mid_messages = [
        {"role": "system", "content": mid_system},
        {"role": "user", "content": "yes tell me price"},
    ]

    return [
        {
            "name": "First turn hello",
            "messages": hello_messages,
            "user_text": "hello",
        },
        {
            "name": "First turn Hindi hello",
            "messages": hindi_messages,
            "user_text": "हेलो, कौन बोल रहा है?",
        },
        {
            "name": "Mid-call pricing question",
            "messages": mid_messages,
            "user_text": "yes tell me price",
        },
    ]


async def _run_stream(
    client: AsyncOpenAI,
    provider: str,
    model: str,
    scenario_name: str,
    round_num: int,
    messages: List[Dict[str, str]],
) -> RunResult:
    start = time.perf_counter()
    ttft_ms = -1.0
    parts: List[str] = []

    try:
        stream = await client.chat.completions.create(
            model=model,
            messages=messages,
            stream=True,
            temperature=0.7,
            max_tokens=220,
        )
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta.content
            if not delta:
                continue
            parts.append(delta)
            if ttft_ms < 0:
                ttft_ms = (time.perf_counter() - start) * 1000
        total_ms = (time.perf_counter() - start) * 1000
        return RunResult(
            provider=provider,
            model=model,
            scenario=scenario_name,
            round_num=round_num,
            ttft_ms=ttft_ms,
            total_ms=total_ms,
            response_text="".join(parts).strip(),
        )
    except Exception as e:
        total_ms = (time.perf_counter() - start) * 1000
        return RunResult(
            provider=provider,
            model=model,
            scenario=scenario_name,
            round_num=round_num,
            ttft_ms=ttft_ms,
            total_ms=total_ms,
            response_text="",
            error=str(e),
        )


def _fmt_ms(v: float) -> str:
    return "n/a" if v < 0 else f"{v:.1f} ms"


def _write_report(results: List[RunResult], scenarios: List[Dict[str, str]]) -> None:
    lines: List[str] = []
    lines.append("# LLM Latency Comparison")
    lines.append("")
    lines.append(f"- Generated at: `{datetime.now().isoformat(timespec='seconds')}`")
    lines.append(f"- Rounds per model/scenario: `{ROUNDS}`")
    lines.append("- Benchmark style: streaming chat completion (TTFT + total)")
    lines.append("")

    lines.append("## Prompts Used")
    lines.append("")
    lines.append("These are generated from the same prompt builder used in `backend/llm.py`.")
    lines.append("")
    for s in scenarios:
        lines.append(f"### {s['name']}")
        lines.append(f"- User input: `{s['user_text']}`")
        lines.append("- Messages payload:")
        lines.append("```json")
        # Keep report readable; still enough to inspect exact prompt shape.
        message_preview = []
        for m in s["messages"]:
            content = m["content"]
            if len(content) > 2200:
                content = content[:2200] + "\n... (truncated)"
            message_preview.append({"role": m["role"], "content": content})
        import json
        lines.append(json.dumps(message_preview, ensure_ascii=False, indent=2))
        lines.append("```")
        lines.append("")

    lines.append("## Raw Results")
    lines.append("")
    lines.append("| Provider | Model | Scenario | Round | TTFT | Total | Status |")
    lines.append("|---|---|---|---:|---:|---:|---|")
    for r in results:
        status = "ok" if not r.error else f"error: {r.error[:80]}"
        lines.append(
            f"| {r.provider} | `{r.model}` | {r.scenario} | {r.round_num} | {_fmt_ms(r.ttft_ms)} | {_fmt_ms(r.total_ms)} | {status} |"
        )
    lines.append("")

    lines.append("## Averages (Successful Runs Only)")
    lines.append("")
    lines.append("| Provider | Model | Scenario | Avg TTFT | Avg Total | Runs |")
    lines.append("|---|---|---|---:|---:|---:|")

    grouped: Dict[str, List[RunResult]] = {}
    for r in results:
        if r.error:
            continue
        key = f"{r.provider}|{r.model}|{r.scenario}"
        grouped.setdefault(key, []).append(r)

    for key, rows in sorted(grouped.items()):
        provider, model, scenario = key.split("|", 2)
        avg_ttft = statistics.mean([x.ttft_ms for x in rows if x.ttft_ms >= 0]) if rows else -1
        avg_total = statistics.mean([x.total_ms for x in rows]) if rows else -1
        lines.append(
            f"| {provider} | `{model}` | {scenario} | {_fmt_ms(avg_ttft)} | {_fmt_ms(avg_total)} | {len(rows)} |"
        )
    lines.append("")

    lines.append("## Winner By Scenario (Lowest Avg TTFT)")
    lines.append("")
    for scenario in sorted({r.scenario for r in results}):
        candidates = []
        for key, rows in grouped.items():
            provider, model, sc = key.split("|", 2)
            if sc != scenario:
                continue
            tts = [x.ttft_ms for x in rows if x.ttft_ms >= 0]
            if not tts:
                continue
            candidates.append((statistics.mean(tts), provider, model))
        if not candidates:
            lines.append(f"- {scenario}: no successful runs")
            continue
        candidates.sort(key=lambda x: x[0])
        best = candidates[0]
        lines.append(f"- {scenario}: `{best[1]}:{best[2]}` at `{best[0]:.1f} ms` avg TTFT")

    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")


async def main() -> None:
    groq_key = (os.environ.get("GROQ_API_KEY") or "").strip()
    openai_key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    models = _parse_models()

    groq_client = AsyncOpenAI(api_key=groq_key, base_url="https://api.groq.com/openai/v1") if groq_key else None
    openai_client = AsyncOpenAI(api_key=openai_key) if openai_key else None

    if not groq_client and not openai_client:
        raise RuntimeError("Set GROQ_API_KEY and/or OPENAI_API_KEY in backend/.env first.")

    scenarios = _build_scenarios()
    all_results: List[RunResult] = []

    for sc in scenarios:
        print(f"\n=== Scenario: {sc['name']} ===")
        for round_num in range(1, ROUNDS + 1):
            for m in models:
                provider = m["provider"]
                model = m["model"]

                client = groq_client if provider == "groq" else openai_client
                if client is None:
                    all_results.append(
                        RunResult(
                            provider=provider,
                            model=model,
                            scenario=sc["name"],
                            round_num=round_num,
                            ttft_ms=-1,
                            total_ms=-1,
                            response_text="",
                            error=f"{provider} api key missing",
                        )
                    )
                    print(f"[{provider}:{model}] round {round_num} -> skipped (key missing)")
                    continue

                res = await _run_stream(
                    client=client,
                    provider=provider,
                    model=model,
                    scenario_name=sc["name"],
                    round_num=round_num,
                    messages=sc["messages"],
                )
                all_results.append(res)
                if res.error:
                    print(f"[{provider}:{model}] round {round_num} -> ERROR: {res.error}")
                else:
                    print(
                        f"[{provider}:{model}] round {round_num} -> "
                        f"TTFT={_fmt_ms(res.ttft_ms)} Total={_fmt_ms(res.total_ms)}"
                    )
                await asyncio.sleep(0.35)

    _write_report(all_results, scenarios)
    print(f"\nReport written to: {REPORT_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
