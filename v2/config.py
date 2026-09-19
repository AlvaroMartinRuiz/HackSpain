from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
load_dotenv(ROOT / "v2" / ".env", override=True)


@dataclass(frozen=True)
class Config:
    host: str = "127.0.0.1"
    port: int = field(default_factory=lambda: int(os.getenv("V2_PORT", "7861")))
    mode: str = field(default_factory=lambda: os.getenv("V2_MODE", "simulation"))
    allow_paid: bool = field(default_factory=lambda: os.getenv("V2_ENABLE_PAID", "false").lower() == "true")
    allow_submissions: bool = field(default_factory=lambda: os.getenv("V2_ALLOW_SUBMISSIONS", "false").lower() == "true")
    operator_token: str = field(default_factory=lambda: os.getenv("V2_OPERATOR_TOKEN") or os.getenv("CONSOLE_TOKEN", ""), repr=False)
    gateway_key: str = field(default_factory=lambda: os.getenv("AI_GATEWAY_API_KEY", ""), repr=False)
    llm_model: str = field(default_factory=lambda: os.getenv("V2_LLM_MODEL", "openai/gpt-4.1"))
    deepgram_key: str = field(default_factory=lambda: os.getenv("DEEPGRAM_API_KEY", ""), repr=False)
    cartesia_key: str = field(default_factory=lambda: os.getenv("CARTESIA_API_KEY", ""), repr=False)
    elevenlabs_key: str = field(default_factory=lambda: os.getenv("ELEVENLABS_API_KEY", ""), repr=False)
    voice_en: str = field(default_factory=lambda: os.getenv("V2_CARTESIA_VOICE_EN", "47c38ca4-5f35-497b-b1a3-415245fb35e1"))
    voice_es: str = field(default_factory=lambda: os.getenv("V2_CARTESIA_VOICE_ES", ""))
    voice_ca: str = field(default_factory=lambda: os.getenv("V2_ELEVENLABS_VOICE_CA", os.getenv("ELEVENLABS_VOICE_ID_ES", "DPwFpA8IrumLrL4D7PBE")))
    jev_url: str = field(default_factory=lambda: os.getenv("V2_JEV_URL", ""))
    jev_token: str = field(default_factory=lambda: os.getenv("V2_JEV_TOKEN", ""), repr=False)
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("V2_DATA_DIR", str(ROOT / "v2" / ".data"))))
    revision: str = field(default_factory=lambda: os.getenv("V2_BUILD_REVISION", "unversioned-working-tree"))
    api_key: str = field(default_factory=lambda: os.getenv("PLATFORM_API_KEY") or os.getenv("PROSPER_API_KEY", ""), repr=False)
    api_base_url: str = field(default_factory=lambda: (os.getenv("PLATFORM_API_BASE_URL") or os.getenv("PROSPER_BASE_URL") or "https://hackspain.getprosperapp.com/api/v1").rstrip("/"))
    catalog_path: Path = field(default_factory=lambda: Path(os.getenv("CATALOG_PATH", str(ROOT / "data" / "catalog.json"))))
    quiver_api_key: str = field(default_factory=lambda: os.getenv("QUIVER_API_KEY") or os.getenv("QUIVERAI_API_KEY", ""), repr=False)
    quiver_base_url: str = field(default_factory=lambda: os.getenv("QUIVER_BASE_URL", "https://api.quiver.ai").rstrip("/"))
    quiver_model: str = field(default_factory=lambda: os.getenv("QUIVER_MODEL", ""))
    call_limit_s: int = 145
    completion_grace_s: float = 15

    def __post_init__(self):
        if self.mode not in {"simulation", "practice", "live"}:
            raise ValueError("invalid V2_MODE")
        if self.mode == "live" and not self.allow_submissions:
            raise ValueError("live mode requires explicit V2_ALLOW_SUBMISSIONS=true")
        if self.jev_url and not self.jev_url.startswith("https://"):
            raise ValueError("the Jev service URL must use HTTPS")

    def missing_voice(self) -> list[str]:
        required = {
            "V2_ENABLE_PAID": self.allow_paid, "V2_OPERATOR_TOKEN": self.operator_token,
            "AI_GATEWAY_API_KEY": self.gateway_key, "DEEPGRAM_API_KEY": self.deepgram_key,
            "CARTESIA_API_KEY": self.cartesia_key, "ELEVENLABS_API_KEY": self.elevenlabs_key,
            "V2_CARTESIA_VOICE_ES": self.voice_es,
        }
        return [name for name, value in required.items() if not value]

    def manifest(self) -> dict:
        digest = hashlib.sha256()
        sources = [*(ROOT / "v2").glob("*.py"), *(ROOT / "v2" / "domain").glob("*.py"),
                   ROOT / "v2" / "platform_api" / "client.py", self.catalog_path]
        for path in sorted(sources):
            digest.update(str(path.relative_to(ROOT)).replace("\\", "/").encode() + path.read_bytes())
        return {"schema": 1, "revision": self.revision, "code_sha256": digest.hexdigest(),
                "workflow": "intent-ledger-v1", "pipecat": "1.9.0", "langgraph": "1.2.11",
                "llm": self.llm_model, "stt": "deepgram/nova-3", "cartesia": "sonic-3.6-2026-08-27",
                "voices": {"en": self.voice_en, "es": self.voice_es, "ca": self.voice_ca},
                "jev_question_pack": "conversation-v1", "voice_clone": False,
                "vercel_team": "team_eKQmBBCFo2YNvcJuPGEB7uLN"}
