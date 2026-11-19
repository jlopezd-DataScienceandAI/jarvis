"""Configuration: config.toml for settings, .env for secrets."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent


class UserConfig(BaseModel):
    address: str = "señor"
    city: str = ""


class BrainConfig(BaseModel):
    model: str = "claude-haiku-5-5"
    effort: str = "low"
    verbosity: Literal["breve", "normal", "detallado"] = "normal"
    max_tokens: int = 16000
    idle_reset_minutes: int = 10
    max_context_tokens: int = 60000
    web_search: bool = True
    web_search_tool: str = "web_search_20250305"
    web_fetch_tool: str = "web_fetch_20250910"


class VoiceConfig(BaseModel):
    voice_id: str = ""
    model: str = "eleven_v4_turbo"
    language: str = "es"
    output_format: str = "pcm_24000"
    text_normalization: bool = True

    @property
    def sample_rate(self) -> int:
        """Sample rate encoded in a ``pcm_<rate>`` output format."""
        if not self.output_format.startswith("pcm_"):
            raise ValueError(
                f"output_format '{self.output_format}' no es PCM; usa pcm_16000, pcm_22050 o pcm_24000"
            )
        return int(self.output_format.split("_", 1)[1])


class EarsConfig(BaseModel):
    whisper_model: str = "small"
    compute_type: str = "int8"
    beam_size: int = 5
    cpu_threads: int = 0  # 0 = one per physical core
    language: str = "es"
    wake_model: str = "hey_jarvis"
    wake_threshold: float = 0.5
    wake_by_name: bool = True  # also wake on "Jarvis" said alone, recognised with Whisper
    hotkey: str = "<ctrl>+<alt>+j"
    silence_ms: int = 900
    max_utterance_seconds: int = 20
    input_device: str = ""
    output_device: str = ""


class FilesConfig(BaseModel):
    allowed_roots: list[str] = Field(default_factory=list)


class BriefingConfig(BaseModel):
    on_first_start: bool = True


class CalendarConfig(BaseModel):
    enabled: bool = True
    reminder_minutes: int = Field(default=0, ge=0, le=40320)
    event_minutes: int = Field(default=30, ge=5, le=1440)


class UiConfig(BaseModel):
    enabled: bool = True
    width: int = 1280
    height: int = 860


class Config(BaseModel):
    user: UserConfig = Field(default_factory=UserConfig)
    brain: BrainConfig = Field(default_factory=BrainConfig)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    ears: EarsConfig = Field(default_factory=EarsConfig)
    files: FilesConfig = Field(default_factory=FilesConfig)
    briefing: BriefingConfig = Field(default_factory=BriefingConfig)
    calendar: CalendarConfig = Field(default_factory=CalendarConfig)
    ui: UiConfig = Field(default_factory=UiConfig)

    # Where user data lives. Overridable so tests can point at a temp dir.
    memory_dir: Path = ROOT / "memory"
    data_dir: Path = ROOT / "data"

    @property
    def elevenlabs_api_key(self) -> str:
        return os.environ.get("ELEVENLABS_API_KEY", "").strip()

    @property
    def has_anthropic_credentials(self) -> bool:
        return bool(
            os.environ.get("ANTHROPIC_API_KEY", "").strip()
            or os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip()
        )

    @property
    def calendar_url(self) -> str:
        return os.environ.get("JARVIS_CALENDAR_URL", "").strip()

    @property
    def calendar_key(self) -> str:
        return os.environ.get("JARVIS_CALENDAR_KEY", "").strip()

    @property
    def calendar_ready(self) -> bool:
        """Google Calendar is switched on and `jarvis calendar setup` has been completed."""
        return bool(self.calendar.enabled and self.calendar_url and self.calendar_key)

    def ensure_dirs(self) -> None:
        (self.memory_dir / "notes").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "logs").mkdir(parents=True, exist_ok=True)


def load_config(path: Path | None = None) -> Config:
    """Load .env and config.toml. Missing file or keys fall back to defaults."""
    load_dotenv(ROOT / ".env")
    path = path or ROOT / "config.toml"
    raw: dict = {}
    if path.exists():
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
    config = Config.model_validate(raw)
    config.ensure_dirs()
    return config
