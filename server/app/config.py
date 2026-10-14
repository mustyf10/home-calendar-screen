import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    tz: str
    data_dir: Path
    backup_dir: Path
    anthropic_api_key: str | None
    llm_model: str
    base_url_internal: str = "http://127.0.0.1:8000"

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"


def load() -> Config:
    return Config(
        tz=os.environ.get("TZ") or "Europe/London",
        data_dir=Path(os.environ.get("DATA_DIR") or "/data"),
        backup_dir=Path(os.environ.get("BACKUP_DIR") or "/backups"),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY") or None,
        llm_model=os.environ.get("LLM_MODEL") or "claude-sonnet-5-5",
    )


config = load()
