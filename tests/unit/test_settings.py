"""Settings fail closed: unsafe combinations raise instead of degrading quietly."""

from datetime import UTC, datetime

import pytest

from ci_core.settings import (
    AttachmentPolicy,
    AuthMode,
    ClassifierBackend,
    EmbedderBackend,
    GraphBackend,
    SettingsError,
    load_settings,
)

SETTING_NAMES = [
    "DEPLOYMENT_ENVIRONMENT",
    "ATTACHMENT_UNSUPPORTED_POLICY",
    "CLASSIFIER_BACKEND",
    "CLASSIFIER_DECISIONS_API_APPROVED",
    "EMBEDDER",
    "EMBEDDING_DIMENSIONS",
    "SCREENER",
    "GRAPH_BACKEND",
    "AUTH_MODE",
    "EMAIL_INCLUDE_SENT",
    "EMAIL_BACKFILL_SINCE",
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in SETTING_NAMES:
        monkeypatch.delenv(name, raising=False)


def test_local_defaults_to_fakes_and_publish_partial(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEPLOYMENT_ENVIRONMENT", "local")
    s = load_settings()
    assert s.classifier_backend is ClassifierBackend.FAKE
    assert s.embedder is EmbedderBackend.FAKE
    assert s.graph_backend is GraphBackend.FIXTURE
    assert s.attachment_unsupported_policy is AttachmentPolicy.PUBLISH_PARTIAL
    assert s.auth_mode is AuthMode.ENTRA
    assert s.email_include_sent is True
    assert s.embedding_dimensions == 1536


def test_development_defaults_to_real_backends(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEPLOYMENT_ENVIRONMENT", "development")
    s = load_settings()
    assert s.classifier_backend is ClassifierBackend.AZURE_OPENAI
    assert s.graph_backend is GraphBackend.GRAPH
    assert s.attachment_unsupported_policy is AttachmentPolicy.PUBLISH_PARTIAL


def test_missing_environment_is_refused() -> None:
    with pytest.raises(SettingsError, match="deployment_environment"):
        load_settings()


def test_production_requires_explicit_attachment_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEPLOYMENT_ENVIRONMENT", "production")
    with pytest.raises(SettingsError, match="attachment_unsupported_policy"):
        load_settings()
    monkeypatch.setenv("ATTACHMENT_UNSUPPORTED_POLICY", "hold_message")
    assert load_settings().attachment_unsupported_policy is AttachmentPolicy.HOLD_MESSAGE


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("CLASSIFIER_BACKEND", "fake"),
        ("EMBEDDER", "fake"),
        ("SCREENER", "fake"),
        ("GRAPH_BACKEND", "fixture"),
    ],
)
def test_production_refuses_fake_backends(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv("DEPLOYMENT_ENVIRONMENT", "production")
    monkeypatch.setenv("ATTACHMENT_UNSUPPORTED_POLICY", "exempt_cid_inline")
    monkeypatch.setenv(name, value)
    with pytest.raises(SettingsError, match="fake backends are not allowed"):
        load_settings()


@pytest.mark.parametrize("environment", ["development", "production"])
def test_local_test_auth_only_in_local(monkeypatch: pytest.MonkeyPatch, environment: str) -> None:
    monkeypatch.setenv("DEPLOYMENT_ENVIRONMENT", environment)
    monkeypatch.setenv("ATTACHMENT_UNSUPPORTED_POLICY", "hold_message")
    monkeypatch.setenv("AUTH_MODE", "local_test")
    with pytest.raises(SettingsError, match="only allowed when DEPLOYMENT_ENVIRONMENT=local"):
        load_settings()


def test_local_test_auth_allowed_locally(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEPLOYMENT_ENVIRONMENT", "local")
    monkeypatch.setenv("AUTH_MODE", "local_test")
    assert load_settings().auth_mode is AuthMode.LOCAL_TEST


def test_decisions_api_requires_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEPLOYMENT_ENVIRONMENT", "development")
    monkeypatch.setenv("CLASSIFIER_BACKEND", "decisions_api")
    with pytest.raises(SettingsError, match="D-C1"):
        load_settings()
    monkeypatch.setenv("CLASSIFIER_DECISIONS_API_APPROVED", "true")
    assert load_settings().classifier_backend is ClassifierBackend.DECISIONS_API


def test_backfill_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEPLOYMENT_ENVIRONMENT", "local")
    assert load_settings().initial_capture_allowed is False
    monkeypatch.setenv("EMAIL_BACKFILL_SINCE", "2026-01-01T00:00:00-06:00")
    s = load_settings()
    assert s.initial_capture_allowed is True
    assert s.email_backfill_since == datetime(2026, 1, 1, 6, 0, tzinfo=UTC)
    monkeypatch.setenv("EMAIL_BACKFILL_SINCE", "2026-01-01T00:00:00")
    with pytest.raises(SettingsError, match="UTC offset"):
        load_settings()


def test_empty_values_count_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEPLOYMENT_ENVIRONMENT", "production")
    monkeypatch.setenv("ATTACHMENT_UNSUPPORTED_POLICY", "")
    with pytest.raises(SettingsError, match="attachment_unsupported_policy"):
        load_settings()
