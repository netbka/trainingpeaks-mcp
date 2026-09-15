"""Coach-scoped TrainingPeaks group feeds.

The group list is always fetched first. Every current non-empty group is then
read from its own native feed endpoint. Group membership belongs to the coach,
not to an athlete profile, and is returned additively for EZRUN reconciliation.
"""

from __future__ import annotations

import asyncio
from typing import Any

from tp_mcp.client import TPClient
from tp_mcp.tools.groups import tp_list_groups as _tp_list_groups

_FEED_ENDPOINT = "/fitness/v1/coaches/{coach_id}/athletegroups/{group_id}/feed"


async def _fetch_feed(
    client: TPClient, coach_id: int, group: dict[str, Any]
) -> dict[str, Any]:
    group_id = group.get("id")
    if not isinstance(group_id, int):
        return {
            "isError": True,
            "error_code": "FEED_GROUP_INVALID",
            "message": "TrainingPeaks athlete group has no numeric id.",
        }

    response = await client.get(
        _FEED_ENDPOINT.format(coach_id=coach_id, group_id=group_id)
    )
    if response.is_error:
        return {
            "group_id": group_id,
            "isError": True,
            "error_code": (
                response.error_code.value if response.error_code else "API_ERROR"
            ),
            "message": response.message,
        }

    payload = response.data if isinstance(response.data, dict) else {}
    hits = payload.get("hits") if isinstance(payload.get("hits"), list) else []
    statuses = (
        payload.get("statuses") if isinstance(payload.get("statuses"), list) else []
    )
    total_hits = payload.get("totalHits")
    total_hits = total_hits if isinstance(total_hits, int) else len(hits)
    if total_hits > len(hits):
        return {
            "group_id": group_id,
            "isError": True,
            "error_code": "FEED_INCOMPLETE",
            "message": (
                f"TrainingPeaks reported {total_hits} feed hits but returned "
                f"only {len(hits)}. The response is not committed so no "
                "events are silently lost."
            ),
            "totalHits": total_hits,
            "returnedHits": len(hits),
        }
    return {
        "group_id": group_id,
        "totalHits": total_hits,
        "hits": hits,
        "statuses": statuses,
    }


async def tp_list_groups() -> dict[str, Any]:
    """List all current coach groups and attach every non-empty group feed.

    Existing groups and count fields remain compatible. feeds is the dynamic
    collection. The legacy singular feed remains an alias for the default group
    during rollout. Individual feed failures stay isolated.
    """
    result = await _tp_list_groups()
    if result.get("isError"):
        return result

    groups = result.get("groups") if isinstance(result.get("groups"), list) else []
    non_empty = [
        group
        for group in groups
        if isinstance(group, dict)
        and isinstance(group.get("id"), int)
        and isinstance(group.get("athlete_ids"), list)
        and len(group["athlete_ids"]) > 0
    ]

    async with TPClient() as client:
        user_data = await client._get_user_data()
        if not user_data or not user_data.get("personId"):
            return {
                **result,
                "feeds": [],
                "feed": {
                    "isError": True,
                    "error_code": "AUTH_INVALID",
                    "message": "Could not resolve the coach account. Re-authenticate.",
                },
            }

        coach_id = user_data["personId"]
        feeds = (
            await asyncio.gather(
                *[_fetch_feed(client, coach_id, group) for group in non_empty]
            )
            if non_empty
            else []
        )

    default_group = next(
        (group for group in groups if group.get("is_default") is True), None
    )
    default_id = default_group.get("id") if isinstance(default_group, dict) else None
    default_feed = next(
        (feed for feed in feeds if feed.get("group_id") == default_id), None
    )

    enriched = {**result, "coach_id": coach_id, "feeds": feeds}
    if default_feed is not None:
        enriched["feed"] = default_feed
    return enriched
