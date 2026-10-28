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


class SensitiveDataPolicy(StrEnum):
    """Bank, ID and card numbers and credentials: ``mask`` them and index the rest of
    the message (Matthew, 2026-10-28), or ``restrict`` the whole message."""

    MASK = "mask"
    RESTRICT = "restrict"


class GraphAuth(StrEnum):
    """``certificate``: app registration (operator's machine). ``managed_identity``: in Azure."""

    CERTIFICATE = "certificate"
    MANAGED_IDENTITY = "managed_identity"


class DatabaseAuth(StrEnum):
    """``password`` uses PGPASSWORD; ``entra`` fetches an Entra token for Azure PostgreSQL."""

    PASSWORD = "password"  # noqa: S105 - a mode name, not a credential
    ENTRA = "entra"


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
    database_auth: DatabaseAuth = DatabaseAuth.PASSWORD

    # Microsoft Graph (GRAPH_BACKEND=graph). App-only, certificate credential;
    # the certificate file stays outside the repo.
    graph_auth: GraphAuth = GraphAuth.CERTIFICATE
    graph_tenant_id: str | None = None
    graph_client_id: str | None = None
    graph_certificate_path: str | None = None

    # Azure AI services in the Finishes tenant, reached with Entra tokens.
    azure_openai_endpoint: str | None = None
    azure_openai_api_version: str = "2024-10-21"
    azure_openai_chat_deployment: str | None = None
    azure_openai_embedding_deployment: str = "text-embedding-3-large"
    azure_language_endpoint: str | None = None
    azure_content_safety_endpoint: str | None = None

    # Attachment text extraction without OCR or malware scanning: an exception
    # to L11 approved by Matthew on 2026-10-28 for the first real-mail run.
    attachment_extraction_enabled: bool = False
    attachment_max_bytes: int = 25 * 1024 * 1024
    sensitive_data_policy: SensitiveDataPolicy = SensitiveDataPolicy.MASK
    # Keep original attachment files in Blob Storage (evidence account, container
    # "attachments"), without malware scanning: approved by Matthew on 2026-10-28
    # (CLAUDE.md section 3a). Locally they go to LOCAL_ORIGINALS_DIR instead.
    attachment_store_originals: bool = False
    # Skip obvious bulk and automated mail before any paid step (approved by
    # Matthew on 2026-10-28; ci_processing.prefilter). Mail from these sender
    # domains (comma-separated) is always processed in full.
    bulk_prefilter_enabled: bool = True
    bulk_prefilter_allow_domains: str = ""
    evidence_blob_account: str | None = None
    evidence_blob_container: str = "attachments"
    local_originals_dir: str = "local/originals"

    # Ingestion Function App switches. Capture and processing stay off until
    # someone turns them on; the database mode (runtime_config) still applies.
    capture_enabled: bool = False
    capture_hours: float = 2.0
    capture_workers: int = 6
    processing_enabled: bool = False
    processing_minutes: float = 8.0
    processing_workers: int = 4

    # Hosted search API / MCP (Entra delegated tokens).
    entra_tenant_id: str | None = None
    search_public_url: str | None = None  # e.g. https://func-fs-ci-search-dev.azurewebsites.net
    search_api_client_id: str | None = None  # the search API app registration
    search_api_app_id_uri: str | None = None  # e.g. api://<client id>

    # Cost page: Azure Cost Management for this resource group (the admin
    # console's identity needs Cost Management Reader there).
    azure_subscription_id: str | None = None
    azure_resource_group: str = "fs-communication-intelligence"
    # The Azure OpenAI account whose deployments the console offers as models
    # (default: the first part of AZURE_OPENAI_ENDPOINT's host name).
    azure_openai_account: str | None = None

    # Admin console (apps/admin): the public SPA client the browser signs in
    # with. Tokens are for the search API registration, scope Communications.Console.
    admin_spa_client_id: str | None = None

    # Local-only principal for the MCP server and CLI (AUTH_MODE=local_test).
    local_test_principal_oid: str = "00000000-0000-0000-0000-00000000c0de"
    local_test_principal_roles: str = "Communications.Reader.All"

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

    def require(self, *names: str) -> None:
        """Raise ``SettingsError`` naming every unset setting a real backend needs."""
        missing = [n.upper() for n in names if not getattr(self, n)]
        if missing:
            raise SettingsError(f"set {', '.join(missing)} (see local/azure-dev.env.example)")

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
