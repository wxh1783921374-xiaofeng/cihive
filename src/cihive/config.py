from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CIHIVE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    github_app_id: int | None = None
    github_private_key: str | None = None
    github_private_key_path: Path | None = None
    github_webhook_secret: str | None = None
    github_app_slug: str = "cihive"
    github_api_url: str = "https://api.github.com"

    control_plane_url: str = "http://127.0.0.1:8000"
    admin_token: str | None = None
    workflow_file: str = ".github/workflows/cihive.yml"
    require_oidc: bool = True
    github_oidc_audience: str = "cihive"
    github_oidc_issuer: str = "https://token.actions.githubusercontent.com"
    github_oidc_jwks_url: str = "https://token.actions.githubusercontent.com/.well-known/jwks"

    data_dir: Path = Path("data")
    database_path: Path | None = None
    model_provider: Literal["openai", "none"] = "openai"
    model: str = "gpt-6-astra"

    max_workers: int = Field(default=3, ge=1, le=5)
    max_model_turns: int = Field(default=12, ge=1, le=40)
    max_model_calls_per_worker: int = Field(default=2, ge=1, le=4)
    run_timeout_seconds: int = Field(default=1200, ge=30)
    assignment_ttl_seconds: int = Field(default=1800, ge=60)
    max_patch_lines: int = Field(default=500, ge=1)
    max_patch_files: int = Field(default=20, ge=1)
    max_patch_bytes: int = Field(default=1_000_000, ge=1024)
    max_file_bytes: int = Field(default=512_000, ge=1024)
    max_context_bytes: int = Field(default=2_000_000, ge=1024)
    max_log_chars: int = Field(default=200_000, ge=1000)
    max_log_bytes: int = Field(default=10_000_000, ge=10_000)
    max_webhook_bytes: int = Field(default=2_000_000, ge=1024)

    @property
    def openai_api_key(self) -> str | None:
        return os.getenv("OPENAI_API_KEY")

    @property
    def resolved_database_path(self) -> Path:
        return self.database_path or self.data_dir / "cihive.db"

    def load_private_key(self) -> str | None:
        if self.github_private_key:
            return self.github_private_key.replace("\\n", "\n")
        if self.github_private_key_path:
            return self.github_private_key_path.read_text(encoding="utf-8")
        return None

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.resolved_database_path.parent.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "artifacts").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "traces").mkdir(parents=True, exist_ok=True)
