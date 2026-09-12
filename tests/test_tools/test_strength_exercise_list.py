"""Tests for the read-only full Strength Builder exercise-library tool."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tp_mcp.client.http import APIResponse
from tp_mcp.tools.strength_exercise_list import tp_list_strength_exercises


def _mock_tp_client():
    mock_client = AsyncMock()
    mock_client.ensure_athlete_id = AsyncMock(return_value=123456)
    mock_client._ensure_access_token = AsyncMock(return_value=APIResponse(success=True))
    cache = MagicMock()
    cache.access_token = "test_access_token"
    mock_client._token_cache = cache
    return mock_client


@pytest.mark.asyncio
async def test_lists_live_builtin_custom_and_parameter_definitions():
    library = {
        "exercises": {
            "131": {
                "id": "131",
                "title": "Back Squat",
                "searchText": "Back Squat Quads",
                "alternateTitles": ["Squat"],
                "primaryMuscleGroups": ["Quads"],
                "secondaryMuscleGroups": ["Glute"],
                "canEdit": False,
                "ownerId": None,
            },
            "628875": {
                "id": "628875",
                "title": "жим платформы",
                "searchText": "жим платформы Quads",
                "alternateTitles": [],
                "primaryMuscleGroups": ["Quads"],
                "secondaryMuscleGroups": [],
                "canEdit": True,
                "ownerId": 5269270,
            },
        },
        "muscle_groups": ["Quads", "Glute"],
        "block_types": ["SingleExercise", "Superset"],
    }
    parameters = {
        "Reps": {
            "parameter": "Reps",
            "title": "Reps",
            "category": "Reps",
            "unit": {"title": "Reps", "abbreviation": "", "unit": "Reps"},
            "range": None,
        },
        "RIR": {
            "parameter": "RIR",
            "title": "RIR",
            "category": "RIR",
            "unit": {"title": "RIR", "abbreviation": "", "unit": "RepsInReserve"},
            "range": {"min": 1.0, "max": 100.0},
        },
    }

    with patch("tp_mcp.tools.strength_exercise_list.TPClient") as mtp:
        mtp.return_value.__aenter__.return_value = _mock_tp_client()
        with patch(
            "tp_mcp.tools.strength_exercise_list.fetch_live_library_content",
            new=AsyncMock(return_value=library),
        ), patch(
            "tp_mcp.tools.strength_exercise_list.fetch_exercise_parameter_catalog",
            new=AsyncMock(return_value=parameters),
        ):
            result = await tp_list_strength_exercises()

    assert result["count"] == 2
    custom = next(item for item in result["exercises"] if item["exercise_id"] == "628875")
    assert custom["custom"] is True
    assert custom["owner_id"] == 5269270
    assert result["muscle_groups"] == ["Quads", "Glute"]
    assert result["parameter_definitions"][1]["range"] == {"min": 1.0, "max": 100.0}


@pytest.mark.asyncio
async def test_live_api_error_is_not_hidden_by_baked_fallback():
    with patch("tp_mcp.tools.strength_exercise_list.TPClient") as mtp:
        mtp.return_value.__aenter__.return_value = _mock_tp_client()
        with patch(
            "tp_mcp.tools.strength_exercise_list.fetch_live_library_content",
            new=AsyncMock(side_effect=RuntimeError("libraryContent returned 500")),
        ):
            result = await tp_list_strength_exercises()

    assert result["error_code"] == "API_ERROR"
    assert "libraryContent" in result["message"]
