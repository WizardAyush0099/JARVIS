"""Provider fallback must be automatic, bounded and honest."""

from __future__ import annotations

import time

import pytest

from ai.manager import COOLDOWN_AUTH, ProviderHealth, ProviderManager, extract_json
from ai.offline import OfflineEngine
from ai.providers import (
    AllProvidersFailed,
    OpenAICompatProvider,
    ProviderAuthError,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
)
from config.settings import ProviderConfig, Settings


class FakeProvider(OpenAICompatProvider):
    """An OpenAI-compatible provider whose answer we script."""

    def __init__(self, config: ProviderConfig, behaviour) -> None:
        super().__init__(config)
        self.behaviour = behaviour
        self.calls = 0
        self.timeouts = []

    def chat(self, messages, system=None, temperature=0.4, max_tokens=900, timeout=None):  # noqa: D102
        self.calls += 1
        self.timeouts.append(timeout)
        result = self.behaviour(self.calls) if callable(self.behaviour) else self.behaviour
        if isinstance(result, Exception):
            raise result
        return result


def make_manager(settings: Settings, specs, extras=None) -> ProviderManager:
    """Build a manager around scripted providers.

    ``specs`` is a list of ``(slug, behaviour)``; ``extras`` maps a slug to
    extra :class:`ProviderConfig` fields (timeout, retries, ...).
    """
    extras = extras or {}
    manager = ProviderManager(settings, offline_engine=OfflineEngine(settings))
    manager._providers = []
    manager._health = {}
    for slug, behaviour in specs:
        config = ProviderConfig(
            slug=slug,
            label=slug.title(),
            kind="openai",
            base_url="http://localhost",
            model="test-model",
            api_key="k",
            **extras.get(slug, {}),
        )
        manager._providers.append(FakeProvider(config, behaviour))
        manager._health[slug] = ProviderHealth(slug=slug, label=slug.title(), model="test-model")
    return manager


def ask(manager: ProviderManager) -> str:
    return manager.chat([{"role": "user", "content": "hi"}])


# --------------------------------------------------------------------------- #
# fallback
# --------------------------------------------------------------------------- #
def test_rate_limited_provider_falls_through(settings):
    manager = make_manager(
        settings,
        [
            ("first", ProviderRateLimited("first", "quota")),
            ("second", "hello from the backup"),
        ],
    )
    assert ask(manager) == "hello from the backup"
    assert manager.last_provider == "second"
    assert manager._health["first"].cooldown_until > time.time()


def test_cooldown_skips_the_broken_provider_next_time(settings):
    settings.ai.max_retries = 0  # one attempt per provider, so call counts are exact
    manager = make_manager(
        settings,
        [("first", ProviderUnavailable("first", "boom")), ("second", "ok")],
    )
    ask(manager)
    first = manager._providers[0]
    assert first.calls == 1
    ask(manager)
    assert first.calls == 1, "a cooling provider should not be retried immediately"
    assert manager._health["second"].successes == 2


def test_auth_error_cools_down_for_a_long_time(settings):
    manager = make_manager(settings, [("bad", ProviderAuthError("bad", "no key")), ("good", "fine")])
    ask(manager)
    health = manager._health["bad"]
    assert health.cooldown_until - time.time() > COOLDOWN_AUTH * 0.5
    assert health.last_error


def test_transient_failure_is_retried(settings):
    settings.ai.max_retries = 2

    def flaky(call: int) -> str:
        if call <= 2:
            raise ProviderUnavailable("flaky", "temporary")
        return "eventually worked"

    manager = make_manager(settings, [("flaky", flaky)])
    assert ask(manager) == "eventually worked"
    health = manager._health["flaky"]
    assert health.successes == 1 and health.failures == 0


def test_a_timeout_is_never_retried(settings):
    """Three 45s attempts to a dead provider is exactly the "too slow" bug.

    A timeout already spent the provider's whole budget once; retrying it only
    multiplies the wait, so it must take the fall-through immediately.
    """
    settings.ai.max_retries = 2  # would be three attempts for a generic transient
    manager = make_manager(
        settings,
        [("hanging", ProviderTimeout("hanging", "the provider timed out")), ("good", "backup")],
    )
    assert ask(manager) == "backup"
    assert manager.providers[0].calls == 1, "a timeout must not be retried"


def test_a_plain_transient_failure_is_still_retried(settings):
    """The instant, cheap failures keep their retry budget."""
    settings.ai.max_retries = 1
    manager = make_manager(settings, [("flaky", ProviderUnavailable("flaky", "connection refused"))])
    with pytest.raises(AllProvidersFailed):
        ask(manager)
    assert manager.providers[0].calls == 2


def test_timeouts_are_classified_as_timeouts():
    from ai.http import HttpError
    from ai.providers import ProviderTimeout, classify_http_error

    assert isinstance(classify_http_error("x", TimeoutError("took too long")), ProviderTimeout)
    assert isinstance(
        classify_http_error("x", HttpError("request timed out")), ProviderTimeout
    )
    # a network error is *not* a timeout: it is instant, so it stays retryable
    assert not isinstance(
        classify_http_error("x", HttpError("network error: refused")), ProviderTimeout
    )


def test_emptiness_is_treated_as_a_failure(settings):
    manager = make_manager(settings, [("blank", "   "), ("good", "real answer")])
    assert ask(manager) == "real answer"
    assert manager._health["blank"].failures == 1


def test_every_provider_failing_raises_with_reasons(settings):
    manager = make_manager(
        settings,
        [("a", ProviderUnavailable("a", "down")), ("b", ProviderRateLimited("b", "quota"))],
    )
    with pytest.raises(AllProvidersFailed) as caught:
        ask(manager)
    slugs = {slug for slug, _ in caught.value.failures}
    assert slugs == {"a", "b"}


def test_online_check_reflects_cooldowns(settings):
    manager = make_manager(settings, [("only", "fine")])
    assert manager.has_online_provider() is True
    manager._mark_failure(manager._health["only"], ProviderUnavailable("only", "down"), 0.1)
    assert manager.has_online_provider() is False


def test_reset_clears_cooldowns(settings):
    manager = make_manager(settings, [("only", ProviderUnavailable("only", "down"))])
    with pytest.raises(AllProvidersFailed):
        ask(manager)
    assert manager.has_online_provider() is False
    manager.reset_cooldowns()
    assert manager.has_online_provider() is True
    assert manager._health["only"].to_dict()["cooldown_s"] == 0.0
    assert manager._health["only"].to_dict()["status"] == "ready"


# --------------------------------------------------------------------------- #
# keyless configuration
# --------------------------------------------------------------------------- #
def test_providers_without_keys_are_not_in_the_chain(settings):
    manager = ProviderManager(settings)  # conftest removes all keys
    assert manager.provider_names == ["offline"]
    assert manager.has_online_provider() is False
    statuses = {item["slug"]: item["status"] for item in manager.status()}
    assert statuses["gemini"] == "not configured"
    assert statuses["offline"] == "ready"


def test_keyless_fallback_closes_the_chain(settings, monkeypatch):
    """With no usable key, the keyless endpoint answers - but never first."""
    monkeypatch.setenv("POLLINATIONS_ENABLED", "true")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")  # never used, never called
    manager = ProviderManager(Settings.load(root=settings.paths.root))
    assert manager.provider_names == ["gemini", "pollinations", "offline"]
    assert manager.has_online_provider() is True
    pollinations = next(p for p in manager.providers if p.slug == "pollinations")
    assert isinstance(pollinations, OpenAICompatProvider)
    assert pollinations.config.requires_key is False


def test_keyless_provider_can_be_switched_off(settings, monkeypatch):
    monkeypatch.setenv("POLLINATIONS_ENABLED", "false")
    manager = ProviderManager(Settings.load(root=settings.paths.root))
    assert "pollinations" not in manager.provider_names


def test_provider_timeout_caps_the_global_timeout(settings):
    """A fragile provider must never hold the chain for the global 45s."""
    manager = make_manager(settings, [("slow", "fine")], extras={"slow": {"timeout": 3.0}})
    assert ask(manager) == "fine"
    assert manager.providers[0].timeouts == [3.0]


def test_provider_retry_budget_overrides_the_default(settings):
    """retries=0 means exactly one attempt, however impatient the settings are."""
    manager = make_manager(
        settings,
        [("flaky", ProviderUnavailable("flaky", "down"))],
        extras={"flaky": {"retries": 0}},
    )
    settings.ai.max_retries = 3
    with pytest.raises(AllProvidersFailed):
        ask(manager)
    assert manager.providers[0].calls == 1


def test_offline_provider_always_answers(settings):
    manager = ProviderManager(settings)
    answer = ask(manager)
    assert answer and isinstance(answer, str)


def test_offline_answer_is_useful(settings):
    manager = ProviderManager(settings, offline_engine=OfflineEngine(settings))
    reply = manager.chat([{"role": "user", "content": "who am i"}])
    assert "Ayush" in reply


# --------------------------------------------------------------------------- #
# JSON extraction
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw,expected",
    [
        ('{"a": 1}', {"a": 1}),
        ('```json\n{"a": 1}\n```', {"a": 1}),
        ('Sure! Here is the plan:\n{"steps": []}\nHope that helps.', {"steps": []}),
        ('[1, 2, 3]', [1, 2, 3]),
    ],
)
def test_extract_json(raw, expected):
    assert extract_json(raw) == expected


@pytest.mark.parametrize("raw", ["no json here", "", None, "{broken"])
def test_extract_json_returns_none(raw):
    assert extract_json(raw) is None


def test_classify_error_slug_is_preserved():
    from ai.providers import classify_http_error

    error = classify_http_error("groq", ProviderRateLimited("groq", "quota"))
    assert error.slug == "groq"


# --------------------------------------------------------------------------- #
# catalogue sanity
# --------------------------------------------------------------------------- #
def test_every_provider_preset_is_explainable():
    """Every preset the console can show must link somewhere useful."""
    from config.settings import PROVIDER_PRESETS

    for slug, preset in PROVIDER_PRESETS.items():
        if preset.get("kind") == "offline":
            continue  # the local engine needs no signup and no link
        assert preset.get("docs"), f"{slug} has no docs link"
        assert preset.get("kind") in ("openai", "gemini")
        if preset.get("local") or preset.get("keyless"):
            assert not preset.get("key_env"), f"{slug} must not ask for a key"
        else:
            assert preset.get("key_env"), f"{slug} must name the variable a key goes in"


def test_status_exposes_how_to_fix_a_provider(settings):
    manager = ProviderManager(settings)
    by_slug = {item["slug"]: item for item in manager.status()}
    gemini = by_slug["gemini"]
    assert gemini["status"] == "not configured"
    assert gemini["docs"].startswith("https://")
    assert gemini["requires_key"] is True
    assert "key_env" not in gemini, "the API surface never names secret variables"
