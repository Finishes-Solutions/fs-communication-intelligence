"""Process settings read from environment variables.

Deployed apps read the ingestion mode from the PostgreSQL ``runtime_config``
revision (ADR-011), so it is not a setting here. These settings choose
backends and policies, and they fail closed: unsafe combinations raise at
startup instead of degrading quietly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class DeploymentEnvironment(StrEnum):
    LOCAL = "local"
    DEVELOPMENT = "development"
    PRODUCTION = "production"


class AttachmentPolicy(StrEnum):
    """Open decision D-E1 (ADR-016)."""

    HOLD_MESSAGE = "hold_message"
    PUBLISH_PARTIAL = "publish_partial"
    EXEMPT_CID_INLINE = "exempt_cid_inline"


class ClassifierBackend(StrEnum):
    """Open decision D-C1 (ADR-017)."""

    AZURE_OPENAI = "azure_openai"
    DECISIONS_API = "decisions_api"
    FAKE = "fake"


class EmbedderBackend(StrEnum):
    AZURE_OPENAI = "azure_openai"
    FAKE = "fake"


class ScreenerBackend(StrEnum):
    AZURE = "azure"
    FAKE = "fake"


class GraphBackend(StrEnum):
    GRAPH = "graph"
    FIXTURE = "fixture"


class AuthMode(StrEnum):
    ENTRA = "entra"
    LOCAL_TEST = "local_test"


_FAKE_BACKENDS: dict[str, StrEnum] = {
    "classifier_backend": ClassifierBackend.FAKE,
    "embedder": EmbedderBackend.FAKE,
    "screener": ScreenerBackend.FAKE,
    "graph_backend": GraphBackend.FIXTURE,
}

_LOCAL_DEFAULTS: dict[str, StrEnum] = {
    **_FAKE_BACKENDS,
    "attachment_unsupported_policy": AttachmentPolicy.PUBLISH_PARTIAL,
}

_CLOUD_DEFAULTS: dict[str, StrEnum] = {
    "classifier_backend": ClassifierBackend.AZURE_OPENAI,
    "embedder": EmbedderBackend.AZURE_OPENAI,
    "screener": ScreenerBackend.AZURE,
    "graph_backend": GraphBackend.GRAPH,
}


class SettingsError(ValueError):
    """Raised when settings are missing or unsafe for the deployment environment."""


class Settings(BaseSettings):
    """Environment-variable settings. Names match ``config/runtime.example.env``."""

    model_config = SettingsConfigDict(case_sensitive=False, extra="ignore", frozen=True)

    deployment_environment: DeploymentEnvironment
    attachment_unsupported_policy: AttachmentPolicy
    classifier_backend: ClassifierBackend
    classifier_decisions_api_approved: bool = False
    embedder: EmbedderBackend
    embedding_dimensions: int = 1536
    screener: ScreenerBackend
    graph_backend: GraphBackend
    auth_mode: AuthMode = AuthMode.ENTRA
    email_include_sent: bool = True
    email_backfill_since: datetime | None = None
    # Local only: a libpq URL for the docker-compose database. Deployed apps
    # will connect with an Entra token instead (not built yet).
    database_url: str | None = None
    # Local stand-in for private Blob storage (raw Graph payloads).
    local_blob_dir: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _apply_environment_defaults(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        values = {k.lower(): v for k, v in data.items() if v not in (None, "")}
        env = str(values.get("deployment_environment", "")).lower()
        if env == DeploymentEnvironment.LOCAL:
            defaults = _LOCAL_DEFAULTS
        elif env == DeploymentEnvironment.DEVELOPMENT:
            defaults = {
                **_CLOUD_DEFAULTS,
                "attachment_unsupported_policy": AttachmentPolicy.PUBLISH_PARTIAL,
            }
        else:
            # Production: no attachment-policy default, so a missing value fails.
            defaults = _CLOUD_DEFAULTS
        for key, value in defaults.items():
            values.setdefault(key, value)
        return values

    @field_validator("email_backfill_since")
    @classmethod
    def _backfill_is_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("EMAIL_BACKFILL_SINCE must include a UTC offset")
        return value.astimezone(UTC)

    @field_validator("embedding_dimensions")
    @classmethod
    def _positive_dimensions(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("EMBEDDING_DIMENSIONS must be positive")
        return value

    @model_validator(mode="after")
    def _refuse_unsafe_combinations(self) -> Settings:
        local = self.deployment_environment is DeploymentEnvironment.LOCAL
        if self.auth_mode is AuthMode.LOCAL_TEST and not local:
            raise ValueError(
                "AUTH_MODE=local_test is only allowed when DEPLOYMENT_ENVIRONMENT=local"
            )
        if self.deployment_environment is DeploymentEnvironment.PRODUCTION:
            fakes = [name for name, fake in _FAKE_BACKENDS.items() if getattr(self, name) == fake]
            if fakes:
                raise ValueError(f"fake backends are not allowed in production: {', '.join(fakes)}")
        if (
            self.classifier_backend is ClassifierBackend.DECISIONS_API
            and not self.classifier_decisions_api_approved
        ):
            raise ValueError(
                "CLASSIFIER_BACKEND=decisions_api sends email content to OpenAI and requires "
                "CLASSIFIER_DECISIONS_API_APPROVED=true after data-processing approval (D-C1)"
            )
        return self

    @property
    def initial_capture_allowed(self) -> bool:
        """Initial mailbox capture is blocked until a backfill boundary is set (D-S2)."""
        return self.email_backfill_since is not None


def load_settings() -> Settings:
    """Read settings from the process environment, raising ``SettingsError`` on failure."""
    try:
        return Settings()  # values come from the environment
    except ValueError as exc:
        raise SettingsError(str(exc)) from exc
