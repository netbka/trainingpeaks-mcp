"""Tests for the tp_ai_generate_workout pure-generator tool."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tp_mcp.client.http import APIResponse
from tp_mcp.tools.generate import tp_ai_generate_workout

TEST_ATHLETE_ID = 123456
TEST_ACCESS_TOKEN = "gAAAA_test_access_token_12345"

_VALID_ARGS = {
    "workout_type_id": 3,
    "primary_intensity_metric": "percentOfThresholdHr",
    "primary_length_metric": "duration",
    "primary_intensity_target_or_range": "range",
    "user_content": "45 minute easy recovery run in zone 1",
}


def _mock_tp_client(athlete_id=TEST_ATHLETE_ID, access_token=TEST_ACCESS_TOKEN):
    mock_client = AsyncMock()
    mock_client.ensure_athlete_id = AsyncMock(return_value=athlete_id)
    mock_client._ensure_access_token = AsyncMock(return_value=APIResponse(success=True))
    mock_token_cache = MagicMock()
    mock_token_cache.access_token = access_token
    mock_client._token_cache = mock_token_cache
    return mock_client


def _sample_response():
    return {
        "workoutTypeValueId": 3,
        "structure": {
            "structure": [
                {"type": "step", "length": {"value": 2700, "unit": "second"}},
            ],
            "primaryLengthMetric": "duration",
            "primaryIntensityMetric": "percentOfThresholdHr",
            "primaryIntensityTargetOrRange": "range",
            "polyline": [],
        },
        "title": "Recovery Run",
        "description": "Easy 45 minute recovery run.",
    }


def _mock_http_client(status=200, body=None, raise_json=False):
    resp = MagicMock()
    resp.status_code = status
    if raise_json:
        resp.json.side_effect = ValueError("not json")
    else:
        resp.json.return_value = body if body is not None else _sample_response()
    http_client = AsyncMock()
    http_client.post.return_value = resp
    return http_client


@pytest.mark.asyncio
async def test_success():
    mock_client = _mock_tp_client()
    mock_http = _mock_http_client()
    with patch("tp_mcp.tools.generate.TPClient") as tp, \
         patch("tp_mcp.tools.generate.httpx.AsyncClient") as hx:
        tp.return_value.__aenter__.return_value = mock_client
        hx.return_value.__aenter__.return_value = mock_http
        result = await tp_ai_generate_workout(**_VALID_ARGS)

    assert result["workoutTypeValueId"] == 3
    assert result["title"] == "Recovery Run"
    assert result["description"] == "Easy 45 minute recovery run."
    assert result["structure"]["primaryLengthMetric"] == "duration"
    assert "isError" not in result

    # pure generator: only the generate endpoint was hit, once
    assert mock_http.post.call_count == 1
    called_url = mock_http.post.call_args[0][0]
    assert called_url.endswith("/workout-analysis/v1/generate/workout")


@pytest.mark.asyncio
async def test_401_returns_auth_expired():
    mock_client = _mock_tp_client()
    mock_http = _mock_http_client(status=401)
    with patch("tp_mcp.tools.generate.TPClient") as tp, \
         patch("tp_mcp.tools.generate.httpx.AsyncClient") as hx:
        tp.return_value.__aenter__.return_value = mock_client
        hx.return_value.__aenter__.return_value = mock_http
        result = await tp_ai_generate_workout(**_VALID_ARGS)

    assert result["isError"] is True
    assert result["error_code"] == "AUTH_EXPIRED"


@pytest.mark.asyncio
async def test_malformed_body_not_json():
    mock_client = _mock_tp_client()
    mock_http = _mock_http_client(raise_json=True)
    with patch("tp_mcp.tools.generate.TPClient") as tp, \
         patch("tp_mcp.tools.generate.httpx.AsyncClient") as hx:
        tp.return_value.__aenter__.return_value = mock_client
        hx.return_value.__aenter__.return_value = mock_http
        result = await tp_ai_generate_workout(**_VALID_ARGS)

    assert result["isError"] is True
    assert result["error_code"] == "API_ERROR"


@pytest.mark.asyncio
async def test_malformed_body_missing_structure():
    mock_client = _mock_tp_client()
    mock_http = _mock_http_client(body={"title": "x", "description": "y"})
    with patch("tp_mcp.tools.generate.TPClient") as tp, \
         patch("tp_mcp.tools.generate.httpx.AsyncClient") as hx:
        tp.return_value.__aenter__.return_value = mock_client
        hx.return_value.__aenter__.return_value = mock_http
        result = await tp_ai_generate_workout(**_VALID_ARGS)

    assert result["isError"] is True
    assert result["error_code"] == "API_ERROR"


@pytest.mark.asyncio
async def test_validation_empty_user_content():
    args = {**_VALID_ARGS, "user_content": "  "}
    result = await tp_ai_generate_workout(**args)
    assert result["isError"] is True
    assert result["error_code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_validation_bad_workout_type_id():
    args = {**_VALID_ARGS, "workout_type_id": 0}
    result = await tp_ai_generate_workout(**args)
    assert result["isError"] is True
    assert result["error_code"] == "VALIDATION_ERROR"
