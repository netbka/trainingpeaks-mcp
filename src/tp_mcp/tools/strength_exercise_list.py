"""Read-only live Strength Builder exercise-library listing.

This is intentionally separate from ``tp_search_exercises``: callers such as
EZRUN's strength planner need one current account-scoped catalogue snapshot,
including caller-owned custom exercises, rather than N name searches against
the baked fallback catalogue.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from tp_mcp.client import TPClient
from tp_mcp.tools.strength_exercises import (
    STRENGTH_TIMEOUT,
    _access,
    fetch_exercise_parameter_catalog,
    fetch_live_library_content,
)

logger = logging.getLogger("tp-mcp")


def _err(code: str, message: str) -> dict[str, Any]:
    return {"isError": True, "error_code": code, "message": message}


async def tp_list_strength_exercises() -> dict[str, Any]:
    """Return the current combined Strength Builder library for the caller.

    The result is account-scoped (not athlete-scoped) and comes only from the
    live TrainingPeaks ``libraryContent`` endpoint, so caller-owned custom
    exercises are present immediately. Unlike ``tp_search_exercises`` there is
    deliberately no baked fallback: a stale snapshot would be misleading to a
    caller that asked for the authoritative current library.

    Returns slim exercise search metadata plus the current muscle groups,
    block types and exercise-parameter definitions used by TrainingPeaks'
    custom-exercise editor.
    """

    async with TPClient() as client:
        _, access, err = await _access(client)
        if err:
            return err
        try:
            async with httpx.AsyncClient(timeout=STRENGTH_TIMEOUT) as h:
                library = await fetch_live_library_content(access, h)
                parameters = await fetch_exercise_parameter_catalog(access, h)
        except httpx.TimeoutException:
            return _err("NETWORK_ERROR", "Strength exercise library read timed out.")
        except httpx.RequestError:
            logger.exception("Network error reading strength exercise library")
            return _err("NETWORK_ERROR", "A network error occurred.")
        except (RuntimeError, ValueError) as exc:
            return _err("API_ERROR", str(exc))

    exercises = []
    for row in library["exercises"].values():
        exercises.append(
            {
                "exercise_id": str(row.get("id") or row.get("exerciseId") or ""),
                "title": row.get("title"),
                "search_text": row.get("searchText"),
                "alternate_titles": row.get("alternateTitles") or [],
                "primary_muscle_groups": row.get("primaryMuscleGroups") or [],
                "secondary_muscle_groups": row.get("secondaryMuscleGroups") or [],
                "custom": row.get("canEdit") is True and row.get("ownerId") is not None,
                "can_edit": row.get("canEdit") is True,
                "owner_id": row.get("ownerId"),
            }
        )
    exercises.sort(key=lambda row: str(row.get("title") or "").casefold())

    parameter_definitions = []
    for row in parameters.values():
        parameter_definitions.append(
            {
                "parameter": row.get("parameter"),
                "title": row.get("title"),
                "category": row.get("category"),
                "unit": row.get("unit"),
                "range": row.get("range"),
            }
        )

    return {
        "count": len(exercises),
        "exercises": exercises,
        "muscle_groups": library["muscle_groups"],
        "block_types": library["block_types"],
        "parameter_definitions": parameter_definitions,
    }
