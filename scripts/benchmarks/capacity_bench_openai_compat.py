#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "httpx[http2]>=0.28",
#   "jinja2>=3.1",
#   "numpy>=2.0",
#   "tqdm>=4.67",
#   "transformers>=4.51",
# ]
# ///
"""Capacity-ready OpenAI-compatible load test for SGLang/vLLM.

Guarantees vs the pristine upstream openai-llm-benchmark clone:
1. Prompt lengths are constructed/verified with the model tokenizer + chat template.
2. Each request uses a unique prompt body (seeded), so identical-prompt Radix cache inflation is avoided.
3. SGLang `ignore_eos=true` is set, and any response with completion_tokens != max_tokens is rejected.
4. Thinking mode is disabled for Qwen decode measurement.
5. Optional `--stream` measures TTFT as time-to-first non-empty content/reasoning token.
   Non-stream mode only reports end-to-end latency (TTFT is null).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx
import numpy as np
from transformers import AutoTokenizer

try:
    from tqdm.asyncio import tqdm_asyncio
except ImportError:  # pragma: no cover
    tqdm_asyncio = None


@dataclass
class RequestResult:
    ok: bool
    accepted: bool
    error: str | None
    latency_s: float
    ttft_s: float | None
    prompt_tokens_usage: int
    completion_tokens: int
    total_tokens: int
    finish_reason: str | None
    constructed_prompt_tokens: int
    output_preview: str
    stream: bool


def percentile(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    return float(np.percentile(values, p))


def latency_summary(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"avg": None, "p50": None, "p95": None, "p99": None, "max": None}
    return {
        "avg": round(statistics.fmean(values), 3),
        "p50": round(percentile(values, 50), 3),
        "p95": round(percentile(values, 95), 3),
        "p99": round(percentile(values, 99), 3),
        "max": round(max(values), 3),
    }


class ExactPromptBuilder:
    """Build unique prompts with exact chat-template token counts."""

    def __init__(self, tokenizer_id: str, *, enable_thinking: bool) -> None:
        self.tok = AutoTokenizer.from_pretrained(tokenizer_id, trust_remote_code=True)
        self.enable_thinking = enable_thinking

    def rendered_token_count(self, user_text: str) -> int:
        rendered = self.tok.apply_chat_template(
            [{"role": "user", "content": user_text}],
            tokenize=True,
            add_generation_prompt=True,
            enable_thinking=self.enable_thinking,
            return_dict=True,
        )
        # Transformers may return a BatchEncoding. len(BatchEncoding) is the
        # number of fields (typically input_ids and attention_mask), not tokens.
        token_ids = rendered["input_ids"] if hasattr(rendered, "keys") else rendered
        if len(token_ids) and isinstance(token_ids[0], (list, tuple)):
            token_ids = token_ids[0]
        return len(token_ids)

    def build(self, target_tokens: int, *, seed: int) -> tuple[str, int]:
        header = (
            f"CAPACITY_BENCH seed={seed} target_tokens={target_tokens}. "
            "Continue the integer sequence starting at 0, space-separated, "
            "without stopping early."
        )
        prefix = header + "\nSEQ: "
        prefix_tokens = self.rendered_token_count(prefix)
        if prefix_tokens >= target_tokens:
            raise ValueError(
                f"header alone uses {prefix_tokens} tokens; cannot build target {target_tokens}"
            )

        # Grow with unique numeric tokens until we reach/overshoot, then trim.
        body = prefix
        n = 0
        while self.rendered_token_count(body) < target_tokens:
            body += f"{(seed * 1000003 + n) % 997} "
            n += 1
            if n > target_tokens * 8:
                raise RuntimeError(
                    "failed to grow prompt to target token count; "
                    f"current={self.rendered_token_count(body)} target={target_tokens}"
                )

        # Trim by removing trailing numbers until exact or one under, then pad with short fillers.
        parts = body.split(" ")
        while len(parts) > 1 and self.rendered_token_count(" ".join(parts).rstrip() + " ") > target_tokens:
            parts.pop()
        body = " ".join(parts).rstrip() + " "

        got = self.rendered_token_count(body)
        if got == target_tokens:
            return body, got

        # Fine adjust with single-character fillers that usually map 1:1 for Qwen/Gemma tokenizers.
        fillers = ["a", "b", "c", "d", "e", "x", "y", "z", ".", ",", ";"]
        guard = 0
        while got < target_tokens and guard < 5000:
            guard += 1
            advanced = False
            for cand in fillers:
                trial = body + cand
                trial_got = self.rendered_token_count(trial)
                if trial_got == target_tokens:
                    return trial, trial_got
                if trial_got < target_tokens:
                    body = trial
                    got = trial_got
                    advanced = True
                    break
            if not advanced:
                # Append a space+digit which often adds tokens predictably.
                body += f" {guard % 10}"
                got = self.rendered_token_count(body)
                if got == target_tokens:
                    return body, got
                if got > target_tokens:
                    raise RuntimeError(
                        f"unable to hit exact token count; last={got} target={target_tokens}"
                    )

        got = self.rendered_token_count(body)
        if got != target_tokens:
            raise RuntimeError(f"constructed prompt tokens={got}, expected {target_tokens}")
        return body, got


def _first_token_text(delta: dict[str, Any]) -> str:
    content = delta.get("content")
    reasoning = delta.get("reasoning_content") or delta.get("reasoning")
    pieces: list[str] = []
    if isinstance(content, str) and content:
        pieces.append(content)
    if isinstance(reasoning, str) and reasoning:
        pieces.append(reasoning)
    return "".join(pieces)


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _validate_positive_counts(args: argparse.Namespace) -> None:
    for option, value in (
        ("--requests", args.requests),
        ("--concurrency", args.concurrency),
        ("--prompt-tokens", args.prompt_tokens),
        ("--max-tokens", args.max_tokens),
    ):
        if value < 1:
            raise ValueError(f"{option} must be a positive integer")


async def one_request(
    client: httpx.AsyncClient,
    *,
    url: str,
    headers: dict[str, str],
    model: str,
    prompt: str,
    constructed_prompt_tokens: int,
    max_tokens: int,
    temperature: float,
    timeout_s: float,
    disable_thinking: bool,
    stream: bool,
) -> RequestResult:
    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": stream,
        # SGLang OpenAI-compatible extension: force full-length generations.
        "ignore_eos": True,
    }
    if stream:
        payload["stream_options"] = {"include_usage": True}
    if disable_thinking:
        payload["chat_template_kwargs"] = {"enable_thinking": False}

    t0 = time.perf_counter()
    ttft_s: float | None = None
    try:
        if stream:
            output_chunks: list[str] = []
            prompt_tokens = 0
            completion_tokens = 0
            total_tokens = 0
            finish_reason: str | None = None
            status = 0
            stream_done = False
            async with client.stream(
                "POST",
                url,
                headers=headers,
                json=payload,
                timeout=timeout_s,
            ) as response:
                status = response.status_code
                if status >= 400:
                    body = (await response.aread()).decode("utf-8", "replace")[:300]
                    return RequestResult(
                        ok=False,
                        accepted=False,
                        error=f"HTTP {status}: {body}",
                        latency_s=time.perf_counter() - t0,
                        ttft_s=None,
                        prompt_tokens_usage=0,
                        completion_tokens=0,
                        total_tokens=0,
                        finish_reason=None,
                        constructed_prompt_tokens=constructed_prompt_tokens,
                        output_preview="",
                        stream=True,
                    )

                async for line in response.aiter_lines():
                    if not line or line.startswith(":") or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        stream_done = True
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError as exc:
                        raise RuntimeError("malformed SSE data: expected JSON") from exc

                    if isinstance(chunk, dict) and chunk.get("error"):
                        raise RuntimeError("stream returned an error payload")

                    usage = chunk.get("usage") or {}
                    if usage:
                        prompt_tokens = int(usage.get("prompt_tokens") or prompt_tokens)
                        completion_tokens = int(
                            usage.get("completion_tokens") or completion_tokens
                        )
                        total_tokens = int(usage.get("total_tokens") or total_tokens)

                    choice = (chunk.get("choices") or [{}])[0]
                    if choice.get("finish_reason"):
                        finish_reason = choice.get("finish_reason")
                    delta = choice.get("delta") or {}
                    token_text = _first_token_text(delta)
                    if token_text:
                        if ttft_s is None:
                            # TTFT = first non-empty content/reasoning token, not role/metadata.
                            ttft_s = time.perf_counter() - t0
                        output_chunks.append(token_text)
                if not stream_done:
                    raise RuntimeError("stream ended before [DONE]")

            latency_s = time.perf_counter() - t0
            accepted = completion_tokens == max_tokens
            return RequestResult(
                ok=True,
                accepted=accepted,
                error=None
                if accepted
                else (
                    f"incomplete generation: completion_tokens={completion_tokens} "
                    f"!= max_tokens={max_tokens}"
                ),
                latency_s=latency_s,
                ttft_s=ttft_s,
                prompt_tokens_usage=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens or (prompt_tokens + completion_tokens),
                finish_reason=finish_reason,
                constructed_prompt_tokens=constructed_prompt_tokens,
                output_preview="".join(output_chunks)[:120],
                stream=True,
            )

        response = await client.post(url, headers=headers, json=payload, timeout=timeout_s)
        latency_s = time.perf_counter() - t0
        if response.status_code >= 400:
            return RequestResult(
                ok=False,
                accepted=False,
                error=f"HTTP {response.status_code}: {response.text[:300]}",
                latency_s=latency_s,
                ttft_s=None,
                prompt_tokens_usage=0,
                completion_tokens=0,
                total_tokens=0,
                finish_reason=None,
                constructed_prompt_tokens=constructed_prompt_tokens,
                output_preview="",
                stream=False,
            )
        data = response.json()
        usage = data.get("usage") or {}
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        completion_tokens = int(usage.get("completion_tokens") or 0)
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        total_tokens = int(usage.get("total_tokens") or (prompt_tokens + completion_tokens))
        finish_reason = choice.get("finish_reason")
        content = message.get("content") or ""
        accepted = completion_tokens == max_tokens
        return RequestResult(
            ok=True,
            accepted=accepted,
            error=None
            if accepted
            else f"incomplete generation: completion_tokens={completion_tokens} != max_tokens={max_tokens}",
            latency_s=latency_s,
            # Non-stream cannot observe TTFT; do not fake it with e2e latency.
            ttft_s=None,
            prompt_tokens_usage=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            finish_reason=finish_reason,
            constructed_prompt_tokens=constructed_prompt_tokens,
            output_preview=content[:120],
            stream=False,
        )
    except Exception as exc:  # noqa: BLE001
        return RequestResult(
            ok=False,
            accepted=False,
            error=str(exc),
            latency_s=time.perf_counter() - t0,
            ttft_s=ttft_s,
            prompt_tokens_usage=0,
            completion_tokens=0,
            total_tokens=0,
            finish_reason=None,
            constructed_prompt_tokens=constructed_prompt_tokens,
            output_preview="",
            stream=stream,
        )


async def run(args: argparse.Namespace) -> dict[str, Any]:
    _validate_positive_counts(args)
    base = args.base_url.rstrip("/")
    url = f"{base}/v1/chat/completions"
    health_url = f"{base}/health"
    headers = {
        "Authorization": f"Bearer {args.api_key}",
        "Content-Type": "application/json",
    }

    builder = ExactPromptBuilder(args.tokenizer, enable_thinking=not args.disable_thinking)
    prompts: list[tuple[str, int]] = []
    for i in range(args.requests):
        prompts.append(builder.build(args.prompt_tokens, seed=i + 1))

    # Verify uniqueness and exactness before sending traffic.
    unique = {p for p, _ in prompts}
    if len(unique) != len(prompts):
        raise RuntimeError("constructed prompts were not unique; prefix/cache inflation risk")
    bad = [got for _, got in prompts if got != args.prompt_tokens]
    if bad:
        raise RuntimeError(f"non-exact constructed prompts present: {bad[:5]}")


    async with httpx.AsyncClient(http2=True, verify=not args.insecure, timeout=None) as client:
        health = await client.get(health_url)
        if health.status_code >= 400:
            raise RuntimeError(f"health check failed: HTTP {health.status_code}")

        results_by_index: list[RequestResult | None] = [None] * args.requests
        next_index = 0

        async def worker() -> None:
            nonlocal next_index
            while next_index < args.requests:
                idx = next_index
                next_index += 1
                prompt, constructed = prompts[idx]
                result = await one_request(
                    client,
                    url=url,
                    headers=headers,
                    model=args.model,
                    prompt=prompt,
                    constructed_prompt_tokens=constructed,
                    max_tokens=args.max_tokens,
                    temperature=args.temperature,
                    timeout_s=args.request_timeout,
                    disable_thinking=args.disable_thinking,
                    stream=args.stream,
                )
                results_by_index[idx] = result
                if progress is not None:
                    progress.update(1)

        progress = (
            tqdm_asyncio(total=args.requests, desc="Requests")
            if tqdm_asyncio and not args.quiet
            else None
        )
        wall_t0 = time.perf_counter()
        try:
            async with asyncio.TaskGroup() as task_group:
                for _ in range(min(args.concurrency, args.requests)):
                    task_group.create_task(worker())
        finally:
            if progress is not None:
                progress.close()
        wall_s = time.perf_counter() - wall_t0
        results = [result for result in results_by_index if result is not None]

    accepted = [r for r in results if r.accepted]
    transport_ok = [r for r in results if r.ok]
    rejected_incomplete = [r for r in results if r.ok and not r.accepted]
    failed = [r for r in results if not r.ok]

    latencies = [r.latency_s for r in accepted]
    ttfts = [r.ttft_s for r in accepted if r.ttft_s is not None]
    completion_tokens = sum(r.completion_tokens for r in accepted)
    prompt_tokens = sum(r.prompt_tokens_usage for r in accepted)

    # Compare constructed vs server-reported prompt tokens.
    prompt_mismatch = [
        {
            "constructed": r.constructed_prompt_tokens,
            "usage_prompt_tokens": r.prompt_tokens_usage,
            "delta": r.prompt_tokens_usage - r.constructed_prompt_tokens,
        }
        for r in accepted
        if r.prompt_tokens_usage != r.constructed_prompt_tokens
    ]

    summary = {
        "label": "capacity_ready_stream_ttft" if args.stream else "capacity_ready",
        "tool": "scripts/benchmarks/capacity_bench_openai_compat.py",
        "upstream_reference": "https://github.com/robert-mcdermott/openai-llm-benchmark",
        "notes": [
            "Upstream clone under vendor/openai-llm-benchmark is kept pristine.",
            "Prompt token counts are tokenizer-exact after chat template rendering.",
            "ignore_eos=true is set for SGLang full-length decode.",
            "Only responses with completion_tokens == max_tokens count toward throughput.",
            "end_to_end_latency_s is full request latency.",
            "ttft_s is measured only in --stream mode, on first non-empty content/reasoning token.",
        ],
        "endpoint": url,
        "model": args.model,
        "tokenizer": args.tokenizer,
        "stream": args.stream,
        "requests": args.requests,
        "concurrency": args.concurrency,
        "prompt_tokens_target": args.prompt_tokens,
        "max_tokens": args.max_tokens,
        "ignore_eos": True,
        "disable_thinking": args.disable_thinking,
        "wall_seconds": round(wall_s, 3),
        "transport_successes": len(transport_ok),
        "accepted_full_length": len(accepted),
        "rejected_incomplete": len(rejected_incomplete),
        "failures": len(failed),
        "request_throughput_rps_accepted": round(len(accepted) / wall_s, 3) if wall_s else 0.0,
        "output_tok_per_s_accepted": round(completion_tokens / wall_s, 3) if wall_s else 0.0,
        "total_tok_per_s_accepted": round((prompt_tokens + completion_tokens) / wall_s, 3)
        if wall_s
        else 0.0,
        "end_to_end_latency_s_accepted": latency_summary(latencies),
        # Backward-compatible alias; explicitly end-to-end, not TTFT.
        "latency_s_accepted": latency_summary(latencies),
        "ttft_s_accepted": latency_summary(ttfts) if args.stream else None,
        "ttft_samples": len(ttfts),
        "avg_usage_prompt_tokens_accepted": round(statistics.fmean([r.prompt_tokens_usage for r in accepted]), 1)
        if accepted
        else None,
        "avg_completion_tokens_accepted": round(statistics.fmean([r.completion_tokens for r in accepted]), 1)
        if accepted
        else None,
        "prompt_token_mismatches_vs_constructed": {
            "count": len(prompt_mismatch),
            "samples": prompt_mismatch[:5],
        },
        "incomplete_samples": [
            {
                "completion_tokens": r.completion_tokens,
                "finish_reason": r.finish_reason,
                "error": r.error,
            }
            for r in rejected_incomplete[:5]
        ],
        "failure_samples": [r.error for r in failed[:5]],
        "sample_output": next((r.output_preview for r in accepted if r.output_preview), ""),
        "capacity_metrics_valid": (
            len(accepted) == args.requests
            and not prompt_mismatch
            and (not args.stream or len(ttfts) == len(accepted))
        ),
        "requests_detail": [asdict(r) for r in results] if args.save_requests else None,
    }
    return summary


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base-url", required=True, help="http(s)://host[:port] root (no /v1)")
    p.add_argument("--api-key", required=True)
    p.add_argument("--model", default="Qwen/Qwen3.6-35B-A3B-FP8")
    p.add_argument(
        "--tokenizer",
        default="Qwen/Qwen3.6-35B-A3B-FP8",
        help="HF tokenizer id used to construct/verify exact prompt token counts",
    )
    p.add_argument("--requests", type=_positive_int, default=16)
    p.add_argument("--concurrency", type=_positive_int, default=1)
    p.add_argument(
        "--prompt-tokens",
        type=_positive_int,
        default=128,
        help="Exact rendered chat-template prompt token count",
    )
    p.add_argument("--max-tokens", type=_positive_int, default=128)
    p.add_argument("--temperature", type=float, default=0.0)
    p.add_argument("--request-timeout", type=float, default=300.0)
    p.add_argument("--insecure", action="store_true")
    p.add_argument("--disable-thinking", action="store_true", default=True)
    p.add_argument("--enable-thinking", action="store_true", help="Override and allow thinking")
    p.add_argument(
        "--stream",
        action="store_true",
        help="Enable streaming and measure TTFT on first non-empty content/reasoning token",
    )
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--save-requests", action="store_true")
    p.add_argument("--output", type=Path, default=None)
    args = p.parse_args()
    if args.enable_thinking:
        args.disable_thinking = False
    return args


def main() -> None:
    args = parse_args()
    summary = asyncio.run(run(args))
    printable = {k: v for k, v in summary.items() if k != "requests_detail"}
    print(json.dumps(printable, indent=2))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
