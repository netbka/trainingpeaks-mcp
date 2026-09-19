"""Tests for dynamic coach-scoped TrainingPeaks group feeds."""

from unittest.mock import AsyncMock, patch

import pytest

from tp_mcp.client.http import APIResponse
from tp_mcp.tools.coach_feed import tp_list_groups

GROUPS = {
    "groups": [
        {"id": 11, "name": "Group A", "athlete_count": 2, "athlete_ids": [1, 2], "is_default": False},
        {"id": 12, "name": "My Athletes", "athlete_count": 1, "athlete_ids": [3], "is_default": True},
        {"id": 13, "name": "Empty", "athlete_count": 0, "athlete_ids": [], "is_default": False},
    ],
    "count": 3,
}
USER = {"personId": 1135463}


def response(unique_id: str) -> APIResponse:
    return APIResponse(
        success=True,
        data={
            "totalHits": 1,
            "hits": [{"userAction": {"uniqueId": unique_id, "date": "2026-09-13T11:28:09Z"}}],
            "statuses": [],
        },
    )


@pytest.mark.asyncio
async def test_list_groups_fetches_every_non_empty_group_after_group_discovery():
    inst = AsyncMock()
    inst._get_user_data = AsyncMock(return_value=USER)
    inst.get = AsyncMock(side_effect=[response("a"), response("b")])
    with patch("tp_mcp.tools.coach_feed._tp_list_groups", AsyncMock(return_value=GROUPS)) as list_groups:
        with patch("tp_mcp.tools.coach_feed.TPClient") as mc:
            mc.return_value.__aenter__.return_value = inst
            out = await tp_list_groups()

    list_groups.assert_awaited_once()
    assert out["groups"] == GROUPS["groups"]
    assert out["coach_id"] == 1135463
    assert [feed["group_id"] for feed in out["feeds"]] == [11, 12]
    assert out["feed"]["group_id"] == 12
    assert inst.get.await_count == 2
    inst.get.assert_any_await("/fitness/v1/coaches/1135463/athletegroups/11/feed")
    inst.get.assert_any_await("/fitness/v1/coaches/1135463/athletegroups/12/feed")


@pytest.mark.asyncio
async def test_list_groups_skips_empty_groups():
    inst = AsyncMock()
    inst._get_user_data = AsyncMock(return_value=USER)
    inst.get = AsyncMock()
    only_empty = {"groups": [GROUPS["groups"][2]], "count": 1}
    with patch("tp_mcp.tools.coach_feed._tp_list_groups", AsyncMock(return_value=only_empty)):
        with patch("tp_mcp.tools.coach_feed.TPClient") as mc:
            mc.return_value.__aenter__.return_value = inst
            out = await tp_list_groups()

    assert out["feeds"] == []
    inst.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_one_feed_failure_does_not_hide_other_groups():
    inst = AsyncMock()
    inst._get_user_data = AsyncMock(return_value=USER)
    inst.get = AsyncMock(
        side_effect=[
            APIResponse(success=False, message="unavailable"),
            response("ok"),
        ]
    )
    with patch("tp_mcp.tools.coach_feed._tp_list_groups", AsyncMock(return_value=GROUPS)):
        with patch("tp_mcp.tools.coach_feed.TPClient") as mc:
            mc.return_value.__aenter__.return_value = inst
            out = await tp_list_groups()

    assert out["feeds"][0]["isError"] is True
    assert out["feeds"][0]["group_id"] == 11
    assert out["feeds"][1]["group_id"] == 12
    assert out["feed"]["group_id"] == 12


@pytest.mark.asyncio
async def test_feed_narrower_than_total_hits_is_still_committed():
    # The feed endpoint is a recency window, not a paginated full history:
    # totalHits routinely exceeds the returned hits even for quiet groups, so
    # a mismatch is not a completeness failure - see coach_feed.py's
    # 2026-09-20 note. The returned hits are genuine data and must be
    # committed, just flagged as `truncated` for observability.
    inst = AsyncMock()
    inst._get_user_data = AsyncMock(return_value=USER)
    inst.get = AsyncMock(
        side_effect=[
            APIResponse(
                success=True,
                data={
                    "totalHits": 3,
                    "hits": [
                        {"userAction": {"uniqueId": "only-one", "date": "2026-09-13T11:28:09Z"}}
                    ],
                    "statuses": [],
                },
            ),
            response("complete"),
        ]
    )
    with patch("tp_mcp.tools.coach_feed._tp_list_groups", AsyncMock(return_value=GROUPS)):
        with patch("tp_mcp.tools.coach_feed.TPClient") as mc:
            mc.return_value.__aenter__.return_value = inst
            out = await tp_list_groups()

    narrow = out["feeds"][0]
    assert narrow.get("isError") is not True
    assert narrow["totalHits"] == 3
    assert len(narrow["hits"]) == 1
    assert narrow["truncated"] is True
    assert out["feeds"][1]["group_id"] == 12
    assert out["feeds"][1]["truncated"] is False
