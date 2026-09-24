"""AI provider abstraction.

Every provider speaks the same tiny interface, so the brain never knows or cares
which vendor answered:

    provider.chat(messages, system=..., temperature=..., max_tokens=...) -> str

Implemented:

* :class:`OpenAICompatProvider` - OpenAI, Groq, OpenRouter, Together, Cerebras,
  Mistral, DeepSeek, LM Studio, **and Ollama** (all expose ``/chat/completions``)
* :class:`GeminiProvider` - Google Gemini's native REST shape
* :class:`OfflineProvider` - the local rule engine, always available

Adding another vendor is one class + one entry in ``config.settings``.
"""

from __future__ import annotations

import json as jsonlib
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Mapping, Optional, Sequence

from ai.http import HttpError, request_json
from config.settings import ProviderConfig

Message = Dict[str, str]


# --------------------------------------------------------------------------- #
# errors
# --------------------------------------------------------------------------- #
class ProviderError(Exception):
    """Base class for provider failures (carries which provider failed)."""

    def __init__(self, slug: str, message: str) -> None:
        super().__init__(f"{slug}: {message}")
        self.slug = slug
        self.message = message


class ProviderUnavailable(ProviderError):
    """Network problem, timeout, 5xx or the local daemon is not running."""


class ProviderRateLimited(ProviderError):
    """Quota exhausted / too many requests.  Cool down then move on."""


class ProviderAuthError(ProviderError):
    """Missing, invalid or revoked credentials."""


class ProviderBadResponse(ProviderError):
    """Answered, but we could not use the answer."""


class AllProvidersFailed(ProviderError):
    """Every provider in the chain failed; carries the per-provider reasons."""

    def __init__(self, failures: Sequence[tuple]) -> None:
        detail = "; ".join(f"{slug}: {reason}" for slug, reason in failures) or "no providers"
        super().__init__("all", detail)
        self.failures = list(failures)


def classify_http_error(slug: str, exc: Exception) -> ProviderError:
    """Turn an :class:`HttpError` (or anything else) into a provider error."""
    if isinstance(exc, ProviderError):
        return exc
    if isinstance(exc, HttpError):
        body = (exc.body or "").lower()
        status = exc.status
        if status in (401, 403) or "api key not valid" in body or "invalid_api_key" in body:
            return ProviderAuthError(slug, "credentials rejected")
        if status == 429 or "quota" in body or "rate limit" in body:
            return ProviderRateLimited(slug, "rate limited or out of quota")
        if status == 404 and "model" in body:
            return ProviderBadResponse(slug, "model not found")
        if status is not None and 400 <= status < 500:
            return ProviderBadResponse(slug, f"request rejected ({status})")
        return ProviderUnavailable(slug, str(exc))
    return ProviderUnavailable(slug, str(exc))


# --------------------------------------------------------------------------- #
# base class
# --------------------------------------------------------------------------- #
class AIProvider(ABC):
    kind = "abstract"

    def __init__(self, config: ProviderConfig) -> None:
        self.config = config

    # -- identity ----------------------------------------------------------
    @property
    def slug(self) -> str:
        return self.config.slug

    @property
    def label(self) -> str:
        return self.config.label

    @property
    def model(self) -> str:
        return self.config.model

    @property
    def local(self) -> bool:
        return self.config.local

    def describe(self) -> str:
        return f"{self.label} ({self.model})" if self.model else self.label

    # -- api ---------------------------------------------------------------
    @abstractmethod
    def chat(
        self,
        messages: Sequence[Message],
        system: Optional[str] = None,
        temperature: float = 0.4,
        max_tokens: int = 900,
        timeout: Optional[float] = None,
    ) -> str:
        """Return the assistant's text for ``messages`` or raise ProviderError."""

    def available(self) -> bool:
        """Cheap pre-flight check (only Ollama overrides this)."""
        return self.config.configured

    def health(self) -> Dict[str, Any]:
        return {"slug": self.slug, "available": self.available(), "model": self.model}


# --------------------------------------------------------------------------- #
# OpenAI compatible
# --------------------------------------------------------------------------- #
class OpenAICompatProvider(AIProvider):
    kind = "openai"

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        if self.slug == "openrouter":
            headers["HTTP-Referer"] = "https://github.com/WizardAyush0099/JARVIS"
            headers["X-Title"] = "JARVIS"
        return headers

    def chat(
        self,
        messages: Sequence[Message],
        system: Optional[str] = None,
        temperature: float = 0.4,
        max_tokens: int = 900,
        timeout: Optional[float] = None,
    ) -> str:
        payload_messages: List[Message] = []
        if system:
            payload_messages.append({"role": "system", "content": system})
        payload_messages.extend({"role": m.get("role", "user"), "content": m.get("content", "")} for m in messages)

        url = f"{self.config.base_url}/chat/completions"
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": payload_messages,
            "temperature": float(temperature),
            "stream": False,
        }
        # Some OpenAI-compatible servers reject max_tokens; only send it when sane.
        if max_tokens and max_tokens > 0:
            payload["max_tokens"] = int(max_tokens)

        try:
            data = request_json(
                url,
                payload=payload,
                headers=self._headers(),
                timeout=timeout or self.config.timeout,
                retries=0,  # the manager owns retry/fallback policy
            )
        except Exception as exc:
            raise classify_http_error(self.slug, exc) from exc

        if not isinstance(data, Mapping):
            raise ProviderBadResponse(self.slug, "unexpected response shape")
        choices = data.get("choices") or []
        if not choices:
            error = data.get("error")
            if isinstance(error, Mapping):
                message = str(error.get("message", "unknown error")).lower()
                if "quota" in message or "rate" in message:
                    raise ProviderRateLimited(self.slug, str(error.get("message")))
                if "key" in message:
                    raise ProviderAuthError(self.slug, str(error.get("message")))
                raise ProviderBadResponse(self.slug, str(error.get("message")))
            raise ProviderBadResponse(self.slug, "no choices in response")

        message = choices[0].get("message") or {}
        content = message.get("content") or ""
        if isinstance(content, list):  # some gateways return content parts
            content = " ".join(
                part.get("text", "") for part in content if isinstance(part, Mapping)
            )
        text = str(content).strip()
        if not text:
            raise ProviderBadResponse(self.slug, "empty completion")
        return text


class OllamaProvider(OpenAICompatProvider):
    """Local Ollama.  Works with no internet at all."""

    def available(self) -> bool:
        try:
            request_json(
                f"{self.config.base_url}/models",
                headers={"Authorization": "Bearer ollama"},
                timeout=2.0,
                retries=0,
            )
            return True
        except Exception:
            return False


# --------------------------------------------------------------------------- #
# Google Gemini (native REST)
# --------------------------------------------------------------------------- #
class GeminiProvider(AIProvider):
    kind = "gemini"

    def chat(
        self,
        messages: Sequence[Message],
        system: Optional[str] = None,
        temperature: float = 0.4,
        max_tokens: int = 900,
        timeout: Optional[float] = None,
    ) -> str:
        contents = []
        for message in messages:
            role = message.get("role", "user")
            role = "model" if role == "assistant" else "user"
            contents.append({"role": role, "parts": [{"text": message.get("content", "")}]})

        payload: Dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "temperature": float(temperature),
                "maxOutputTokens": int(max_tokens),
            },
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}

        url = f"{self.config.base_url}/models/{self.model}:generateContent"
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["x-goog-api-key"] = self.config.api_key

        try:
            data = request_json(
                url, payload=payload, headers=headers,
                timeout=timeout or self.config.timeout, retries=0,
            )
        except Exception as exc:
            raise classify_http_error(self.slug, exc) from exc

        if not isinstance(data, Mapping):
            raise ProviderBadResponse(self.slug, "unexpected response shape")
        candidates = data.get("candidates") or []
        if not candidates:
            feedback = data.get("promptFeedback") or {}
            if feedback:
                raise ProviderBadResponse(self.slug, f"blocked: {feedback.get('blockReason', 'unknown')}")
            raise ProviderBadResponse(self.slug, "no candidates in response")

        parts = ((candidates[0].get("content") or {}).get("parts")) or []
        text = " ".join(str(part.get("text", "")) for part in parts if isinstance(part, Mapping)).strip()
        if not text:
            reason = candidates[0].get("finishReason", "empty")
            raise ProviderBadResponse(self.slug, f"empty completion ({reason})")
        return text


# --------------------------------------------------------------------------- #
# offline
# --------------------------------------------------------------------------- #
class OfflineProvider(AIProvider):
    """The local rule engine - zero network, zero quota, always present."""

    kind = "offline"

    def __init__(self, config: ProviderConfig, engine: Any = None) -> None:
        super().__init__(config)
        self._engine = engine

    @property
    def engine(self) -> Any:
        if self._engine is None:  # imported lazily to keep import graph flat
            from ai.offline import OfflineEngine

            self._engine = OfflineEngine()
        return self._engine

    def chat(
        self,
        messages: Sequence[Message],
        system: Optional[str] = None,
        temperature: float = 0.4,
        max_tokens: int = 900,
        timeout: Optional[float] = None,
    ) -> str:
        text = ""
        for message in reversed(list(messages)):
            if message.get("role") == "user":
                text = str(message.get("content", ""))
                break
        answer = self.engine.answer(text)
        if not answer:
            answer = (
                "I'm running offline right now, so I can only handle time, dates, "
                "calculations, unit conversions, system status and anything I already "
                "remember. Add an API key (or start Ollama) and I'll be fully back."
            )
        return answer


def build_provider(config: ProviderConfig) -> AIProvider:
    """Factory: pick the right implementation for a resolved config."""
    if config.kind == "offline":
        return OfflineProvider(config)
    if config.kind == "gemini":
        return GeminiProvider(config)
    if config.slug == "ollama":
        return OllamaProvider(config)
    return OpenAICompatProvider(config)


def messages_from_pairs(pairs: Sequence[Sequence[str]]) -> List[Message]:
    """Tiny helper used by tests and by legacy ChatLog migration."""
    return [{"role": role, "content": content} for role, content in pairs]


def dumps_safe(payload: Any) -> str:
    try:
        return jsonlib.dumps(payload)
    except Exception:  # pragma: no cover - defensive
        return str(payload)


__all__ = [
    "AIProvider",
    "AllProvidersFailed",
    "GeminiProvider",
    "Message",
    "OfflineProvider",
    "OllamaProvider",
    "OpenAICompatProvider",
    "ProviderAuthError",
    "ProviderBadResponse",
    "ProviderError",
    "ProviderRateLimited",
    "ProviderUnavailable",
    "build_provider",
    "classify_http_error",
    "messages_from_pairs",
]
