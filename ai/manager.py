"""Provider manager - the "never dead" part of the brain.

Nothing in the rest of JARVIS talks to a vendor directly; everything goes through
:meth:`ProviderManager.chat`, which:

* walks the configured provider chain in priority order
* retries *transient* failures a bounded number of times (the old project had no
  retry at all, so one hiccup meant a dead assistant)
* classifies failures - rate limit, bad key, network, malformed answer
* puts a failing provider into a cooldown so the next request skips straight to a
  healthy one instead of waiting through the same timeout
* falls back to the always-present offline engine instead of crashing
* publishes what happened so the GUI can show it honestly

Keys are never logged; :func:`core.logging_setup.redact` is a second belt.
"""

from __future__ import annotations

import json as jsonlib
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ai.providers import (
    AIProvider,
    AllProvidersFailed,
    OfflineProvider,
    ProviderAuthError,
    ProviderBadResponse,
    ProviderError,
    ProviderRateLimited,
    ProviderUnavailable,
    build_provider,
    classify_http_error,
)
from config.settings import Settings
from core.logging_setup import get_logger

log = get_logger("ai")

COOLDOWN_RATE_LIMIT = 90.0
COOLDOWN_UNAVAILABLE = 25.0
COOLDOWN_AUTH = 900.0
COOLDOWN_BAD_RESPONSE = 5.0
COOLDOWN_MAX = 900.0


# --------------------------------------------------------------------------- #
# health bookkeeping
# --------------------------------------------------------------------------- #
@dataclass
class ProviderHealth:
    slug: str
    label: str
    model: str
    local: bool = False
    status: str = "ready"  # ready | cooling | error
    calls: int = 0
    successes: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    last_error: str = ""
    last_used: float = 0.0
    last_latency_ms: int = 0
    cooldown_until: float = 0.0

    def to_dict(self, now: Optional[float] = None) -> Dict[str, Any]:
        now = time.time() if now is None else now
        cooldown = max(0.0, round(self.cooldown_until - now, 1))
        if self.status == "not configured":
            status = "not configured"
        elif cooldown > 0:
            status = "cooling"
        elif self.status == "error" and self.last_error:
            status = "degraded"
        else:
            status = self.status
        return {
            "slug": self.slug,
            "label": self.label,
            "model": self.model,
            "local": self.local,
            "status": status,
            "calls": self.calls,
            "successes": self.successes,
            "failures": self.failures,
            "last_error": self.last_error,
            "last_latency_ms": self.last_latency_ms,
            "cooldown_s": cooldown,
        }


# --------------------------------------------------------------------------- #
# JSON extraction (models wrap JSON in prose and code fences)
# --------------------------------------------------------------------------- #
_FENCE = re.compile(r"```(?:json|JSON)?\s*(.+?)```", re.S)


def extract_json(text: str) -> Optional[Any]:
    """Pull the first valid JSON object/array out of model output."""
    if not text:
        return None
    candidates: List[str] = []
    for match in _FENCE.finditer(text):
        candidates.append(match.group(1).strip())
    candidates.append(text.strip())

    decoder = jsonlib.JSONDecoder()
    for candidate in candidates:
        try:
            return jsonlib.loads(candidate)
        except ValueError:
            pass
        for index, char in enumerate(candidate):
            if char in "[{":
                try:
                    value, _ = decoder.raw_decode(candidate[index:])
                    return value
                except ValueError:
                    continue
    return None


# --------------------------------------------------------------------------- #
# manager
# --------------------------------------------------------------------------- #
class ProviderManager:
    def __init__(self, settings: Settings, events: Any = None, offline_engine: Any = None) -> None:
        self.settings = settings
        self.events = events
        self._lock = threading.RLock()
        self._providers: List[AIProvider] = []
        self._health: Dict[str, ProviderHealth] = {}
        self._offline_engine = offline_engine
        self.last_provider: str = ""
        self._build()

    # -- construction ------------------------------------------------------
    def _build(self) -> None:
        self._providers = []
        self._health = {}
        for config in self.settings.ai.providers:
            health = ProviderHealth(
                slug=config.slug, label=config.label, model=config.model, local=config.local
            )
            if not config.usable:
                # Keep it visible in the status panel so the user can see what to
                # add, but never spend a network timeout on a provider with no key.
                health.status = "not configured"
                health.last_error = "no API key set"
                self._health[config.slug] = health
                continue
            provider: AIProvider = (
                OfflineProvider(config, self._offline_engine)
                if config.kind == "offline"
                else build_provider(config)
            )
            self._providers.append(provider)
            self._health[config.slug] = health
        if not any(p.slug != "offline" for p in self._providers):
            log.warning(
                "no online AI provider is configured - JARVIS is running on the offline engine"
            )

    @property
    def providers(self) -> List[AIProvider]:
        return list(self._providers)

    @property
    def provider_names(self) -> List[str]:
        return [p.slug for p in self._providers]

    @property
    def online_providers(self) -> List[str]:
        return [p.slug for p in self._providers if not p.local]

    def has_online_provider(self) -> bool:
        """True when at least one configured provider is not cooling down."""
        now = time.time()
        with self._lock:
            return any(
                p.slug != "offline" and self._health[p.slug].cooldown_until <= now
                for p in self._providers
            )

    # -- ordering ----------------------------------------------------------
    def _ordered(self, now: float) -> List[Tuple[AIProvider, ProviderHealth]]:
        with self._lock:
            entries = [(p, self._health[p.slug]) for p in self._providers]
        online = [(p, h) for p, h in entries if p.slug != "offline"]
        offline = [(p, h) for p, h in entries if p.slug == "offline"]

        ready = [(p, h) for p, h in online if h.cooldown_until <= now]
        if not ready and online:
            # Everything is cooling down. Rather than silently going offline,
            # retry the provider that recovers soonest - a stale cooldown is a
            # worse failure mode than one extra timeout.
            ready = [min(online, key=lambda item: item[1].cooldown_until)]
        return ready + offline

    # -- bookkeeping -------------------------------------------------------
    def _mark_success(self, health: ProviderHealth, latency: float) -> None:
        with self._lock:
            health.calls += 1
            health.successes += 1
            health.consecutive_failures = 0
            health.last_error = ""
            health.last_used = time.time()
            health.last_latency_ms = int(latency * 1000)
            health.cooldown_until = 0.0
            health.status = "ready"

    def _mark_failure(self, health: ProviderHealth, error: ProviderError, latency: float) -> None:
        if isinstance(error, ProviderRateLimited):
            base = COOLDOWN_RATE_LIMIT
        elif isinstance(error, ProviderAuthError):
            base = COOLDOWN_AUTH
        elif isinstance(error, ProviderBadResponse):
            base = COOLDOWN_BAD_RESPONSE
        else:
            base = COOLDOWN_UNAVAILABLE

        with self._lock:
            health.calls += 1
            health.failures += 1
            health.consecutive_failures += 1
            health.last_error = error.message[:300]
            health.last_latency_ms = int(latency * 1000)
            penalty = min(COOLDOWN_MAX, base * min(4, health.consecutive_failures))
            health.cooldown_until = time.time() + penalty
            health.status = "error"
        log.warning(
            "provider %s failed (%s); cooling down %.0fs",
            health.slug,
            type(error).__name__,
            penalty,
        )

    # -- the one method everything uses ------------------------------------
    def chat(
        self,
        messages: Sequence[Mapping[str, str]],
        system: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        timeout: Optional[float] = None,
        task: str = "chat",
    ) -> str:
        """Return a model answer, trying every provider until one works."""
        temperature = self.settings.ai.temperature if temperature is None else temperature
        max_tokens = self.settings.ai.max_tokens if max_tokens is None else max_tokens
        timeout = self.settings.ai.timeout if timeout is None else timeout
        payload = [dict(m) for m in messages]

        order = self._ordered(time.time())
        if not order:
            raise AllProvidersFailed([("none", "no providers configured")])

        failures: List[Tuple[str, str]] = []
        for provider, health in order:
            attempts = 1 + (0 if provider.local else max(0, self.settings.ai.max_retries))
            for attempt in range(attempts):
                started = time.time()
                try:
                    text = provider.chat(
                        payload,
                        system=system,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        timeout=timeout,
                    )
                    if not text or not text.strip():
                        raise ProviderBadResponse(provider.slug, "empty answer")
                    self._mark_success(health, time.time() - started)
                    self.last_provider = provider.slug
                    return text.strip()
                except Exception as raw_error:  # noqa: BLE001 - classified below
                    error = classify_http_error(provider.slug, raw_error)
                    is_transient = isinstance(error, ProviderUnavailable)
                    if is_transient and attempt + 1 < attempts:
                        log.debug(
                            "%s transient failure (%s); retrying (%d/%d)",
                            provider.slug,
                            error.message,
                            attempt + 1,
                            attempts,
                        )
                        time.sleep(min(2.0, 0.4 * (2**attempt)))
                        continue
                    self._mark_failure(health, error, time.time() - started)
                    failures.append((provider.slug, error.message))
                    if self.events is not None and provider.slug != "offline":
                        self.events.publish(
                            "provider",
                            message=f"{provider.label} unavailable - trying the next provider",
                            provider=provider.slug,
                            error=error.message,
                        )
                    break

        log.error("all providers failed: %s", failures)
        raise AllProvidersFailed(failures)

    def chat_json(
        self,
        messages: Sequence[Mapping[str, str]],
        system: Optional[str] = None,
        retry_on_invalid: bool = True,
        **kwargs: Any,
    ) -> Optional[Any]:
        """Ask for JSON and parse it, retrying once with a stricter reminder."""
        text = self.chat(messages, system=system, **kwargs)
        parsed = extract_json(text)
        if parsed is not None:
            return parsed
        if not retry_on_invalid:
            return None

        reminder = (
            (system or "")
            + "\n\nIMPORTANT: your previous answer was not valid JSON. "
            "Reply with a single JSON object and nothing else - no prose, no code fences."
        )
        follow_up = list(messages) + [
            {"role": "assistant", "content": text[:2000]},
            {"role": "user", "content": "That was not valid JSON. Reply with the JSON object only."},
        ]
        try:
            text = self.chat(follow_up, system=reminder, **kwargs)
        except ProviderError:
            return None
        return extract_json(text)

    def complete(self, prompt: str, system: str = "", **kwargs: Any) -> str:
        """Single-shot helper used by tools (summarise, explain, rewrite)."""
        return self.chat([{"role": "user", "content": prompt}], system=system or None, **kwargs)

    # -- introspection -----------------------------------------------------
    def status(self) -> List[Dict[str, Any]]:
        now = time.time()
        with self._lock:
            return [
                self._health[config.slug].to_dict(now)
                for config in self.settings.ai.providers
                if config.slug in self._health
            ]

    def reset_cooldowns(self) -> None:
        with self._lock:
            for health in self._health.values():
                health.cooldown_until = 0.0
                health.consecutive_failures = 0
                health.status = "ready"
        log.info("provider cooldowns cleared")

    def describe_chain(self) -> str:
        return " -> ".join(p.describe() for p in self._providers)


__all__ = [
    "COOLDOWN_AUTH",
    "COOLDOWN_RATE_LIMIT",
    "ProviderHealth",
    "ProviderManager",
    "extract_json",
]
