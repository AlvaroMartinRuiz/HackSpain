"""Runtime configuration, read once from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

load_dotenv(ROOT / ".env")


def _env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name)
        if value:
            return value.strip()
    return default


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


def _headers(name: str) -> dict:
    """Parse "Key: value, Other: value" into headers, tolerating stray spaces."""
    raw = os.getenv(name) or ""
    headers: dict[str, str] = {}
    for part in raw.split(","):
        key, sep, value = part.partition(":")
        if sep and key.strip() and value.strip():
            headers[key.strip()] = value.strip()
    return headers


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # Platform (clinic reads + submissions)
    api_key: str = field(default_factory=lambda: _env("PLATFORM_API_KEY", "PROSPER_API_KEY"))
    api_base_url: str = field(
        default_factory=lambda: _env(
            "PLATFORM_API_BASE_URL",
            "PROSPER_BASE_URL",
            default="https://hackspain.getprosperapp.com/api/v1",
        ).rstrip("/")
    )

    # Server
    port: int = field(default_factory=lambda: _int("PORT", 7860))
    # Loopback, because ngrok dials from this machine and nothing else needs to.
    # On a venue network 0.0.0.0 hands the console — names, DNIs and transcripts
    # — to everyone on the wifi. Set it explicitly if the tunnel ever moves.
    host: str = field(default_factory=lambda: _env("HOST", default="127.0.0.1"))
    public_ws_url: str = field(default_factory=lambda: _env("PUBLIC_WS_URL"))
    public_console_url: str = field(default_factory=lambda: _env("PUBLIC_CONSOLE_URL"))
    # Reload restarts the server whenever a file is saved, which during a scored
    # call drops the live socket. Off unless asked for.
    reload: bool = field(default_factory=lambda: _bool("RELOAD", False))
    # The console behind ngrok needs this; empty means reachable from this
    # machine only.
    console_token: str = field(default_factory=lambda: _env("CONSOLE_TOKEN"))

    # Speech to text
    stt_provider: str = field(default_factory=lambda: _env("STT_PROVIDER", default="deepgram").lower())
    deepgram_api_key: str = field(default_factory=lambda: _env("DEEPGRAM_API_KEY"))
    deepgram_model: str = field(default_factory=lambda: _env("DEEPGRAM_MODEL", default="nova-3"))
    deepgram_language: str = field(default_factory=lambda: _env("DEEPGRAM_LANGUAGE", default="multi"))
    stt_endpointing_ms: int = field(default_factory=lambda: _int("STT_ENDPOINTING_MS", 500))
    stt_utterance_end_ms: int = field(default_factory=lambda: _int("STT_UTTERANCE_END_MS", 1000))
    stt_min_confidence: float = field(
        default_factory=lambda: _float("STT_MIN_CONFIDENCE", 0.45)
    )
    # Digits over the phone are the sharpest scoring test in the set. Deepgram
    # will emit "44556677" instead of "cuatro cuatro cinco…".
    stt_numerals: bool = field(default_factory=lambda: _bool("STT_NUMERALS", True))
    elevenlabs_stt_model: str = field(
        default_factory=lambda: _env("ELEVENLABS_STT_MODEL", default="scribe_v2_realtime")
    )
    stt_filter_background: bool = field(
        default_factory=lambda: _bool("STT_FILTER_BACKGROUND", True)
    )

    # Language model
    llm_provider: str = field(default_factory=lambda: _env("LLM_PROVIDER", default="openai").lower())
    llm_api_key: str = field(default_factory=lambda: _env("LLM_API_KEY", "OPENAI_API_KEY"))
    llm_base_url: str = field(
        default_factory=lambda: _env("LLM_BASE_URL", default="https://api.openai.com/v1").rstrip("/")
    )
    llm_model: str = field(default_factory=lambda: _env("LLM_MODEL", default="gpt-4o"))
    llm_temperature: float = field(default_factory=lambda: _float("LLM_TEMPERATURE", 0.2))
    llm_max_tool_rounds: int = field(default_factory=lambda: _int("LLM_MAX_TOOL_ROUNDS", 6))
    llm_timeout_s: float = field(default_factory=lambda: _float("LLM_TIMEOUT_S", 60.0))
    # Empty skips the field; Gemini and o-series accept low/medium/high.
    llm_reasoning_effort: str = field(
        default_factory=lambda: _env("LLM_REASONING_EFFORT").lower()
    )
    # Gateways often need one of their own, e.g. Cloudflare's cf-aig-gateway-id.
    llm_extra_headers: dict = field(default_factory=lambda: _headers("LLM_EXTRA_HEADERS"))

    # Text to speech
    # ElevenLabs on the scored path; Aura (`TTS_PROVIDER=deepgram`) is the cheap
    # practice voice and the automatic fallback if Flash 429s.
    tts_provider: str = field(default_factory=lambda: _env("TTS_PROVIDER", default="elevenlabs").lower())
    deepgram_tts_model_es: str = field(
        default_factory=lambda: _env("DEEPGRAM_TTS_MODEL_ES", default="aura-2-silvia-es")
    )
    deepgram_tts_model_en: str = field(
        default_factory=lambda: _env("DEEPGRAM_TTS_MODEL_EN", default="aura-2-thalia-en")
    )
    elevenlabs_api_key: str = field(default_factory=lambda: _env("ELEVENLABS_API_KEY"))
    elevenlabs_voice_id: str = field(
        default_factory=lambda: _env("ELEVENLABS_VOICE_ID", default="EXAVITQu4vr4xnSDxMaL")
    )
    # Native accents: Sarah (American) for English, Gin (peninsular) for Spanish/Catalan.
    elevenlabs_voice_id_en: str = field(
        default_factory=lambda: _env("ELEVENLABS_VOICE_ID_EN", "ELEVENLABS_VOICE_ID",
                                    default="EXAVITQu4vr4xnSDxMaL")
    )
    elevenlabs_voice_id_es: str = field(
        default_factory=lambda: _env("ELEVENLABS_VOICE_ID_ES", default="DPwFpA8IrumLrL4D7PBE")
    )
    elevenlabs_model: str = field(
        default_factory=lambda: _env("ELEVENLABS_MODEL", default="eleven_flash_v2_5")
    )
    elevenlabs_catalan_model: str = field(
        default_factory=lambda: _env(
            "ELEVENLABS_CATALAN_MODEL", default="eleven_v3_conversational"
        )
    )
    cartesia_api_key: str = field(default_factory=lambda: _env("CARTESIA_API_KEY"))
    cartesia_voice_id: str = field(default_factory=lambda: _env("CARTESIA_VOICE_ID"))
    cartesia_model: str = field(default_factory=lambda: _env("CARTESIA_MODEL", default="sonic-2"))
    openai_tts_voice: str = field(default_factory=lambda: _env("OPENAI_TTS_VOICE", default="alloy"))
    openai_tts_model: str = field(default_factory=lambda: _env("OPENAI_TTS_MODEL", default="gpt-4o-mini-tts"))

    # Call behaviour
    call_hard_limit_s: float = field(default_factory=lambda: _float("CALL_HARD_LIMIT_S", 145.0))
    submit_deadline_s: float = field(default_factory=lambda: _float("SUBMIT_DEADLINE_S", 25.0))
    greeting: str = field(
        default_factory=lambda: _env(
            "AGENT_GREETING",
            default="Clínica Arenal, good morning, buenos días. How can I help?",
        )
    )
    barge_in: bool = field(default_factory=lambda: _bool("BARGE_IN", True))
    # Nearly every caller speaks English; a detected language replaces this.
    default_language: str = field(default_factory=lambda: _env("DEFAULT_LANGUAGE", default="en").lower())
    silence_prompt_s: float = field(default_factory=lambda: _float("SILENCE_PROMPT_S", 10.0))
    silence_prompt_max: int = field(default_factory=lambda: _int("SILENCE_PROMPT_MAX", 2))
    # Synthesise the greeting and fixed lines at start-up (~300 characters).
    tts_warm_cache: bool = field(default_factory=lambda: _bool("TTS_WARM_CACHE", True))

    # Design assets
    # Quiver draws SVG, not data, so it is called by scripts/generate_assets.py
    # at build time and never on a page load. The console reads the result off
    # disk, which is why it still has its icons with no key and no wifi.
    quiver_api_key: str = field(default_factory=lambda: _env("QUIVER_API_KEY", "QUIVERAI_API_KEY"))
    quiver_base_url: str = field(
        default_factory=lambda: _env("QUIVER_BASE_URL", default="https://api.quiver.ai").rstrip("/")
    )
    # Empty on purpose: the generator asks /v1/models what this key may use
    # rather than pinning an id that might not exist on the account.
    quiver_model: str = field(default_factory=lambda: _env("QUIVER_MODEL"))

    # Observability
    db_path: str = field(default_factory=lambda: _env("DB_PATH", default=str(DATA_DIR / "calls.db")))
    catalog_path: str = field(
        default_factory=lambda: _env("CATALOG_PATH", default=str(DATA_DIR / "catalog.json"))
    )

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.api_base_url)

    @property
    def stt_active(self) -> str:
        if self.stt_provider == "elevenlabs" and self.elevenlabs_api_key:
            return "elevenlabs"
        if self.stt_provider == "deepgram" and self.deepgram_api_key:
            return "deepgram"
        if self.llm_api_key:
            return "whisper"
        return "none"

    @property
    def stt_model_label(self) -> str:
        if self.stt_active == "elevenlabs":
            return self.elevenlabs_stt_model
        if self.stt_active == "deepgram":
            return self.deepgram_model
        if self.stt_active == "whisper":
            return "whisper-1"
        return "none"

    def missing_voice_keys(self) -> list[str]:
        missing: list[str] = []
        if not self.llm_api_key:
            missing.append("LLM_API_KEY")
        if self.stt_provider == "deepgram" and not self.deepgram_api_key:
            missing.append("DEEPGRAM_API_KEY")
        if self.stt_provider == "elevenlabs" and not self.elevenlabs_api_key:
            missing.append("ELEVENLABS_API_KEY")
        if self.tts_provider == "deepgram" and not self.deepgram_api_key:
            if "DEEPGRAM_API_KEY" not in missing:
                missing.append("DEEPGRAM_API_KEY")
        if self.tts_provider == "elevenlabs" and not self.elevenlabs_api_key:
            if "ELEVENLABS_API_KEY" not in missing:
                missing.append("ELEVENLABS_API_KEY")
        if self.tts_provider == "cartesia" and not self.cartesia_api_key:
            missing.append("CARTESIA_API_KEY")
        if self.tts_provider == "openai" and not self.llm_api_key:
            missing.append("LLM_API_KEY (openai tts)")
        return missing


settings = Settings()
