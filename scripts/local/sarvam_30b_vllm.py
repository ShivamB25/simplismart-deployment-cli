"""Launch a local OpenAI-compatible vLLM server for Sarvam-30B.

Requires Sarvam's patched vLLM installation or the Sarvam vLLM fork, which
registers the sarvam_moe architecture before this launcher is run.
"""

from __future__ import annotations

import os
import shlex
import shutil

MODEL_ID = "sarvamai/sarvam-30b"


def main() -> None:
    vllm = shutil.which("vllm")
    if vllm is None:
        raise RuntimeError("vllm is not installed or is not on PATH")

    environment = os.environ.copy()
    environment.setdefault("HIP_VISIBLE_DEVICES", "0")

    command = [
        vllm,
        "serve",
        MODEL_ID,
        "--host",
        "0.0.0.0",
        "--port",
        "8001",
        "--tensor-parallel-size",
        "1",
        "--dtype",
        "bfloat16",
        "--max-model-len",
        "2048",
        "--gpu-memory-utilization",
        "0.80",
        "--max-num-seqs",
        "16",
        "--trust-remote-code",
    ]

    print(f"Launching: {shlex.join(command)}", flush=True)
    os.execvpe(vllm, command, environment)


if __name__ == "__main__":
    main()
