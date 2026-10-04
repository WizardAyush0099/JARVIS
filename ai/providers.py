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
import re
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Mapping, Optional, Sequence

from ai.http import HttpError, request_json
from config.settings import ProviderConfig
from core.logging_setup import get_logger

log = get_logger("providers")

Message = Dict[str, str]

#: Phrases vendors use when the model id is unknown or has been retired.  A
#: retired id is the single most common reason a perfectly healthy provider
#: looks "unavailable", so it is worth detecting precisely rather than lumping
#: in with every other 4xx.
MISSING_MODEL_HINTS = (
    "model not found",
    "model_not_found",
    "does not exist",
    "not exist",
    "no such model",
    "unknown model",
    "invalid model",
    "unsupported model",
    "is not supported",
    "no longer available",
    "has been retired",
    "is retired",
    "not available for",
)

#: Model ids that are never a chat model - embeddings, audio, guard rails.
NON_CHAT_MARKERS = (
    "embed", "whisper", "guard", "safeguard", "tts", "clip", "lyria", "transcribe",
    "audio", "realtime", "moderation", "rerank", "onnx", "vision-model",
)

#: Markers of a small, fast model - the sensible default on a Raspberry Pi.
FAST_MODEL_MARKERS = ("flash", "lite", "mini", "small", "turbo", "instant")

#: How many model ids to try before giving a provider up.  A vendor's model
#: list can be stale - Google still advertises ``gemini-2.5-flash`` while the
#: generateContent endpoint rejects it - so the first substitute is sometimes
#: dead too, and stopping there would report a perfectly good provider broken.
MAX_MODEL_SWITCHES = 3


def _tokens(model: str) -> List[str]:
    """Split a model id into comparable words ("gemini-3.5-flash" -> 3 words)."""
    return [t for t in re.split(r"[^a-z0-9]+", (model or "").lower()) if t]


def _matches_marker(model: str, markers: Sequence[str]) -> bool:
    """True when any marker is a whole word of the id.

    Deliberately *not* a substring test: "gemini" contains "mini", so a
    substring check classified every Gemini model as a small one and then
    picked the wrong replacement.
    """
    words = _tokens(model)
    return any(
        word == marker or word.startswith(marker)
        for word in words
        for marker in markers
    )


def is_fast_model(model: str) -> bool:
    """Small and quick - the sensible default on a Raspberry Pi."""
    return _matches_marker(model, FAST_MODEL_MARKERS)


def is_non_chat_model(model: str) -> bool:
    """Embeddings, audio and guard rails can never hold a conversation."""
    return _matches_marker(model, NON_CHAT_MARKERS)


def looks_like_missing_model(error: Any) -> bool:
    """Did this provider reject us because the model id is gone?"""
    message = str(getattr(error, "message", "") or error).lower()
    if "model" not in message:
        return False
    return any(hint in message for hint in MISSING_MODEL_HINTS)


def pick_model(
    available: Sequence[str], preferred: str, exclude: Sequence[str] = ()
) -> Optional[str]:
    """Choose the best available substitute for a retired ``preferred`` id.

    Keeps the vendor's intent when we can (same family), and otherwise prefers
    a small, fast model - a Pi assistant wants the quick answer, not the
    largest one the vendor sells.  ``exclude`` holds ids already found to be
    dead, so a stale vendor listing cannot send us round in circles.
    """
    blocked = {str(item).lower() for item in exclude}
    ids = [m for m in available if m and m.lower() not in blocked]
    if not ids:
        return None
    if preferred and preferred in ids:
        return preferred
    stem = (preferred or "").split("/")[-1].split("-")[0].lower()
    related = [m for m in ids if stem and stem in m.lower()]
    pool = related or ids
    fast = [m for m in pool if is_fast_model(m)]
    return (fast or pool)[0]


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


class ProviderTimeout(ProviderUnavailable):
    """The provider accepted the request and then never answered.

    Separate from the other transient failures on purpose: a timeout has already
    consumed its whole budget once, so retrying it only multiplies the wait.  A
    connection that is refused fails instantly and *is* worth retrying; a request
    that hangs for the full timeout is not.
    """


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
    # ``socket.timeout`` is ``TimeoutError`` on every supported Python, so this
    # catches the transport layer as well as the HttpError below.
    if isinstance(exc, TimeoutError):
        return ProviderTimeout(slug, "the provider timed out")
    if isinstance(exc, HttpError) and exc.status is None and "timed out" in f"{exc.message} {exc}".lower():
        return ProviderTimeout(slug, "the provider timed out")
    if isinstance(exc, HttpError):
        body = (exc.body or "").lower()
        status = exc.status
        if status in (401, 403) or "api key not valid" in body or "invalid_api_key" in body:
            return ProviderAuthError(slug, "credentials rejected")
        if status == 429 or "quota" in body or "rate limit" in body:
            return ProviderRateLimited(slug, "rate limited or out of quota")
        if status == 404 and "model" in body:
            return ProviderBadResponse(slug, "model not found")
        # Vendors signal a retired model id with all sorts of statuses (400,
        # 404, 422...), so trust the wording rather than the status code.
        if "model" in body and any(hint in body for hint in MISSING_MODEL_HINTS):
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
        #: set when the vendor retired the configured model and we moved on
        self._model: Optional[str] = None

    # -- identity ----------------------------------------------------------
    @property
    def slug(self) -> str:
        return self.config.slug

    @property
    def label(self) -> str:
        return self.config.label

    @property
    def model(self) -> str:
        return self._model or self.config.model

    @property
    def local(self) -> bool:
        return self.config.local

    def describe(self) -> str:
        return f"{self.label} ({self.model})" if self.model else self.label

    # -- self-healing ------------------------------------------------------
    def list_models(self) -> List[str]:
        """Ids this vendor serves right now (empty when it will not say)."""
        return []

    def chat_models(self) -> List[str]:
        """The subset of :meth:`list_models` that can actually hold a chat."""
        return [m for m in self.list_models() if not is_non_chat_model(m)]

    def adopt_available_model(self, exclude: Sequence[str] = ()) -> bool:
        """Switch to a model this vendor really serves.  True if one was found.

        Called only after a missing-model rejection, so the extra request is
        worth it: the alternative is losing the provider for the whole run.
        ``exclude`` lists ids already found to be dead - a vendor's own model
        listing is not always a promise that the model still answers.
        """
        try:
            available = self.chat_models()
        except Exception as exc:  # noqa: BLE001 - discovery is best effort
            log.debug("%s: could not list models (%s)", self.slug, exc)
            return False
        chosen = pick_model(available, self.config.model, exclude=exclude)
        if not chosen:
            return False
        log.warning(
            "provider %s no longer serves %r; switching to %r",
            self.slug, self.config.model, chosen,
        )
        self._model = chosen
        return True

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

    def list_models(self) -> List[str]:
        """Ask this endpoint which model ids it currently serves."""
        data = request_json(
            f"{self.config.base_url}/models",
            headers=self._headers(),
            timeout=15.0,
            retries=0,
        )
        if not isinstance(data, Mapping):
            return []
        return [
            str(entry.get("id"))
            for entry in (data.get("data") or [])
            if isinstance(entry, Mapping) and entry.get("id")
        ]

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

    def list_models(self) -> List[str]:
        """Gemini's own model list, restricted to ones that can generate text."""
        headers = {"Content-Type": "application/json"}
        url = f"{self.config.base_url}/models"
        if self.config.api_key:
            headers["x-goog-api-key"] = self.config.api_key
        data = request_json(url, headers=headers, timeout=15.0, retries=0)
        if not isinstance(data, Mapping):
            return []
        names: List[str] = []
        for entry in data.get("models") or []:
            if not isinstance(entry, Mapping):
                continue
            methods = entry.get("supportedGenerationMethods") or []
            if "generateContent" not in methods:
                continue
            name = str(entry.get("name", ""))
            if name:
                names.append(name.split("/")[-1])
        return names

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
        # `reply`, not `answer`: as a model fallback this must never execute a
        # tool from whatever prompt it was handed (see OfflineEngine.reply).
        answer = self.engine.reply(text)
        if not answer:
            # Deliberately short and quiet.  The long "add an API key" pitch used
            # to be returned here, which meant every single turn that fell back
            # to the local engine repeated it - including turns where real tool
            # results were waiting.  The one-time notice now lives in the planner.
            answer = (
                "I don't have an online model available for that right now, and I "
                "don't have a local answer for it either."
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
    "MAX_MODEL_SWITCHES",
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
    "is_fast_model",
    "is_non_chat_model",
    "looks_like_missing_model",
    "messages_from_pairs",
    "pick_model",
]
