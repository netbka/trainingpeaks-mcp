"""Coach-scoped TrainingPeaks change feed support.

The web application loads an activity feed from the coach's athlete-group
endpoint.  The MCP server already exposes ``tp_list_groups`` and its public
schema takes no arguments, so this module keeps that existing tool contract
backwards-compatible and enriches its result with the default group's feed.

EZRUN deliberately calls ``tp_list_athletes`` first, then this read-only tool:
the roster establishes athlete identity/mapping, while the feed says which of
those athletes changed.
"""

from __future__ import annotations

import os
from typing import Any

from tp_mcp.client import TPClient
from tp_mcp.tools.groups import tp_list_groups as _tp_list_groups

_FEED_ENDPOINT = "/fitness/v1/coaches/{coach_id}/athletegroups/{group_id}/feed"


def _select_feed_group(groups: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Choose the configured group, otherwise the TrainingPeaks default group."""
    configured = (os.getenv("TP_COACH_FEED_GROUP_ID") or "").strip()
    if configured:
        try:
            configured_id = int(configured)
        except ValueError:
            return None
        return next((group for group in groups if group.get("id") == configured_id), None)

    return next((group for group in groups if group.get("is_default") is True), None)


async def tp_list_groups() -> dict[str, Any]:
    """List coach groups and include the selected group's native activity feed.

    Existing ``groups`` and ``count`` fields are preserved.  ``feed`` is an
    additive object with ``group_id``, ``totalHits``, ``hits`` and ``statuses``.
    On a feed-only failure the group list remains usable and ``feed`` carries an
    error envelope instead of turning the whole tool result into an MCP error.
    """
    result = await _tp_list_groups()
    if result.get("isError"):
        return result

    groups = result.get("groups") if isinstance(result.get("groups"), list) else []
    group = _select_feed_group(groups)
    if group is None:
        configured = (os.getenv("TP_COACH_FEED_GROUP_ID") or "").strip()
        message = (
            f"TP_COACH_FEED_GROUP_ID={configured!r} is not present in the coach groups."
            if configured
            else "No default TrainingPeaks athlete group was returned."
        )
        return {
            **result,
            "feed": {
                "isError": True,
                "error_code": "FEED_GROUP_NOT_FOUND",
                "message": message,
            },
        }

    group_id = group.get("id")
    if not isinstance(group_id, int):
        return {
            **result,
            "feed": {
                "isError": True,
                "error_code": "FEED_GROUP_INVALID",
                "message": "The selected TrainingPeaks athlete group has no numeric id.",
            },
        }

    async with TPClient() as client:
        user_data = await client._get_user_data()
        if not user_data or not user_data.get("personId"):
            return {
                **result,
                "feed": {
                    "isError": True,
                    "error_code": "AUTH_INVALID",
                    "message": "Could not resolve the coach account. Re-authenticate.",
                },
            }

        coach_id = user_data["personId"]
        response = await client.get(
            _FEED_ENDPOINT.format(coach_id=coach_id, group_id=group_id)
        )
        if response.is_error:
            return {
                **result,
                "feed": {
                    "isError": True,
                    "error_code": (
                        response.error_code.value if response.error_code else "API_ERROR"
                    ),
                    "message": response.message,
                    "group_id": group_id,
                },
            }

        payload = response.data if isinstance(response.data, dict) else {}
        hits = payload.get("hits") if isinstance(payload.get("hits"), list) else []
        statuses = (
            payload.get("statuses") if isinstance(payload.get("statuses"), list) else []
        )
        total_hits = payload.get("totalHits")

        return {
            **result,
            "feed": {
                "group_id": group_id,
                "totalHits": total_hits if isinstance(total_hits, int) else len(hits),
                "hits": hits,
                "statuses": statuses,
            },
        }
