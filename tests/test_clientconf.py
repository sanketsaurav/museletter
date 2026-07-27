import pytest

from museletter import clientconf


@pytest.fixture(autouse=True)
def tmp_config(monkeypatch, tmp_path):
    monkeypatch.setattr(clientconf, "CONFIG_PATH", tmp_path / "config.toml")
    for var in ("MUSELETTER_URL", "MUSELETTER_API_KEY", "MUSELETTER_PROFILE"):
        monkeypatch.delenv(var, raising=False)


def test_token_roundtrip():
    token = clientconf.encode_token("https://news.example.com/", "sekret-key")
    assert token.startswith("ml_")
    url, key = clientconf.decode_token(token)
    assert url == "https://news.example.com"  # trailing slash trimmed
    assert key == "sekret-key"


def test_decode_token_rejects_garbage():
    for bad in ("", "nope", "ml_!!!!", "ml_" + "x" * 5):
        with pytest.raises(clientconf.ConfigError):
            clientconf.decode_token(bad)


def test_save_and_resolve_default_profile():
    clientconf.save_profile("default", "https://a.example.com", "k1")
    url, key, source = clientconf.resolve()
    assert (url, key, source) == ("https://a.example.com", "k1", "profile:default")


def test_multiple_profiles_and_selection():
    clientconf.save_profile("personal", "https://p.example.com", "kp")
    clientconf.save_profile("client", "https://c.example.com", "kc", make_default=False)
    # default stays the first one saved
    assert clientconf.resolve()[0] == "https://p.example.com"
    # explicit profile selection
    assert clientconf.resolve("client")[0] == "https://c.example.com"


def test_env_overrides_profiles(monkeypatch):
    clientconf.save_profile("default", "https://saved.example.com", "k1")
    monkeypatch.setenv("MUSELETTER_URL", "https://env.example.com")
    monkeypatch.setenv("MUSELETTER_API_KEY", "envkey")
    url, key, source = clientconf.resolve()
    assert (url, key, source) == ("https://env.example.com", "envkey", "env")


def test_profile_env_selects_profile(monkeypatch):
    clientconf.save_profile("work", "https://work.example.com", "kw", make_default=False)
    monkeypatch.setenv("MUSELETTER_PROFILE", "work")
    assert clientconf.resolve()[0] == "https://work.example.com"


def test_resolve_errors_when_unconfigured():
    with pytest.raises(clientconf.ConfigError, match="no server configured"):
        clientconf.resolve()


def test_unknown_profile_errors():
    clientconf.save_profile("default", "https://a.example.com", "k1")
    with pytest.raises(clientconf.ConfigError, match="unknown profile"):
        clientconf.resolve("nope")


def test_backward_compat_flat_config(monkeypatch, tmp_path):
    path = tmp_path / "flat.toml"
    path.write_text('url = "https://old.example.com"\napi_key = "oldkey"\n')
    monkeypatch.setattr(clientconf, "CONFIG_PATH", path)
    url, key, source = clientconf.resolve()
    assert url == "https://old.example.com"
    assert key == "oldkey"
    assert source == "profile:default"


def test_config_file_is_chmod_600():
    clientconf.save_profile("default", "https://a.example.com", "k1")
    assert (clientconf.CONFIG_PATH.stat().st_mode & 0o777) == 0o600
