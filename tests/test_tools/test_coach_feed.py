"""Tests for the coach-scoped TrainingPeaks activity feed wrapper."""

from unittest.mock import AsyncMock, patch

import pytest

from tp_mcp.client.http import APIResponse
from tp_mcp.tools.coach_feed import tp_list_groups

GROUPS = {
    "groups": [
        {"id": 11, "name": "Group A", "athlete_count": 2, "is_default": False},
        {"id": 12, "name": "My Athletes", "athlete_count": 3, "is_default": True},
    ],
    "count": 2,
}
USER = {"personId": 1135463}
FEED = {
    "totalHits": 2,
    "hits": [
        {"userAction": {"uniqueId": "new", "date": "2026-09-13T11:28:09Z"}},
        {"userAction": {"uniqueId": "old", "date": "2026-09-13T10:00:00Z"}},
    ],
    "statuses": [],
}


def _client(**methods):
    inst = AsyncMock()
    for name, value in methods.items():
        setattr(inst, name, AsyncMock(return_value=value))
    return inst


@pytest.mark.asyncio
async def test_list_groups_enriches_default_group_with_feed(monkeypatch):
    monkeypatch.delenv("TP_COACH_FEED_GROUP_ID", raising=False)
    inst = _client(
        _get_user_data=USER,
        get=APIResponse(success=True, data=FEED),
    )
    with patch("tp_mcp.tools.coach_feed._tp_list_groups", AsyncMock(return_value=GROUPS)):
        with patch("tp_mcp.tools.coach_feed.TPClient") as mc:
            mc.return_value.__aenter__.return_value = inst
            out = await tp_list_groups()

    assert out["groups"] == GROUPS["groups"]
    assert out["feed"]["group_id"] == 12
    assert out["feed"]["totalHits"] == 2
    assert [h["userAction"]["uniqueId"] for h in out["feed"]["hits"]] == ["new", "old"]
    inst.get.assert_awaited_once_with(
        "/fitness/v1/coaches/1135463/athletegroups/12/feed"
    )


@pytest.mark.asyncio
async def test_list_groups_uses_configured_feed_group(monkeypatch):
    monkeypatch.setenv("TP_COACH_FEED_GROUP_ID", "11")
    inst = _client(
        _get_user_data=USER,
        get=APIResponse(success=True, data=FEED),
    )
    with patch("tp_mcp.tools.coach_feed._tp_list_groups", AsyncMock(return_value=GROUPS)):
        with patch("tp_mcp.tools.coach_feed.TPClient") as mc:
            mc.return_value.__aenter__.return_value = inst
            out = await tp_list_groups()

    assert out["feed"]["group_id"] == 11
    inst.get.assert_awaited_once_with(
        "/fitness/v1/coaches/1135463/athletegroups/11/feed"
    )


@pytest.mark.asyncio
async def test_list_groups_reports_missing_configured_group_without_api_call(monkeypatch):
    monkeypatch.setenv("TP_COACH_FEED_GROUP_ID", "999")
    with patch("tp_mcp.tools.coach_feed._tp_list_groups", AsyncMock(return_value=GROUPS)):
        with patch("tp_mcp.tools.coach_feed.TPClient") as mc:
            out = await tp_list_groups()

    assert out["feed"]["isError"] is True
    assert out["feed"]["error_code"] == "FEED_GROUP_NOT_FOUND"
    mc.assert_not_called()


@pytest.mark.asyncio
async def test_list_groups_preserves_group_result_when_feed_api_fails(monkeypatch):
    monkeypatch.delenv("TP_COACH_FEED_GROUP_ID", raising=False)
    inst = _client(
        _get_user_data=USER,
        get=APIResponse(success=False, message="feed unavailable"),
    )
    with patch("tp_mcp.tools.coach_feed._tp_list_groups", AsyncMock(return_value=GROUPS)):
        with patch("tp_mcp.tools.coach_feed.TPClient") as mc:
            mc.return_value.__aenter__.return_value = inst
            out = await tp_list_groups()

    assert out["groups"] == GROUPS["groups"]
    assert out["feed"]["isError"] is True
    assert out["feed"]["group_id"] == 12
