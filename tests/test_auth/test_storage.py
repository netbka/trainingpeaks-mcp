"""Tests for the unified credential storage precedence.

Precedence under test:
    1. TP_AUTH_COOKIE
    2. TP_AUTH_COOKIE_FILE
    3. system keyring
    4. encrypted file
"""

import pytest

from tp_mcp.auth import storage
from tp_mcp.auth.keyring import CredentialResult


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("TP_AUTH_COOKIE", raising=False)
    monkeypatch.delenv("TP_AUTH_COOKIE_FILE", raising=False)


@pytest.fixture
def no_stored_backends(monkeypatch):
    """keyring unavailable, encrypted file empty."""
    monkeypatch.setattr(storage, "is_keyring_available", lambda: False)
    monkeypatch.setattr(
        storage,
        "get_credential_encrypted",
        lambda: CredentialResult(success=False, message="No credential file found"),
    )


class TestGetCredentialPrecedence:
    def test_env_var_beats_file(self, tmp_path, monkeypatch, no_stored_backends):
        f = tmp_path / "cookie"
        f.write_text("from-file")
        monkeypatch.setenv("TP_AUTH_COOKIE", "from-env")
        monkeypatch.setenv("TP_AUTH_COOKIE_FILE", str(f))

        result = storage.get_credential()
        assert result.success is True
        assert result.cookie == "from-env"

    def test_file_beats_keyring_and_encrypted(self, tmp_path, monkeypatch):
        f = tmp_path / "cookie"
        f.write_text("from-file")
        monkeypatch.setenv("TP_AUTH_COOKIE_FILE", str(f))
        monkeypatch.setattr(storage, "is_keyring_available", lambda: True)
        monkeypatch.setattr(
            storage,
            "get_credential_keyring",
            lambda: CredentialResult(success=True, message="kr", cookie="from-keyring"),
        )
        monkeypatch.setattr(
            storage,
            "get_credential_encrypted",
            lambda: CredentialResult(success=True, message="enc", cookie="from-encrypted"),
        )

        result = storage.get_credential()
        assert result.cookie == "from-file"

    def test_falls_through_to_keyring_when_file_missing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TP_AUTH_COOKIE_FILE", str(tmp_path / "absent"))
        monkeypatch.setattr(storage, "is_keyring_available", lambda: True)
        monkeypatch.setattr(
            storage,
            "get_credential_keyring",
            lambda: CredentialResult(success=True, message="kr", cookie="from-keyring"),
        )

        result = storage.get_credential()
        assert result.cookie == "from-keyring"

    def test_falls_through_to_encrypted_when_file_empty(self, tmp_path, monkeypatch):
        f = tmp_path / "cookie"
        f.write_text("")
        monkeypatch.setenv("TP_AUTH_COOKIE_FILE", str(f))
        monkeypatch.setattr(storage, "is_keyring_available", lambda: False)
        monkeypatch.setattr(
            storage,
            "get_credential_encrypted",
            lambda: CredentialResult(success=True, message="enc", cookie="from-encrypted"),
        )

        result = storage.get_credential()
        assert result.cookie == "from-encrypted"

    def test_hot_reload_via_get_credential(self, tmp_path, monkeypatch, no_stored_backends):
        f = tmp_path / "cookie"
        f.write_text("cookie-A")
        monkeypatch.setenv("TP_AUTH_COOKIE_FILE", str(f))

        assert storage.get_credential().cookie == "cookie-A"
        f.write_text("cookie-B")
        assert storage.get_credential().cookie == "cookie-B"


class TestGetStorageBackend:
    def test_environment(self, monkeypatch):
        monkeypatch.setenv("TP_AUTH_COOKIE", "x")
        assert storage.get_storage_backend() == "environment"

    def test_cookie_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TP_AUTH_COOKIE_FILE", str(tmp_path / "cookie"))
        assert storage.get_storage_backend() == "cookie_file"

    def test_keyring(self, monkeypatch):
        monkeypatch.setattr(storage, "is_keyring_available", lambda: True)
        assert storage.get_storage_backend() == "keyring"

    def test_encrypted_file(self, monkeypatch):
        monkeypatch.setattr(storage, "is_keyring_available", lambda: False)
        assert storage.get_storage_backend() == "encrypted_file"
