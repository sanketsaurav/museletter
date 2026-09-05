import pytest

from museletter.config import Settings


def test_attribution_defaults_on(monkeypatch):
    monkeypatch.delenv("MUSELETTER_ATTRIBUTION", raising=False)
    assert Settings().attribution is True
    assert Settings.from_env().attribution is True


@pytest.mark.parametrize("value", ["false", "FALSE", "0", "no"])
def test_attribution_env_off(monkeypatch, value):
    monkeypatch.setenv("MUSELETTER_ATTRIBUTION", value)
    assert Settings.from_env().attribution is False


@pytest.mark.parametrize("value", ["true", "1", "yes"])
def test_attribution_env_on(monkeypatch, value):
    monkeypatch.setenv("MUSELETTER_ATTRIBUTION", value)
    assert Settings.from_env().attribution is True
