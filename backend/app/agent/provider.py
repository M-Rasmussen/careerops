"""LLM providers. The agent only depends on `ModelProvider.complete`; providers never run tools."""

import http.client
import json
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.config import AISettings


class ProviderError(Exception):
    """A provider call failed. `kind` is a stable category stored on the run."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON text from the model; validated by the tool registry, never eval'd


@dataclass(frozen=True)
class ModelReply:
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int | None = None
    output_tokens: int | None = None


class ModelProvider(Protocol):
    name: str
    model: str

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelReply: ...


Opener = Callable[..., Any]
MAX_RESPONSE_BYTES = 2_000_000


class OpenAICompatibleProvider:
    """POST {base_url}/chat/completions with function tools (OpenAI, Ollama, OpenRouter, vLLM, ...)."""

    name = "openai"

    def __init__(self, settings: AISettings, opener: Opener = urllib.request.urlopen) -> None:
        self.model = settings.model
        self._settings = settings
        self._open = opener

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelReply:
        s = self._settings
        body = json.dumps(
            {
                "model": s.model,
                "messages": messages,
                "tools": tools,
                "tool_choice": "auto",
                s.token_param: s.max_output_tokens,
            }
        ).encode()
        headers = {"Content-Type": "application/json"}
        if s.api_key:
            headers["Authorization"] = f"Bearer {s.api_key}"
        req = urllib.request.Request(f"{s.base_url}/chat/completions", data=body, headers=headers)
        timed_out = ProviderError("timeout", f"Provider timed out after {s.timeout_seconds}s")
        # timeout applies to connect and to each socket read (not total time); max_steps bounds the run.
        try:
            with self._open(req, timeout=s.timeout_seconds) as res:
                raw = res.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            # Status only: response bodies can echo request details we don't want in logs.
            raise ProviderError("provider_http_error", f"Provider returned HTTP {exc.code}") from None
        except TimeoutError:
            raise timed_out from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise timed_out from None
            raise ProviderError("provider_unreachable", "Could not reach the AI provider") from None
        except (OSError, http.client.HTTPException):
            # Connection reset, TLS failure, or a truncated body mid-read.
            raise ProviderError("provider_connection_error", "Connection to the AI provider failed") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ProviderError("invalid_response", "Provider response too large")
        return parse_chat_completion(raw)


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _arguments(value: object) -> str:
    # Spec says a JSON string; some compatible servers send an object. Either way the tool registry validates it.
    if value is None or isinstance(value, str):
        return value or "{}"
    if isinstance(value, dict):
        return json.dumps(value)
    raise TypeError("tool arguments must be a JSON string or object")


def parse_chat_completion(raw: bytes) -> ModelReply:
    try:
        data = json.loads(raw)
        message = data["choices"][0]["message"]
        calls = [
            ToolCall(
                id=str(c.get("id") or f"call_{i}"),  # some servers omit ids
                name=str(c["function"]["name"]),
                arguments=_arguments(c["function"].get("arguments")),
            )
            for i, c in enumerate(message.get("tool_calls") or [])
        ]
        content = message.get("content")
        usage = data.get("usage") or {}
        return ModelReply(
            content=content if isinstance(content, str) else None,
            tool_calls=calls,
            input_tokens=_count(usage.get("prompt_tokens")),
            output_tokens=_count(usage.get("completion_tokens")),
        )
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        raise ProviderError("invalid_response", "Provider returned a malformed response") from None


class ScriptedProvider:
    """Deterministic fake for tests: returns (or raises) the scripted replies in order. Never networks."""

    name = "fake"
    model = "scripted"

    def __init__(self, replies: Sequence[ModelReply | Exception]) -> None:
        self._replies = list(replies)
        self.calls: list[list[dict[str, Any]]] = []

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelReply:
        self.calls.append([dict(m) for m in messages])
        if not self._replies:
            raise ProviderError("invalid_response", "Scripted provider ran out of replies")
        reply = self._replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def build_provider(settings: AISettings) -> ModelProvider | None:
    """The configured provider, or None when AI is not configured."""
    return OpenAICompatibleProvider(settings) if settings.configured else None
