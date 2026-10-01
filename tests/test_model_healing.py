"""A retired model id must not cost us the provider.

Vendors retire model names constantly, and a stale one used to make a
perfectly healthy provider look "unavailable" - which is exactly how JARVIS
ended up answering every prompt from the offline engine.  The manager now asks
the vendor what it serves and retries on the spot instead.
"""

from __future__ import annotations

from typing import List

from ai.manager import ProviderHealth, ProviderManager
from ai.offline import OfflineEngine
from ai.providers import (
    OpenAICompatProvider,
    ProviderBadResponse,
    ProviderRateLimited,
    classify_http_error,
    looks_like_missing_model,
    pick_model,
)
from ai.http import HttpError
from config.settings import ProviderConfig, Settings


class RetiringProvider(OpenAICompatProvider):
    """Rejects its configured model once, then answers - like a real vendor."""

    def __init__(self, config: ProviderConfig, available: List[str]) -> None:
        super().__init__(config)
        self._available = list(available)
        self.calls = 0

    def list_models(self) -> List[str]:
        return list(self._available)

    def chat(self, messages, system=None, temperature=0.4, max_tokens=900, timeout=None):  # noqa: D102
        self.calls += 1
        if self._model is None:  # still the retired id
            raise ProviderBadResponse(self.slug, "model not found")
        return f"answered with {self.model}"


def build(settings: Settings, provider) -> ProviderManager:
    manager = ProviderManager(settings, offline_engine=OfflineEngine(settings))
    manager._providers = [provider]
    manager._health = {
        provider.slug: ProviderHealth(
            slug=provider.slug, label=provider.label, model=provider.config.model
        )
    }
    return manager


def make_provider(model: str, available: List[str]) -> RetiringProvider:
    return RetiringProvider(
        ProviderConfig(
            slug="groq",
            label="Groq",
            kind="openai",
            base_url="https://api.groq.com/openai/v1",
            model=model,
            api_key="k",
        ),
        available,
    )


# --------------------------------------------------------------------------- #
# the provider is saved, not written off
# --------------------------------------------------------------------------- #
def test_a_retired_model_is_replaced_instead_of_losing_the_provider(settings):
    settings.ai.max_retries = 0
    provider = make_provider("llama-3.3-70b-versatile", ["qwen/qwen3.8-27b", "openai/gpt-oss-20b"])
    manager = build(settings, provider)

    answer = manager.chat([{"role": "user", "content": "hi"}])

    assert answer == "answered with qwen/qwen3.8-27b"
    assert manager.last_provider == "groq"
    assert provider.calls == 2, "one rejected call, then one retry with the new id"
    assert manager._health["groq"].cooldown_until == 0, "a healed provider must not cool down"


def test_the_new_model_is_reported_in_the_status(settings):
    """The console shows the model, so it must show the one now in use."""
    settings.ai.max_retries = 0
    manager = build(settings, make_provider("llama-3.3-70b-versatile", ["qwen/qwen3.8-27b"]))
    manager.chat([{"role": "user", "content": "hi"}])

    row = next(r for r in manager.status() if r["slug"] == "groq")
    assert row["model"] == "qwen/qwen3.8-27b"


def test_no_alternative_leaves_the_provider_failing_normally(settings):
    settings.ai.max_retries = 0
    provider = make_provider("llama-3.3-70b-versatile", [])  # vendor will not say
    manager = build(settings, provider)

    try:
        manager.chat([{"role": "user", "content": "hi"}])
    except Exception as exc:  # noqa: BLE001 - the point is that it still fails
        assert "groq" in str(exc)
    else:  # pragma: no cover - would mean healing invented an answer
        raise AssertionError("provider should have failed when no model is available")


def test_other_failures_are_not_mistaken_for_a_retired_model(settings):
    """A rate limit must cool the provider down, not silently switch models."""
    settings.ai.max_retries = 0

    class RateLimited(RetiringProvider):
        def chat(self, messages, system=None, temperature=0.4, max_tokens=900, timeout=None):
            raise ProviderRateLimited(self.slug, "quota")

    provider = RateLimited(
        ProviderConfig(
            slug="groq", label="Groq", kind="openai",
            base_url="https://api.groq.com/openai/v1",
            model="llama-3.3-70b-versatile", api_key="k",
        ),
        ["qwen/qwen3.8-27b"],
    )
    manager = build(settings, provider)
    try:
        manager.chat([{"role": "user", "content": "hi"}])
    except Exception:  # noqa: BLE001
        pass
    assert provider.model == "llama-3.3-70b-versatile", "must not switch on a rate limit"
    assert manager._health["groq"].cooldown_until > 0


# --------------------------------------------------------------------------- #
# classification
# --------------------------------------------------------------------------- #
def test_a_retired_model_is_recognised_whatever_the_vendor_calls_it():
    bodies = [
        ('{"error":{"message":"Model Not Exist"}}', 400),
        ('{"error":{"message":"This model models/gemini-2.0-flash is no longer available."}}', 404),
        ('{"error":{"message":"The model `x` does not exist or you do not have access."}}', 404),
        ('{"error":{"message":"Invalid model: gpt-9"}}', 400),
    ]
    for body, status in bodies:
        error = classify_http_error("t", HttpError("x", status=status, body=body))
        assert looks_like_missing_model(error), f"{status} {body}"


def test_real_failures_are_not_mistaken_for_a_retired_model():
    cases = [
        ('{"error":{"message":"invalid api key"}}', 401),
        ('{"error":{"message":"rate limit reached"}}', 429),
        ('{"detail":"Not Found"}', 404),
        ('{"error":{"message":"request timed out"}}', 500),
    ]
    for body, status in cases:
        error = classify_http_error("t", HttpError("x", status=status, body=body))
        assert not looks_like_missing_model(error), f"{status} {body}"


# --------------------------------------------------------------------------- #
# choosing a replacement
# --------------------------------------------------------------------------- #
class StaleListingProvider(OpenAICompatProvider):
    """Advertises a dead model, the way Google still lists gemini-2.5-flash."""

    def __init__(self, config, advertised: List[str], working: set) -> None:
        super().__init__(config)
        self._advertised = list(advertised)
        self._working = set(working)
        self.calls = 0

    def list_models(self) -> List[str]:
        return list(self._advertised)

    def chat(self, messages, system=None, temperature=0.4, max_tokens=900, timeout=None):  # noqa: D102
        self.calls += 1
        if self.model not in self._working:
            raise ProviderBadResponse(self.slug, "model not found")
        return f"answered with {self.model}"


def test_a_stale_vendor_listing_does_not_kill_the_provider(settings):
    """The first substitute can be dead too - so try more than one.

    Google still advertises ``gemini-2.5-flash`` while the generateContent
    endpoint rejects it, so stopping at the first replacement reported a
    perfectly healthy Gemini as unavailable.
    """
    settings.ai.max_retries = 0
    provider = StaleListingProvider(
        ProviderConfig(
            slug="gemini", label="Google Gemini", kind="gemini",
            base_url="https://generativelanguage.googleapis.com/v1beta",
            model="gemini-2.0-flash", api_key="k",
        ),
        advertised=["gemini-2.5-flash", "gemini-3.5-flash", "gemini-flash-latest"],
        working={"gemini-3.5-flash"},
    )
    manager = build(settings, provider)

    answer = manager.chat([{"role": "user", "content": "hi"}])

    assert answer == "answered with gemini-3.5-flash"
    assert provider.calls == 3, "the retired id, then the stale one, then a live one"
    assert manager._health["gemini"].cooldown_until == 0


def test_it_gives_up_once_every_candidate_is_exhausted(settings):
    settings.ai.max_retries = 0
    provider = StaleListingProvider(
        ProviderConfig(
            slug="gemini", label="Google Gemini", kind="gemini",
            base_url="https://generativelanguage.googleapis.com/v1beta",
            model="gemini-2.0-flash", api_key="k",
        ),
        advertised=["gemini-2.5-flash", "gemini-3.5-flash", "gemini-flash-latest"],
        working=set(),
    )
    manager = build(settings, provider)

    try:
        manager.chat([{"role": "user", "content": "hi"}])
    except Exception:  # noqa: BLE001
        pass
    else:  # pragma: no cover
        raise AssertionError("should fail once nothing it advertises answers")
    assert provider.calls <= 4, "it must not keep trying forever"


def test_the_substitute_keeps_the_family_when_it_can():
    assert pick_model(["deepseek-v4-pro", "deepseek-flash"], "deepseek-chat") == "deepseek-flash"


def test_the_substitute_prefers_a_small_model_on_a_pi():
    chosen = pick_model(["gemini-3.8-pro", "gemini-3.5-flash"], "gemini-2.0-flash")
    assert chosen == "gemini-3.5-flash"


def test_an_exact_match_is_never_replaced():
    assert pick_model(["a", "b"], "b") == "b"


def test_nothing_to_choose_from():
    assert pick_model([], "whatever") is None
