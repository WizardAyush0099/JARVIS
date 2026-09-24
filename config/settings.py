"""Central, env-driven configuration for JARVIS.

Everything tunable lives here and is read from environment variables so that no
secret is ever hard-coded, logged or sent to the GUI.  The GUI/web API only ever
consume :meth:`Settings.public_dict`, which is a deliberately redacted view.

Configuration is layered (last wins):

1. the real process environment (always wins unless ``override=True``)
2. ``.env``
3. ``.env.local``

``.env`` is optional: JARVIS boots, serves the GUI and stays useful with zero
configuration because the ``offline`` provider is always appended.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# .env loading (python-dotenv if present, tiny built-in parser otherwise)
# --------------------------------------------------------------------------- #
def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    if " #" in value:  # trailing inline comment on an unquoted value
        value = value.split(" #", 1)[0]
    return value.strip()


def parse_env_file(path: Path) -> Dict[str, str]:
    """Parse a simple ``KEY=value`` file.  Never raises."""
    data: Dict[str, str] = {}
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return data
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, _, value = line.partition("=")
        key = key.strip()
        if key:
            data[key] = _unquote(value)
    return data


def load_dotenv(root: Path = PROJECT_ROOT, override: bool = False) -> List[str]:
    """Load ``.env`` then ``.env.local``.  Returns the files that were found."""
    # python-dotenv does the same job; use it when the user already has it.
    try:  # pragma: no cover - optional dependency
        from dotenv import load_dotenv as _dotenv_load  # type: ignore

        for name in (".env", ".env.local"):
            path = root / name
            if path.exists():
                _dotenv_load(dotenv_path=path, override=False)
    except Exception:
        pass

    preexisting = set(os.environ)
    merged: Dict[str, str] = {}
    found: List[str] = []
    for name in (".env", ".env.local"):
        path = root / name
        if not path.exists():
            continue
        found.append(name)
        merged.update(parse_env_file(path))  # .env.local wins over .env

    for key, value in merged.items():
        # the real environment always wins unless the caller overrides it
        if override or key not in preexisting:
            os.environ[key] = value
    return found


# --------------------------------------------------------------------------- #
# typed env helpers
# --------------------------------------------------------------------------- #
def env(key: str, default: str = "") -> str:
    value = os.environ.get(key)
    return default if value is None or value == "" else value.strip()


def env_bool(key: str, default: bool = False) -> bool:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on", "enable", "enabled"}


def env_int(key: str, default: int) -> int:
    try:
        return int(float(env(key, str(default))))
    except (TypeError, ValueError):
        return default


def env_float(key: str, default: float) -> float:
    try:
        return float(env(key, str(default)))
    except (TypeError, ValueError):
        return default


def env_list(key: str, default: Sequence[str] = ()) -> List[str]:
    raw = os.environ.get(key)
    if raw is None or not raw.strip():
        return [str(item) for item in default]
    return [part.strip() for part in re.split(r"[,\n;]", raw) if part.strip()]


def module_present(name: str, package: Optional[str] = None) -> bool:
    """True when an optional dependency is installed.

    Every heavy or platform-specific package (``gpiozero``, ``vosk``,
    ``pocketsphinx``, ``psutil``) is optional in JARVIS, so "is it there?" has to
    be asked without importing it at startup.  ``package`` covers the cases where
    the import name differs from the pip name (``python-dotenv`` -> ``dotenv``).
    """
    import importlib.util

    try:
        return importlib.util.find_spec(package or name) is not None
    except (ImportError, ValueError):  # pragma: no cover - broken installs
        return False


# --------------------------------------------------------------------------- #
# AI provider catalogue
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ProviderConfig:
    """A resolved provider entry (never renders its key via repr)."""

    slug: str
    label: str
    kind: str  # "openai" | "gemini" | "offline"
    base_url: str
    model: str
    api_key: str = field(default="", repr=False)
    local: bool = False
    timeout: float = 45.0
    free_tier: bool = True
    docs: str = ""
    requires_key: bool = False

    @property
    def configured(self) -> bool:
        return self.local or bool(self.api_key)

    @property
    def usable(self) -> bool:
        """Can this provider actually serve a request as configured?"""
        if self.kind == "offline":
            return True
        if self.requires_key and not self.api_key:
            return False
        return bool(self.base_url)


PROVIDER_PRESETS: Dict[str, Dict[str, Any]] = {
    "gemini": dict(
        label="Google Gemini",
        kind="gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        model="gemini-2.0-flash",
        key_env="GEMINI_API_KEY",
        extra_keys=("GOOGLE_API_KEY",),
        docs="https://aistudio.google.com/app/apikey",
    ),
    "groq": dict(
        label="Groq",
        kind="openai",
        base_url="https://api.groq.com/openai/v1",
        model="llama-3.3-70b-versatile",
        key_env="GROQ_API_KEY",
        docs="https://console.groq.com/keys",
    ),
    "openrouter": dict(
        label="OpenRouter",
        kind="openai",
        base_url="https://openrouter.ai/api/v1",
        model="meta-llama/llama-3.3-70b-instruct:free",
        key_env="OPENROUTER_API_KEY",
        docs="https://openrouter.ai/keys",
    ),
    "openai": dict(
        label="OpenAI",
        kind="openai",
        base_url="https://api.openai.com/v1",
        model="gpt-4o-mini",
        key_env="OPENAI_API_KEY",
        free_tier=False,
        docs="https://platform.openai.com/api-keys",
    ),
    "together": dict(
        label="Together AI",
        kind="openai",
        base_url="https://api.together.xyz/v1",
        model="meta-llama/Llama-3.3-70B-Instruct-Turbo-Free",
        key_env="TOGETHER_API_KEY",
        docs="https://api.together.ai/settings/api-keys",
    ),
    "cerebras": dict(
        label="Cerebras",
        kind="openai",
        base_url="https://api.cerebras.ai/v1",
        model="llama-3.3-70b",
        key_env="CEREBRAS_API_KEY",
        docs="https://cloud.cerebras.ai",
    ),
    "mistral": dict(
        label="Mistral",
        kind="openai",
        base_url="https://api.mistral.ai/v1",
        model="mistral-small-latest",
        key_env="MISTRAL_API_KEY",
        docs="https://console.mistral.ai/api-keys",
    ),
    "sambanova": dict(
        label="SambaNova",
        kind="openai",
        base_url="https://api.sambanova.ai/v1",
        model="Meta-Llama-3.3-70B-Instruct",
        key_env="SAMBANOVA_API_KEY",
        # Very low latency, which suits the plan-then-answer pattern. The free
        # tier is limited per day, so treat it as a fast fallback rather than
        # the primary brain.
        docs="https://cloud.sambanova.ai/",
    ),
    "deepseek": dict(
        label="DeepSeek",
        kind="openai",
        base_url="https://api.deepseek.com/v1",
        model="deepseek-chat",
        key_env="DEEPSEEK_API_KEY",
        docs="https://platform.deepseek.com/api_keys",
    ),
    "ollama": dict(
        label="Ollama (local)",
        kind="openai",
        base_url="http://localhost:11434/v1",
        model="llama3.2",
        key_env="",
        local=True,
        docs="https://ollama.com",
    ),
    "offline": dict(
        label="Offline engine",
        kind="offline",
        base_url="",
        model="rules",
        key_env="",
        local=True,
        docs="",
    ),
}

#: Order used when ``AI_PROVIDERS`` is not set at all.
DEFAULT_PROVIDER_ORDER: Tuple[str, ...] = ("gemini", "groq", "openrouter", "openai")


def _preset_slug_from_overrides() -> List[str]:
    """Discover providers configured purely through ``<SLUG>_BASE_URL``."""
    extra: List[str] = []
    for key, _ in os.environ.items():
        match = re.fullmatch(r"([A-Z0-9]+)_BASE_URL", key)
        if not match:
            continue
        slug = match.group(1).lower()
        if slug not in PROVIDER_PRESETS and slug != "custom":
            extra.append(slug)
    return extra


def resolve_providers(timeout: float = 45.0) -> List[ProviderConfig]:
    """Build the provider fallback chain.

    Order: explicitly listed providers first, then any other provider that has
    credentials (so setting a key is enough to make it a usable fallback), and
    finally the always-present ``offline`` engine.
    """
    raw = env("AI_PROVIDERS", ",".join(DEFAULT_PROVIDER_ORDER))
    listed = [name for name in re.split(r"[,\s]+", raw) if name]

    ollama_enabled = env_bool("OLLAMA_ENABLED", False)
    order: List[str] = []
    for name in listed:
        slug = name.lower()
        if slug == "ollama" and not ollama_enabled:
            continue  # only probed when the user says they run it
        order.append(slug)

    known = list(PROVIDER_PRESETS) + _preset_slug_from_overrides()
    for slug in known:
        if slug in order or slug == "offline":
            continue
        preset = PROVIDER_PRESETS.get(slug, {})
        if preset.get("local"):
            # local engines are only probed when explicitly enabled
            if slug == "ollama" and ollama_enabled:
                order.append(slug)
            continue
        key_env = preset.get("key_env", "")
        has_key = bool(key_env and env(key_env)) or bool(env(f"{slug.upper()}_API_KEY"))
        if has_key:
            order.append(slug)
    if "offline" not in order:
        order.append("offline")

    providers: List[ProviderConfig] = []
    for slug in order:
        preset = dict(PROVIDER_PRESETS.get(slug, {}))
        if not preset:
            # Fully custom OpenAI-compatible endpoint, e.g. LM Studio.
            preset = dict(kind="openai", label=slug.title(), model="", key_env="")
        upper = slug.upper()
        key = ""
        if preset.get("key_env"):
            key = env(preset["key_env"], env(f"{upper}_API_KEY"))
        for extra_key in preset.get("extra_keys", ()):  # type: ignore[arg-type]
            key = key or env(extra_key)
        key = key or env(f"{upper}_API_KEY")

        base_url = env(f"{upper}_BASE_URL", preset.get("base_url", ""))
        model = env(f"{upper}_MODEL", preset.get("model", ""))
        local = bool(preset.get("local", False)) or slug == "offline"
        if not base_url and slug != "offline":
            continue
        providers.append(
            ProviderConfig(
                slug=slug,
                label=preset.get("label", slug.title()),
                kind=preset.get("kind", "openai"),
                base_url=base_url.rstrip("/"),
                model=model,
                api_key=key,
                local=local,
                timeout=timeout,
                free_tier=bool(preset.get("free_tier", True)),
                docs=preset.get("docs", ""),
                requires_key=bool(preset.get("key_env")),
            )
        )
    return providers


# --------------------------------------------------------------------------- #
# settings groups
# --------------------------------------------------------------------------- #
@dataclass
class AISettings:
    providers: List[ProviderConfig] = field(default_factory=list)
    temperature: float = 0.4
    max_tokens: int = 900
    timeout: float = 45.0
    max_retries: int = 2
    history_turns: int = 12


@dataclass
class SearchSettings:
    provider: str = "duckduckgo"
    tavily_api_key: str = field(default="", repr=False)
    brave_api_key: str = field(default="", repr=False)
    searxng_url: str = ""
    max_results: int = 5

    @property
    def active(self) -> str:
        if self.provider == "tavily" and self.tavily_api_key:
            return "tavily"
        if self.provider == "brave" and self.brave_api_key:
            return "brave"
        if self.provider == "searxng" and self.searxng_url:
            return "searxng"
        return "duckduckgo"


@dataclass
class ImageSettings:
    provider: str = "pollinations"
    width: int = 1024
    height: int = 1024


@dataclass
class EmailSettings:
    enabled: bool = False
    host: str = "smtp.gmail.com"
    port: int = 587
    user: str = ""
    password: str = field(default="", repr=False)
    sender: str = ""
    use_tls: bool = True
    auto_send: bool = False

    @property
    def configured(self) -> bool:
        return bool(self.enabled and self.host and self.user and self.password)

    @property
    def address(self) -> str:
        return self.sender or self.user


@dataclass
class TTSSettings:
    enabled: bool = True
    engine: str = "edge"
    voice_en: str = "en-GB-RyanNeural"
    voice_hi: str = "hi-IN-MadhurNeural"
    rate: int = -8
    volume: float = 0.9
    cache_dir: str = ""
    #: where spoken replies come out:
    #:   device  - this machine's speakers (CLI, desktop window)
    #:   browser - the web client plays the audio the backend synthesized
    #:   off     - never speak
    voice_output: str = "device"


@dataclass
class STTSettings:
    enabled: bool = False
    engine: str = "google"
    language: str = "en-IN"
    wake_word: str = "jarvis"
    energy_threshold: int = 300
    pause_threshold: float = 0.8
    vosk_model_path: str = ""
    device_index: Optional[int] = None


@dataclass
class WebSettings:
    host: str = "0.0.0.0"
    port: int = 8765
    token: str = field(default="", repr=False)
    open_browser: bool = True


@dataclass
class SafetySettings:
    confirm_destructive: bool = True
    allowed_paths: List[Path] = field(default_factory=list)
    allowed_apps: List[str] = field(default_factory=list)
    tool_timeout: float = 30.0


@dataclass
class PathSettings:
    root: Path
    data_dir: Path
    logs_dir: Path
    assets_dir: Path
    generated_dir: Path
    voice_cache_dir: Path
    memory_file: Path
    chat_log_file: Path
    notes_file: Path
    reminders_file: Path
    trash_dir: Path
    hardware_config: Path

    def ensure(self) -> None:
        for directory in (
            self.data_dir,
            self.logs_dir,
            self.assets_dir,
            self.generated_dir,
            self.voice_cache_dir,
            self.trash_dir,
        ):
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# top level settings
# --------------------------------------------------------------------------- #
@dataclass
class Settings:
    assistant_name: str = "JARVIS"
    owner_name: str = "Ayush"
    language: str = "auto"
    ai: AISettings = field(default_factory=AISettings)
    search: SearchSettings = field(default_factory=SearchSettings)
    image: ImageSettings = field(default_factory=ImageSettings)
    email: EmailSettings = field(default_factory=EmailSettings)
    tts: TTSSettings = field(default_factory=TTSSettings)
    stt: STTSettings = field(default_factory=STTSettings)
    web: WebSettings = field(default_factory=WebSettings)
    safety: SafetySettings = field(default_factory=SafetySettings)
    paths: PathSettings = field(default=None)  # type: ignore[assignment]
    env_files: List[str] = field(default_factory=list)

    # -- construction ------------------------------------------------------
    @classmethod
    def load(cls, root: Path = PROJECT_ROOT, override_env: bool = False) -> "Settings":
        env_files = load_dotenv(root, override=override_env)
        timeout = env_float("AI_REQUEST_TIMEOUT", 45.0)

        paths = PathSettings(
            root=root,
            data_dir=root / "data",
            logs_dir=root / "logs",
            assets_dir=root / "assets",
            generated_dir=root / "assets" / "generated",
            voice_cache_dir=root / "assets" / "voice_cache",
            memory_file=root / "data" / "memory.json",
            chat_log_file=root / "data" / "ChatLog.json",
            notes_file=root / "data" / "notes.json",
            reminders_file=root / "data" / "reminders.json",
            trash_dir=root / "data" / "trash",
            hardware_config=root / "config" / "hardware.json",
        )

        allowed_paths: List[Path] = [root]
        try:
            home = Path.home()
            if home.exists():
                allowed_paths.append(home)
        except Exception:
            pass
        for extra in env_list("ALLOWED_PATHS"):
            expanded = Path(extra).expanduser()
            if expanded.exists():
                allowed_paths.append(expanded)

        settings = cls(
            assistant_name=env("JARVIS_NAME", "JARVIS"),
            owner_name=env("JARVIS_OWNER", "Ayush"),
            language=env("JARVIS_LANGUAGE", "auto").lower(),
            ai=AISettings(
                providers=resolve_providers(timeout=timeout),
                temperature=env_float("AI_TEMPERATURE", 0.4),
                max_tokens=env_int("AI_MAX_TOKENS", 900),
                timeout=timeout,
                max_retries=env_int("AI_MAX_RETRIES", 2),
                history_turns=env_int("JARVIS_HISTORY_TURNS", 12),
            ),
            search=SearchSettings(
                provider=env("SEARCH_PROVIDER", "duckduckgo").lower(),
                tavily_api_key=env("TAVILY_API_KEY"),
                brave_api_key=env("BRAVE_API_KEY"),
                searxng_url=env("SEARXNG_URL"),
                max_results=env_int("SEARCH_MAX_RESULTS", 5),
            ),
            image=ImageSettings(
                provider=env("IMAGE_PROVIDER", "pollinations").lower(),
                width=env_int("IMAGE_WIDTH", 1024),
                height=env_int("IMAGE_HEIGHT", 1024),
            ),
            email=EmailSettings(
                enabled=env_bool("EMAIL_ENABLED", False),
                host=env("SMTP_HOST", "smtp.gmail.com"),
                port=env_int("SMTP_PORT", 587),
                user=env("SMTP_USER"),
                password=env("SMTP_PASSWORD"),
                sender=env("EMAIL_FROM"),
                use_tls=env_bool("EMAIL_USE_TLS", True),
                auto_send=env_bool("EMAIL_AUTO_SEND", False),
            ),
            tts=TTSSettings(
                enabled=env_bool("TTS_ENABLED", True),
                engine=env("TTS_ENGINE", "edge").lower(),
                voice_en=env("TTS_VOICE_EN", "en-GB-RyanNeural"),
                voice_hi=env("TTS_VOICE_HI", "hi-IN-MadhurNeural"),
                rate=env_int("TTS_RATE", -8),
                volume=env_float("TTS_VOLUME", 0.9),
                voice_output=env("JARVIS_VOICE_OUTPUT", "device").lower(),
            ),
            stt=STTSettings(
                enabled=env_bool("STT_ENABLED", False),
                engine=env("STT_ENGINE", "google").lower(),
                language=env("STT_LANGUAGE", "en-IN"),
                wake_word=env("STT_WAKE_WORD", "jarvis").lower(),
                energy_threshold=env_int("STT_ENERGY_THRESHOLD", 300),
                pause_threshold=env_float("STT_PAUSE_THRESHOLD", 0.8),
                vosk_model_path=env("VOSK_MODEL_PATH"),
            ),
            web=WebSettings(
                host=env("JARVIS_WEB_HOST", "0.0.0.0"),
                # PORT is what an isolated container hands us; it wins over
                # JARVIS_WEB_PORT so the same project works in a sandbox and at home
                port=env_int("PORT", env_int("JARVIS_WEB_PORT", 8765)),
                token=env("JARVIS_WEB_TOKEN"),
                open_browser=env_bool("JARVIS_OPEN_BROWSER", True),
            ),
            safety=SafetySettings(
                confirm_destructive=env_bool("CONFIRM_DESTRUCTIVE", True),
                allowed_paths=allowed_paths,
                allowed_apps=[
                    app.lower()
                    for app in env_list(
                        "ALLOWED_APPS",
                        (
                            "code",
                            "chromium",
                            "chromium-browser",
                            "firefox",
                            "terminal",
                            "vscode",
                            "spotify",
                            "vlc",
                            "calculator",
                            "settings",
                            "camera",
                            "thunar",
                        ),
                    )
                ],
                tool_timeout=env_float("TOOL_TIMEOUT", 30.0),
            ),
            paths=paths,
            env_files=env_files,
        )
        settings.tts.cache_dir = str(paths.voice_cache_dir)
        paths.ensure()
        return settings

    # -- read-only views ---------------------------------------------------
    def public_dict(self) -> Dict[str, Any]:
        """Redacted snapshot: safe for the GUI, the web API and the logs."""
        return {
            "assistant_name": self.assistant_name,
            "owner_name": self.owner_name,
            "language": self.language,
            "env_files": list(self.env_files),
            "providers": [
                {
                    "slug": p.slug,
                    "label": p.label,
                    "kind": p.kind,
                    "model": p.model,
                    "local": p.local,
                    "configured": p.configured,
                    "usable": p.usable,
                    "requires_key": p.requires_key,
                    "free_tier": p.free_tier,
                    "docs": p.docs,
                }
                for p in self.ai.providers
            ],
            "search": {"provider": self.search.active, "max_results": self.search.max_results},
            "image": {"provider": self.image.provider, "size": [self.image.width, self.image.height]},
            "email": {"enabled": self.email.configured, "auto_send": self.email.auto_send},
            "tts": {"enabled": self.tts.enabled, "engine": self.tts.engine},
            "stt": {"enabled": self.stt.enabled, "engine": self.stt.engine},
            "web": {"host": self.web.host, "port": self.web.port, "token_required": bool(self.web.token)},
            "safety": {
                "confirm_destructive": self.safety.confirm_destructive,
                "allowed_apps": list(self.safety.allowed_apps),
            },
        }


__all__ = [
    "PROJECT_ROOT",
    "PROVIDER_PRESETS",
    "Settings",
    "ProviderConfig",
    "load_dotenv",
    "parse_env_file",
    "resolve_providers",
    "env",
    "env_bool",
    "env_float",
    "env_int",
    "env_list",
    "module_present",
]
