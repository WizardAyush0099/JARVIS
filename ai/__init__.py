"""JARVIS AI layer: providers, fallback manager and the offline engine."""

from ai.manager import ProviderHealth, ProviderManager, extract_json  # noqa: F401
from ai.offline import OfflineEngine  # noqa: F401
from ai.providers import (  # noqa: F401
    AIProvider,
    AllProvidersFailed,
    ProviderAuthError,
    ProviderBadResponse,
    ProviderError,
    ProviderRateLimited,
    ProviderUnavailable,
    build_provider,
)

__all__ = [
    "AIProvider",
    "AllProvidersFailed",
    "OfflineEngine",
    "ProviderAuthError",
    "ProviderBadResponse",
    "ProviderError",
    "ProviderHealth",
    "ProviderManager",
    "ProviderRateLimited",
    "ProviderUnavailable",
    "build_provider",
    "extract_json",
]
