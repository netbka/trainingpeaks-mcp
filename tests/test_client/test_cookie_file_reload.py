"""End-to-end: the HTTP client re-reads TP_AUTH_COOKIE_FILE on token refresh.

Proves that replacing the cookie file (no process restart) makes the next
cookie->token exchange use the new cookie value.
"""

import httpx
import pytest

from tp_mcp.client.http import TPClient


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    TPClient._shared_token_cache = None
    monkeypatch.delenv("TP_AUTH_COOKIE", raising=False)
    yield
    TPClient._shared_token_cache = None


@pytest.mark.asyncio
async def test_token_exchange_rereads_cookie_file(tmp_path, monkeypatch):
    cookie_file = tmp_path / "tp_auth_cookie"
    cookie_file.write_text("cookie-A")
    monkeypatch.setenv("TP_AUTH_COOKIE_FILE", str(cookie_file))

    seen_cookies: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/users/v3/token":
            seen_cookies.append(request.headers.get("Cookie", ""))
            return httpx.Response(
                200, json={"success": True, "token": {"access_token": "t", "expires_in": 3600}}
            )
        return httpx.Response(200, json={"ok": True})

    client = TPClient()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    await client._exchange_cookie_for_token()
    # replace the file - no restart
    cookie_file.write_text("cookie-B")
    client._token_cache.clear()
    await client._exchange_cookie_for_token()

    await client.close()

    assert seen_cookies == [
        "Production_tpAuth=cookie-A",
        "Production_tpAuth=cookie-B",
    ]
