from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ONTO_", env_file=".env", extra="ignore")

    database_url: str = "postgresql://agent_reader:agent_reader@localhost:5432/commerce"
    statement_timeout_ms: int = 5_000
    max_rows: int = 200

    kg_backend: Literal["rdflib", "graphdb"] = "rdflib"
    ontology_path: Path = Path("ontology/commerce.ttl")
    graphdb_url: str = "http://localhost:7200"
    graphdb_repository: str = "commerce"

    anthropic_api_key: SecretStr | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
    model: str = "claude-sonnet-5-5"
    max_attempts: int = Field(default=3, ge=1, le=10)


@lru_cache
def get_settings() -> Settings:
    return Settings()
