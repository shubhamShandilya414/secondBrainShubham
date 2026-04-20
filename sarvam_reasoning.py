"""Sarvam AI chat-completion helper for Meera's reasoning layer."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Sequence

from urllib.request import Request, urlopen


class SarvamReasoningError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReasoningResult:
    content: str
    reasoning_content: str = ""


def reason_with_sarvam(
    messages: Sequence[dict[str, str]],
    *,
    model: str | None = None,
    reasoning_effort: str = "high",
    temperature: float = 0.2,
    max_tokens: int = 256,
    timeout: int = 45,
) -> ReasoningResult:
    api_key = os.environ.get("SARVAM_API_KEY", "").strip()
    if not api_key:
        raise SarvamReasoningError("SARVAM_API_KEY is not set")

    model_name = (model or os.environ.get("SARVAM_REASONING_MODEL", "").strip() or "sarvam-105b")
    payload = {
        "model": model_name,
        "messages": list(messages),
        "reasoning_effort": reasoning_effort,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    request = Request(
        "https://api.sarvam.ai/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))
    choices = data.get("choices") or []
    if not choices:
        raise SarvamReasoningError("Sarvam returned no choices")

    message = choices[0].get("message") or {}
    content = str(message.get("content", "")).strip()
    reasoning_content = str(message.get("reasoning_content", "")).strip()
    if not content:
        raise SarvamReasoningError("Sarvam returned an empty response")

    return ReasoningResult(content=content, reasoning_content=reasoning_content)
