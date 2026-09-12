"""Tests for live Strength Builder exercise discovery/custom authoring."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tp_mcp.client.http import APIResponse
from tp_mcp.tools.strength import tp_create_strength_workout, tp_search_exercises
from tp_mcp.tools.strength_exercises import (
    normalize_library_content,
    normalize_parameter_catalog,
    tp_create_custom_exercise,
    tp_get_exercise,
    tp_update_custom_exercise,
)

TEST_ATHLETE_ID = 123456
TEST_ACCESS_TOKEN = "test_access_token"


def _mock_tp_client():
    mock_client = AsyncMock()
    mock_client.ensure_athlete_id = AsyncMock(return_value=TEST_ATHLETE_ID)
    mock_client._ensure_access_token = AsyncMock(return_value=APIResponse(success=True))
    cache = MagicMock()
    cache.access_token = TEST_ACCESS_TOKEN
    mock_client._token_cache = cache
    return mock_client


def _response(status: int, payload):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload
    r.text = str(payload)
    return r


def _parameter_catalog():
    return [
        {
            "title": "Reps",
            "unit": {"title": "Reps", "abbreviation": "", "unit": "Reps"},
            "category": "Reps",
            "range": None,
            "parameter": "Reps",
        },
        {
            "title": "Weight kg",
            "unit": {"title": "Kilograms", "abbreviation": "kg", "unit": "Kilograms"},
            "category": "Weight",
            "range": None,
            "parameter": "WeightKg",
        },
        {
            "title": "RIR",
            "unit": {"title": "RIR", "abbreviation": "", "unit": "RepsInReserve"},
            "category": "RIR",
            "range": {"min": 1.0, "max": 100.0},
            "parameter": "RIR",
        },
    ]


def _library_payload():
    return {
        "data": {
            "exercises": [
                {
                    "exerciseId": "131",
                    "canEdit": False,
                    "ownerId": None,
                    "title": "Back Squat",
                    "searchText": "Back Squat bs squats Quads Glute",
                    "searchAttributes": {
                        "alternateTitles": ["bs", "squats"],
                        "primaryMuscleGroups": ["Quads"],
                        "secondaryMuscleGroups": ["Glute"],
                    },
                },
                {
                    "exerciseId": "628875",
                    "canEdit": True,
                    "ownerId": 5269270,
                    "title": "жим платформы",
                    "searchText": "жим платформы Quads",
                    "searchAttributes": {
                        "alternateTitles": [],
                        "primaryMuscleGroups": ["Quads"],
                        "secondaryMuscleGroups": [],
                    },
                },
            ],
            "muscleGroups": [{"title": "Quads"}, {"title": "Glute"}],
            "blocks": [{"blockType": "SingleExercise"}, {"blockType": "Superset"}],
        }
    }


class TestNormalizers:
    def test_library_contains_built_in_and_custom(self):
        out = normalize_library_content(_library_payload())
        assert set(out["exercises"]) == {"131", "628875"}
        assert out["exercises"]["131"]["canEdit"] is False
        custom = out["exercises"]["628875"]
        assert custom["canEdit"] is True
        assert custom["ownerId"] == 5269270
        assert custom["primaryMuscleGroups"] == ["Quads"]
        assert out["muscle_groups"] == ["Quads", "Glute"]
        assert out["block_types"] == ["SingleExercise", "Superset"]

    def test_parameter_catalog_keeps_current_rir_range(self):
        out = normalize_parameter_catalog(_parameter_catalog())
        assert out["RIR"]["range"] == {"min": 1.0, "max": 100.0}
        assert out["WeightKg"]["unit"]["unit"] == "Kilograms"


class TestLiveSearchAndWorkoutUse:
    @pytest.mark.asyncio
    async def test_search_uses_live_combined_library_and_finds_custom(self):
        cred = MagicMock(success=True, cookie="cookie")
        http = AsyncMock()
        http.get.return_value = _response(200, _library_payload())

        with patch("tp_mcp.tools.strength.get_credential", return_value=cred):
            with patch("tp_mcp.tools.strength.TPClient") as mtp:
                mtp.return_value.__aenter__.return_value = _mock_tp_client()
                with patch("tp_mcp.tools.strength.httpx.AsyncClient") as mh:
                    mh.return_value.__aenter__.return_value = http
                    result = await tp_search_exercises("жим")

        assert result["source"] == "live"
        assert result["custom_exercises_available"] is True
        assert result["count"] == 1
        assert result["exercises"][0]["id"] == "628875"
        assert result["exercises"][0]["custom"] is True
        assert result["exercises"][0]["owner_id"] == 5269270

    @pytest.mark.asyncio
    async def test_strength_workout_accepts_custom_id_from_live_library(self):
        blocks = [
            {
                "type": "SingleExercise",
                "exercises": [
                    {"id": "628875", "sets": [{"Reps": "8", "WeightKg": "100", "RIR": "2"}]}
                ],
            }
        ]
        http = AsyncMock()
        http.get.return_value = _response(200, _library_payload())
        http.post.return_value = _response(
            200,
            {"data": {"id": "9001", "snapshot": {"totalBlocks": 1, "totalSets": 1}}},
        )

        with patch("tp_mcp.tools.strength.TPClient") as mtp:
            mtp.return_value.__aenter__.return_value = _mock_tp_client()
            with patch("tp_mcp.tools.strength.httpx.AsyncClient") as mh:
                mh.return_value.__aenter__.return_value = http
                result = await tp_create_strength_workout(
                    date="2026-09-13",
                    title="Custom strength",
                    blocks=blocks,
                )

        assert result["workout_id"] == "9001"
        payload = http.post.call_args.kwargs["json"]
        prescription = payload["blocks"][0]["prescriptions"][0]
        assert prescription["exercise"]["id"] == "628875"
        assert prescription["exercise"]["title"] == "жим платформы"
        columns = {p["parameter"] for p in prescription["parameters"]}
        assert columns == {"Reps", "WeightKg", "RIR"}


class TestCustomExerciseCrud:
    @pytest.mark.asyncio
    async def test_create_uses_post_scaffold_then_put_and_returns_permanent_id(self):
        scaffold = {
            "data": {
                "ownerId": 5269270,
                "title": "",
                "videoUrl": "",
                "instructions": "",
                "parameters": [
                    {
                        "parameter": "Reps",
                        "title": "Reps",
                        "unit": {"title": "Reps", "abbreviation": "", "unit": "Reps"},
                        "category": "Reps",
                        "range": None,
                        "id": "scaffold-reps-id",
                    }
                ],
                "primaryMuscleGroups": None,
                "secondaryMuscleGroups": None,
                "canEdit": True,
                "id": "e064de29-1531-4ffd-b94b-967e4f951893",
            },
            "errors": {},
        }
        saved = {
            "data": {
                "ownerId": 5269270,
                "title": "жим платформы",
                "videoUrl": "https://example.test/video",
                "instructions": "technique",
                "parameters": [
                    {"parameter": "Reps", "id": "1111031"},
                    {"parameter": "WeightKg", "id": "1111032"},
                ],
                "primaryMuscleGroups": ["Quads"],
                "secondaryMuscleGroups": [],
                "canEdit": True,
                "id": "628875",
            },
            "errors": {},
        }

        http = AsyncMock()
        http.get.side_effect = [
            _response(200, _parameter_catalog()),
            _response(200, _library_payload()),
        ]
        http.post.return_value = _response(200, scaffold)
        http.put.return_value = _response(200, saved)

        with patch("tp_mcp.tools.strength_exercises.TPClient") as mtp:
            mtp.return_value.__aenter__.return_value = _mock_tp_client()
            with patch("tp_mcp.tools.strength_exercises.httpx.AsyncClient") as mh:
                mh.return_value.__aenter__.return_value = http
                result = await tp_create_custom_exercise(
                    title="жим платформы",
                    parameters=["Reps", "WeightKg"],
                    video_url="https://example.test/video",
                    instructions="technique",
                    primary_muscle_groups=["Quads"],
                    secondary_muscle_groups=[],
                )

        assert result["exercise_id"] == "628875"
        assert result["custom"] is True
        put_payload = http.put.call_args.kwargs["json"]
        assert put_payload["id"] == "e064de29-1531-4ffd-b94b-967e4f951893"
        assert put_payload["ownerId"] == 5269270
        assert put_payload["parameters"][0]["id"] == "scaffold-reps-id"
        assert "id" not in put_payload["parameters"][1]
        assert put_payload["parameters"][1]["parameter"] == "WeightKg"

    @pytest.mark.asyncio
    async def test_create_rejects_unknown_parameter_before_scaffold_mutation(self):
        http = AsyncMock()
        http.get.side_effect = [
            _response(200, _parameter_catalog()),
            _response(200, _library_payload()),
        ]
        http.post.return_value = _response(200, {"data": {"id": "scaffold", "parameters": []}})

        with patch("tp_mcp.tools.strength_exercises.TPClient") as mtp:
            mtp.return_value.__aenter__.return_value = _mock_tp_client()
            with patch("tp_mcp.tools.strength_exercises.httpx.AsyncClient") as mh:
                mh.return_value.__aenter__.return_value = http
                result = await tp_create_custom_exercise(
                    title="x",
                    parameters=["NotAParameter"],
                )

        # The API scaffold is currently created before parameter validation because
        # default parameters come from that scaffold. The invalid custom exercise
        # is never PUT/saved as a permanent library item.
        assert result["error_code"] == "VALIDATION_ERROR"
        http.put.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_rejects_builtin_read_only_exercise(self):
        current = {
            "data": {
                "id": "131",
                "ownerId": None,
                "title": "Back Squat",
                "canEdit": False,
                "parameters": [],
            }
        }
        http = AsyncMock()
        http.get.return_value = _response(200, current)

        with patch("tp_mcp.tools.strength_exercises.TPClient") as mtp:
            mtp.return_value.__aenter__.return_value = _mock_tp_client()
            with patch("tp_mcp.tools.strength_exercises.httpx.AsyncClient") as mh:
                mh.return_value.__aenter__.return_value = http
                result = await tp_update_custom_exercise("131", title="Nope")

        assert result["error_code"] == "READ_ONLY"
        http.put.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_custom_preserves_existing_parameter_id(self):
        current = {
            "data": {
                "id": "628875",
                "ownerId": 5269270,
                "title": "жим платформы",
                "videoUrl": "",
                "instructions": "",
                "parameters": [{"parameter": "Reps", "id": "1111031"}],
                "primaryMuscleGroups": ["Quads"],
                "secondaryMuscleGroups": [],
                "canEdit": True,
            }
        }
        saved = {
            "data": {
                **current["data"],
                "parameters": [
                    {"parameter": "Reps", "id": "1111031"},
                    {"parameter": "RIR", "id": "1111033"},
                ],
            }
        }
        http = AsyncMock()
        http.get.side_effect = [
            _response(200, current),
            _response(200, _library_payload()),
            _response(200, _parameter_catalog()),
        ]
        http.put.return_value = _response(200, saved)

        with patch("tp_mcp.tools.strength_exercises.TPClient") as mtp:
            mtp.return_value.__aenter__.return_value = _mock_tp_client()
            with patch("tp_mcp.tools.strength_exercises.httpx.AsyncClient") as mh:
                mh.return_value.__aenter__.return_value = http
                result = await tp_update_custom_exercise(
                    "628875",
                    parameters=["Reps", "RIR"],
                )

        assert result["parameters"] == ["Reps", "RIR"]
        put_payload = http.put.call_args.kwargs["json"]
        assert put_payload["parameters"][0]["id"] == "1111031"
        assert "id" not in put_payload["parameters"][1]

    @pytest.mark.asyncio
    async def test_get_exercise_projects_full_detail(self):
        full = {
            "data": {
                "id": "628875",
                "ownerId": 5269270,
                "title": "жим платформы",
                "videoUrl": "https://example.test/video",
                "instructions": "technique",
                "parameters": [{"parameter": "Reps", "id": "1111031"}],
                "primaryMuscleGroups": ["Quads"],
                "secondaryMuscleGroups": [],
                "canEdit": True,
            }
        }
        http = AsyncMock()
        http.get.return_value = _response(200, full)

        with patch("tp_mcp.tools.strength_exercises.TPClient") as mtp:
            mtp.return_value.__aenter__.return_value = _mock_tp_client()
            with patch("tp_mcp.tools.strength_exercises.httpx.AsyncClient") as mh:
                mh.return_value.__aenter__.return_value = http
                result = await tp_get_exercise("628875")

        assert result["exercise_id"] == "628875"
        assert result["parameters"] == ["Reps"]
        assert result["can_edit"] is True
        assert result["custom"] is True
