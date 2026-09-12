"""File-based credential source for TrainingPeaks authentication.

Reads the Production_tpAuth cookie from a plain file whose path is given by
the TP_AUTH_COOKIE_FILE environment variable. Intended for container / runtime
secret delivery: a host-side process writes the cookie to a bind-mounted file
and the MCP process picks up changes without a restart.

SECURITY:
- The cookie value is only ever returned in CredentialResult.cookie, never in
  a message, log line or exception.
- The file path itself is not secret and may appear in messages.

HOT RELOAD:
- The file is read fresh on every get_credential_file() call. Nothing is
  cached. This lets the host-side refresher replace the file and have the next
  token exchange use the new cookie without restarting the container.
"""

import os
from pathlib import Path

from tp_mcp.auth.keyring import CredentialResult

ENV_FILE_VAR_NAME = "TP_AUTH_COOKIE_FILE"


def is_file_source_configured() -> bool:
    """True when TP_AUTH_COOKIE_FILE is set to a non-empty path."""
    return bool(os.environ.get(ENV_FILE_VAR_NAME, "").strip())


def get_credential_file() -> CredentialResult:
    """Read the auth cookie from the file named by TP_AUTH_COOKIE_FILE.

    Read fresh every call - no caching - so a replaced file is seen without a
    process restart.

    Returns:
        CredentialResult:
        - not configured / missing file / empty file -> success=False, no cookie
          (caller falls through to the next backend)
        - readable non-empty file -> success=True with the cookie
        - unreadable file (permissions, decode error) -> success=False, no cookie,
          message carries only the error type, never file contents
    """
    raw_path = os.environ.get(ENV_FILE_VAR_NAME, "").strip()
    if not raw_path:
        return CredentialResult(success=False, message="TP_AUTH_COOKIE_FILE not set")

    path = Path(raw_path)
    try:
        if not path.is_file():
            return CredentialResult(
                success=False,
                message=f"Cookie file not found: {raw_path}",
            )
        cookie = path.read_text(encoding="utf-8").strip()
    except OSError as e:
        return CredentialResult(
            success=False,
            message=f"Could not read cookie file ({type(e).__name__})",
        )
    except UnicodeDecodeError:
        return CredentialResult(
            success=False,
            message="Cookie file is not valid UTF-8 text",
        )

    if not cookie:
        return CredentialResult(
            success=False,
            message="Cookie file is empty",
        )

    return CredentialResult(
        success=True,
        message="Credential from cookie file",
        cookie=cookie,
    )
