"""Build the configured backends (fake or Azure) from settings."""

from __future__ import annotations

from ci_core.settings import (
    ClassifierBackend,
    DeploymentEnvironment,
    EmbedderBackend,
    GraphAuth,
    GraphBackend,
    ScreenerBackend,
    SensitiveDataPolicy,
    Settings,
    SettingsError,
)
from ci_email.fixture import FixtureGraphClient
from ci_email.graph import GraphClient
from ci_email.process import Backends
from ci_processing.classification import Classifier, FakeClassifier
from ci_processing.embedding import Embedder, FakeEmbedder
from ci_processing.originals import AzureBlobStore, DirectoryStore, OriginalStore
from ci_processing.redaction import FakeRedactor, Redactor
from ci_processing.routing import FakePersonRouter, PersonRouter
from ci_processing.screening import FakeScreener, Screener


def graph_client(settings: Settings) -> GraphClient:
    if settings.graph_backend is GraphBackend.FIXTURE:
        return FixtureGraphClient.from_directory()
    from ci_email.graph_http import HttpGraphClient

    if settings.graph_auth is GraphAuth.MANAGED_IDENTITY:
        from ci_core.azure_auth import default_credential

        settings.require("graph_tenant_id")
        return HttpGraphClient(
            settings.graph_tenant_id or "", "", "", credential=default_credential()
        )
    settings.require("graph_tenant_id", "graph_client_id", "graph_certificate_path")

    return HttpGraphClient(
        settings.graph_tenant_id or "",
        settings.graph_client_id or "",
        settings.graph_certificate_path or "",
    )


def embedder(settings: Settings) -> Embedder:
    if settings.embedder is EmbedderBackend.FAKE:
        return FakeEmbedder(settings.embedding_dimensions)
    settings.require("azure_openai_endpoint")
    from ci_processing.azure import AzureOpenAIEmbedder

    return AzureOpenAIEmbedder(
        settings.azure_openai_endpoint or "",
        settings.azure_openai_embedding_deployment,
        settings.azure_openai_api_version,
        settings.embedding_dimensions,
    )


def screener(settings: Settings) -> Screener:
    # With masking, sensitive numbers are masked by the redactor, not rejected here.
    check_pii = settings.sensitive_data_policy is SensitiveDataPolicy.RESTRICT
    if settings.screener is ScreenerBackend.FAKE:
        return FakeScreener(check_pii=check_pii)
    settings.require("azure_language_endpoint", "azure_content_safety_endpoint")
    from ci_processing.azure import AzureScreener

    return AzureScreener(
        settings.azure_language_endpoint or "",
        settings.azure_content_safety_endpoint or "",
        check_pii=check_pii,
    )


def redactor(settings: Settings) -> Redactor | None:
    """None when SENSITIVE_DATA_POLICY=restrict (messages are restricted instead)."""
    if settings.sensitive_data_policy is SensitiveDataPolicy.RESTRICT:
        return None
    if settings.screener is ScreenerBackend.FAKE:
        return FakeRedactor()
    settings.require("azure_language_endpoint")
    from ci_processing.azure import AzureRedactor

    return AzureRedactor(settings.azure_language_endpoint or "")


def classifier(settings: Settings) -> Classifier:
    if settings.classifier_backend is ClassifierBackend.FAKE:
        return FakeClassifier()
    if settings.classifier_backend is ClassifierBackend.DECISIONS_API:
        raise SettingsError("CLASSIFIER_BACKEND=decisions_api is not implemented")
    settings.require("azure_openai_endpoint", "azure_openai_chat_deployment")
    from ci_processing.azure import AzureOpenAIClassifier

    return AzureOpenAIClassifier(
        settings.azure_openai_endpoint or "",
        settings.azure_openai_chat_deployment or "",
        settings.azure_openai_api_version,
    )


def original_store(settings: Settings) -> OriginalStore | None:
    """Where original attachment files live (None when not configured)."""
    if settings.deployment_environment is DeploymentEnvironment.LOCAL:
        return DirectoryStore(settings.local_originals_dir)
    if settings.evidence_blob_account:
        return AzureBlobStore(settings.evidence_blob_account, settings.evidence_blob_container)
    return None


def person_router(settings: Settings) -> PersonRouter:
    """Step 2 of action items. It only runs for people with a profile in action_people."""
    if settings.classifier_backend is ClassifierBackend.FAKE:
        return FakePersonRouter()
    settings.require("azure_openai_endpoint", "azure_openai_chat_deployment")
    from ci_processing.azure import AzureOpenAIPersonRouter

    return AzureOpenAIPersonRouter(
        settings.azure_openai_endpoint or "",
        settings.azure_openai_chat_deployment or "",
        settings.azure_openai_api_version,
    )


def processing_backends(settings: Settings) -> Backends:
    return Backends(
        screener=screener(settings),
        classifier=classifier(settings),
        embedder=embedder(settings),
        graph=graph_client(settings) if settings.attachment_extraction_enabled else None,
        extraction_enabled=settings.attachment_extraction_enabled,
        attachment_max_bytes=settings.attachment_max_bytes,
        originals=original_store(settings) if settings.attachment_store_originals else None,
        redactor=redactor(settings),
        router=person_router(settings),
    )
