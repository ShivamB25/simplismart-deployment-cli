#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "httpx>=0.27",
# ]
# ///
"""OpenAI-compatible chat probe/benchmark for SGLang/vLLM endpoints.

Modes:
  smoke  - connectivity / latency sanity check only (short replies)
  decode - deterministic long-output workload for decode throughput

TTFT is measured only when the first non-empty content or reasoning token
arrives, never on role/metadata SSE chunks.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx


@dataclass
class RequestResult:
    ok: bool
    status_code: int | None
    error: str | None
    latency_ms: float
    ttft_ms: float | None
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    output_text: str
    reasoning_text: str
    usage_reported: bool


def percentile(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * p
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return ordered[low]
    weight = rank - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def build_prompt(*, mode: str, input_tokens: int, output_tokens: int) -> str:
    filler_count = max(input_tokens - 32, 8)
    filler = " ".join(f"w{i % 997}" for i in range(filler_count))
    if mode == "smoke":
        return (
            "Connectivity probe. Ignore the filler words and reply with exactly: "
            "healthy\n"
            f"Filler: {filler}"
        )
    # Deterministic long-output decode workload. Ask for a fixed token budget.
    return (
        "You are in a decode-throughput benchmark.\n"
        f"Ignore the filler words. Generate exactly {output_tokens} tokens.\n"
        "Output only the repeating sequence 'alpha beta gamma delta ' forever "
        "until you hit the token limit. Do not stop early. Do not explain.\n"
        f"Filler: {filler}"
    )


def _delta_text(delta: dict[str, Any]) -> tuple[str, str]:
    content = delta.get("content") or ""
    reasoning = (
        delta.get("reasoning_content")
        or delta.get("reasoning")
        or ""
    )
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        )
    if not isinstance(content, str):
        content = str(content)
    if not isinstance(reasoning, str):
        reasoning = str(reasoning)
    return content, reasoning


async def one_request(
    client: httpx.AsyncClient,
    *,
    url: str,
    headers: dict[str, str],
    model: str,
    prompt: str,
    max_tokens: int,
    temperature: float,
    stream: bool,
    timeout_s: float,
    extra_body: dict[str, Any] | None = None,
) -> RequestResult:
    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": stream,
    }
    if stream:
        payload["stream_options"] = {"include_usage": True}
    if extra_body:
        payload.update(extra_body)

    started = time.perf_counter()
    ttft_ms: float | None = None
    output_chunks: list[str] = []
    reasoning_chunks: list[str] = []
    prompt_tokens = 0
    completion_tokens = 0
    total_tokens = 0
    usage_reported = False
    status: int | None = None

    try:
        if stream:
            async with client.stream(
                "POST",
                url,
                headers=headers,
                json=payload,
                timeout=timeout_s,
            ) as response:
                status = response.status_code
                if status >= 400:
                    body = (await response.aread()).decode("utf-8", "replace")[:500]
                    return RequestResult(
                        ok=False,
                        status_code=status,
                        error=body or f"HTTP {status}",
                        latency_ms=(time.perf_counter() - started) * 1000,
                        ttft_ms=None,
                        prompt_tokens=0,
                        completion_tokens=0,
                        total_tokens=0,
                        output_text="",
                        reasoning_text="",
                        usage_reported=False,
                    )

                async for line in response.aiter_lines():
                    if not line or line.startswith(":") or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue

                    choice = (chunk.get("choices") or [{}])[0]
                    delta = choice.get("delta") or {}
                    content, reasoning = _delta_text(delta)
                    if content:
                        output_chunks.append(content)
                    if reasoning:
                        reasoning_chunks.append(reasoning)
                    # TTFT = first non-empty generated token, not role/metadata.
                    if ttft_ms is None and (content or reasoning):
                        ttft_ms = (time.perf_counter() - started) * 1000

                    usage = chunk.get("usage") or {}
                    if usage:
                        usage_reported = True
                        prompt_tokens = int(usage.get("prompt_tokens") or prompt_tokens)
                        completion_tokens = int(
                            usage.get("completion_tokens") or completion_tokens
                        )
                        total_tokens = int(usage.get("total_tokens") or total_tokens)
        else:
            response = await client.post(
                url,
                headers=headers,
                json=payload,
                timeout=timeout_s,
            )
            status = response.status_code
            if status >= 400:
                return RequestResult(
                    ok=False,
                    status_code=status,
                    error=response.text[:500],
                    latency_ms=(time.perf_counter() - started) * 1000,
                    ttft_ms=None,
                    prompt_tokens=0,
                    completion_tokens=0,
                    total_tokens=0,
                    output_text="",
                    reasoning_text="",
                    usage_reported=False,
                )
            body = response.json()
            choice = (body.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            content = message.get("content") or ""
            reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
            if content:
                output_chunks.append(content)
            if reasoning:
                reasoning_chunks.append(str(reasoning))
            usage = body.get("usage") or {}
            if usage:
                usage_reported = True
                prompt_tokens = int(usage.get("prompt_tokens") or 0)
                completion_tokens = int(usage.get("completion_tokens") or 0)
                total_tokens = int(usage.get("total_tokens") or 0)
            # Non-stream cannot measure true TTFT.
            ttft_ms = None

        return RequestResult(
            ok=True,
            status_code=status,
            error=None,
            latency_ms=(time.perf_counter() - started) * 1000,
            ttft_ms=ttft_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            output_text="".join(output_chunks),
            reasoning_text="".join(reasoning_chunks),
            usage_reported=usage_reported,
        )
    except Exception as exc:  # noqa: BLE001 - surface all request failures in results
        return RequestResult(
            ok=False,
            status_code=None,
            error=str(exc),
            latency_ms=(time.perf_counter() - started) * 1000,
            ttft_ms=ttft_ms,
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            output_text="",
            reasoning_text="",
            usage_reported=False,
        )


async def run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    base = args.base_url.rstrip("/")
    if base.endswith("/v1"):
        chat_url = f"{base}/chat/completions"
        health_url = base[: -len("/v1")] + "/health"
    else:
        chat_url = f"{base}/v1/chat/completions"
        health_url = f"{base}/health"

    headers = {
        "Authorization": f"Bearer {args.api_key}",
        "Content-Type": "application/json",
    }
    prompt = build_prompt(
        mode=args.mode,
        input_tokens=args.input_tokens,
        output_tokens=args.output_tokens,
    )
    extra_body: dict[str, Any] = {}
    if args.disable_thinking:
        extra_body["chat_template_kwargs"] = {"enable_thinking": False}
    semaphore = asyncio.Semaphore(args.concurrency)

    async with httpx.AsyncClient(verify=not args.insecure, timeout=args.timeout) as client:
        health = await client.get(health_url)
        if health.status_code >= 400:
            raise RuntimeError(f"health check failed: HTTP {health.status_code}")

        results: list[RequestResult] = []

        async def worker(index: int) -> None:
            async with semaphore:
                result = await one_request(
                    client,
                    url=chat_url,
                    headers=headers,
                    model=args.model,
                    prompt=prompt,
                    max_tokens=args.output_tokens,
                    temperature=args.temperature,
                    stream=args.stream,
                    timeout_s=args.timeout,
                    extra_body=extra_body,
                )
                results.append(result)
                status = "ok" if result.ok else f"err:{result.error}"
                ttft = (
                    f"ttft={result.ttft_ms:.0f}ms"
                    if result.ttft_ms is not None
                    else "ttft=n/a"
                )
                print(
                    f"[{index + 1}/{args.num_prompts}] {status} "
                    f"latency={result.latency_ms:.0f}ms {ttft} "
                    f"out_tokens={result.completion_tokens} "
                    f"usage={'yes' if result.usage_reported else 'no'}"
                )

        wall_started = time.perf_counter()
        await asyncio.gather(*(worker(i) for i in range(args.num_prompts)))
        wall_s = time.perf_counter() - wall_started

    ok_results = [r for r in results if r.ok]
    latencies = [r.latency_ms for r in ok_results]
    ttfts = [r.ttft_ms for r in ok_results if r.ttft_ms is not None]
    completion_tokens = sum(r.completion_tokens for r in ok_results)
    prompt_tokens = sum(r.prompt_tokens for r in ok_results)
    usage_count = sum(1 for r in ok_results if r.usage_reported)
    avg_completion = (
        completion_tokens / len(ok_results) if ok_results else float("nan")
    )
    decode_valid = (
        args.mode == "decode"
        and usage_count == len(ok_results)
        and ok_results
        and avg_completion >= 0.8 * args.output_tokens
    )

    # Inter-token latency only when TTFT and completion tokens are present.
    itls: list[float] = []
    for result in ok_results:
        if (
            result.ttft_ms is not None
            and result.completion_tokens > 1
            and result.latency_ms > result.ttft_ms
        ):
            itls.append(
                (result.latency_ms - result.ttft_ms) / (result.completion_tokens - 1)
            )

    summary = {
        "label": "connectivity_smoke" if args.mode == "smoke" else "decode_throughput",
        "mode": args.mode,
        "endpoint": chat_url,
        "model": args.model,
        "num_prompts": args.num_prompts,
        "concurrency": args.concurrency,
        "input_tokens_requested": args.input_tokens,
        "output_tokens_requested": args.output_tokens,
        "stream": args.stream,
        "disable_thinking": args.disable_thinking,
        "successes": len(ok_results),
        "failures": len(results) - len(ok_results),
        "usage_reported_count": usage_count,
        "avg_completion_tokens": round(avg_completion, 2) if ok_results else None,
        "decode_metrics_valid": decode_valid,
        "wall_seconds": round(wall_s, 3),
        "request_throughput_rps": round(len(ok_results) / wall_s, 3) if wall_s else 0.0,
        "output_tok_per_s": round(completion_tokens / wall_s, 3) if wall_s else 0.0,
        "total_tok_per_s": round((prompt_tokens + completion_tokens) / wall_s, 3)
        if wall_s
        else 0.0,
        "latency_ms": {
            "avg": round(statistics.fmean(latencies), 1) if latencies else None,
            "p50": round(percentile(latencies, 0.50), 1) if latencies else None,
            "p95": round(percentile(latencies, 0.95), 1) if latencies else None,
            "p99": round(percentile(latencies, 0.99), 1) if latencies else None,
            "max": round(max(latencies), 1) if latencies else None,
        },
        "ttft_ms": {
            "avg": round(statistics.fmean(ttfts), 1) if ttfts else None,
            "p50": round(percentile(ttfts, 0.50), 1) if ttfts else None,
            "p95": round(percentile(ttfts, 0.95), 1) if ttfts else None,
            "p99": round(percentile(ttfts, 0.99), 1) if ttfts else None,
            "max": round(max(ttfts), 1) if ttfts else None,
        },
        "inter_token_latency_ms": {
            "avg": round(statistics.fmean(itls), 2) if itls else None,
            "p50": round(percentile(itls, 0.50), 2) if itls else None,
            "p95": round(percentile(itls, 0.95), 2) if itls else None,
        },
        "tokens": {
            "prompt": prompt_tokens,
            "completion": completion_tokens,
            "total": prompt_tokens + completion_tokens,
            "source": "provider_usage" if usage_count else "missing",
        },
        "notes": [
            "TTFT starts at first non-empty content/reasoning token.",
            "Token counts come only from provider usage when present.",
            (
                "This run is connectivity/smoke only; do not use for throughput claims."
                if args.mode == "smoke"
                else (
                    "Decode metrics valid: usage present and completion tokens near max_tokens."
                    if decode_valid
                    else "Decode metrics incomplete/invalid: missing usage or early stop."
                )
            ),
        ],
        "errors": [r.error for r in results if not r.ok][:10],
        "sample_output": next((r.output_text[:200] for r in ok_results if r.output_text), ""),
        "requests": [asdict(r) for r in results] if args.save_requests else None,
    }
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="Host root or .../v1 URL")
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--model", default="Qwen/Qwen3.6-35B-A3B-FP8")
    parser.add_argument(
        "--mode",
        choices=["smoke", "decode"],
        default="smoke",
        help="smoke = connectivity only; decode = long-output throughput workload",
    )
    parser.add_argument("--num-prompts", type=int, default=8)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--input-tokens", type=int, default=128)
    parser.add_argument("--output-tokens", type=int, default=64)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--stream", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--insecure", action="store_true", help="Skip TLS verification")
    parser.add_argument(
        "--disable-thinking",
        action="store_true",
        help="Disable Qwen thinking mode via chat_template_kwargs.enable_thinking=false",
    )
    parser.add_argument("--save-requests", action="store_true")
    parser.add_argument("--output", type=Path, default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.mode == "decode" and args.output_tokens < 64:
        raise SystemExit("decode mode requires --output-tokens >= 64")
    summary = asyncio.run(run_benchmark(args))
    text = json.dumps({k: v for k, v in summary.items() if k != "requests"}, indent=2)
    print(text)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
