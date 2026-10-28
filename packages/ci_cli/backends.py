"""Build the configured backends (fake or Azure) from settings."""

from __future__ import annotations

from ci_core.settings import (
    ClassifierBackend,
    EmbedderBackend,
    GraphBackend,
    ScreenerBackend,
    Settings,
    SettingsError,
)
from ci_email.fixture import FixtureGraphClient
from ci_email.graph import GraphClient
from ci_email.process import Backends
from ci_processing.classification import Classifier, FakeClassifier
from ci_processing.embedding import Embedder, FakeEmbedder
from ci_processing.screening import FakeScreener, Screener


def graph_client(settings: Settings) -> GraphClient:
    if settings.graph_backend is GraphBackend.FIXTURE:
        return FixtureGraphClient.from_directory()
    settings.require("graph_tenant_id", "graph_client_id", "graph_certificate_path")
    from ci_email.graph_http import HttpGraphClient

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
    if settings.screener is ScreenerBackend.FAKE:
        return FakeScreener()
    settings.require("azure_language_endpoint", "azure_content_safety_endpoint")
    from ci_processing.azure import AzureScreener

    return AzureScreener(
        settings.azure_language_endpoint or "", settings.azure_content_safety_endpoint or ""
    )


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


def processing_backends(settings: Settings) -> Backends:
    return Backends(
        screener=screener(settings),
        classifier=classifier(settings),
        embedder=embedder(settings),
        graph=graph_client(settings) if settings.attachment_extraction_enabled else None,
        extraction_enabled=settings.attachment_extraction_enabled,
        attachment_max_bytes=settings.attachment_max_bytes,
    )
