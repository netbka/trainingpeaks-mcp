"""Tests for the TP_AUTH_COOKIE_FILE credential source."""

import pytest

from tp_mcp.auth.file_source import (
    ENV_FILE_VAR_NAME,
    get_credential_file,
    is_file_source_configured,
)


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv(ENV_FILE_VAR_NAME, raising=False)
    monkeypatch.delenv("TP_AUTH_COOKIE", raising=False)


class TestIsConfigured:
    def test_not_set(self):
        assert is_file_source_configured() is False

    def test_blank(self, monkeypatch):
        monkeypatch.setenv(ENV_FILE_VAR_NAME, "   ")
        assert is_file_source_configured() is False

    def test_set(self, monkeypatch):
        monkeypatch.setenv(ENV_FILE_VAR_NAME, "/some/path")
        assert is_file_source_configured() is True


class TestGetCredentialFile:
    def test_not_configured(self):
        result = get_credential_file()
        assert result.success is False
        assert result.cookie is None

    def test_missing_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv(ENV_FILE_VAR_NAME, str(tmp_path / "nope"))
        result = get_credential_file()
        assert result.success is False
        assert result.cookie is None

    def test_empty_file(self, tmp_path, monkeypatch):
        f = tmp_path / "cookie"
        f.write_text("")
        monkeypatch.setenv(ENV_FILE_VAR_NAME, str(f))
        result = get_credential_file()
        assert result.success is False
        assert result.cookie is None

    def test_whitespace_only_file(self, tmp_path, monkeypatch):
        f = tmp_path / "cookie"
        f.write_text("\n  \n")
        monkeypatch.setenv(ENV_FILE_VAR_NAME, str(f))
        result = get_credential_file()
        assert result.success is False

    def test_reads_cookie(self, tmp_path, monkeypatch):
        f = tmp_path / "cookie"
        f.write_text("  the-cookie-value\n")
        monkeypatch.setenv(ENV_FILE_VAR_NAME, str(f))
        result = get_credential_file()
        assert result.success is True
        assert result.cookie == "the-cookie-value"

    def test_hot_reload_between_calls(self, tmp_path, monkeypatch):
        """A file replaced between two calls yields the new value - no caching."""
        f = tmp_path / "cookie"
        f.write_text("cookie-A")
        monkeypatch.setenv(ENV_FILE_VAR_NAME, str(f))

        first = get_credential_file()
        assert first.cookie == "cookie-A"

        f.write_text("cookie-B")
        second = get_credential_file()
        assert second.cookie == "cookie-B"

    def test_cookie_not_in_message_on_success(self, tmp_path, monkeypatch):
        f = tmp_path / "cookie"
        f.write_text("SECRET_COOKIE_XYZ")
        monkeypatch.setenv(ENV_FILE_VAR_NAME, str(f))
        result = get_credential_file()
        assert "SECRET_COOKIE_XYZ" not in result.message
        assert "SECRET_COOKIE_XYZ" not in repr(result)

    def test_directory_instead_of_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv(ENV_FILE_VAR_NAME, str(tmp_path))
        result = get_credential_file()
        assert result.success is False
        assert result.cookie is None
