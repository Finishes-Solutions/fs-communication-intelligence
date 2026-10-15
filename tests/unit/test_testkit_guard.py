"""The testkit refuses to run anywhere but local, and only with fake/synthetic backends."""

import pytest

from ci_testkit.harness import TESTKIT_ENV, TestkitError, load_testkit_settings


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in [*TESTKIT_ENV, "AUTH_MODE"]:
        monkeypatch.delenv(name, raising=False)


def test_local_defaults_are_accepted() -> None:
    s = load_testkit_settings()
    assert s.deployment_environment.value == "local"


@pytest.mark.parametrize(
    ("name", "value"),
    [("DEPLOYMENT_ENVIRONMENT", "development"), ("GRAPH_BACKEND", "graph")],
)
def test_refuses_non_local_or_real_graph(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(TestkitError):
        load_testkit_settings()
