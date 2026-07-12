"""Configuration: secrets/tuning from env vars, scoring rules from scoring.toml."""

from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ClaimPlatformConfig(BaseModel):
    """A manual-claim platform (no API): defined entirely in scoring.toml."""

    name: str
    points: dict[str, int]

    @field_validator("points")
    @classmethod
    def _difficulties_lowercase(cls, v: dict[str, int]) -> dict[str, int]:
        return {k.lower(): pts for k, pts in v.items()}


class ScoringConfig(BaseModel):
    """Composite-board weights and manual-claim platform definitions."""

    weights: dict[str, float]
    claims: dict[str, ClaimPlatformConfig]

    @classmethod
    def defaults(cls) -> ScoringConfig:
        return cls(
            weights={"htb": 1.0, "thm": 1.0, "rootme": 1.0, "claims": 1.0},
            claims={
                "pg": ClaimPlatformConfig(
                    name="OffSec Proving Grounds",
                    points={"easy": 10, "intermediate": 20, "hard": 30, "insane": 40},
                )
            },
        )

    @classmethod
    def load(cls, path: Path) -> ScoringConfig:
        """Parse scoring.toml; missing file or missing sections fall back to defaults."""
        defaults = cls.defaults()
        if not path.is_file():
            return defaults
        with path.open("rb") as f:
            data = tomllib.load(f)
        weights = data.get("composite", {}).get("weights", defaults.weights)
        raw_claims = data.get("claims")
        claims = (
            {key: ClaimPlatformConfig(**cfg) for key, cfg in raw_claims.items()}
            if raw_claims
            else defaults.claims
        )
        return cls(weights={k: float(v) for k, v in weights.items()}, claims=claims)


class Settings(BaseSettings):
    """Environment configuration. See .env.example for documentation of each field."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    discord_token: str
    htb_app_token: str | None = None
    rootme_api_key: str | None = None

    database_url: str = "sqlite+aiosqlite:///data/hackqueue.db"
    scoring_config: Path = Path("scoring.toml")

    poll_interval_htb: int = 45
    poll_interval_thm: int = 60
    poll_interval_rootme: int = 60
    catalog_refresh_hours: int = 24

    log_level: str = "INFO"
    log_format: str = "pretty"

    def scoring(self) -> ScoringConfig:
        return ScoringConfig.load(self.scoring_config)
