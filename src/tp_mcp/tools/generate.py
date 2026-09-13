"""Pure generator: TrainingPeaks AI structured-workout builder.

Calls the Peaksware workout-analysis "generate/workout" endpoint, which turns a
natural-language workout description into a TrainingPeaks structured-workout
object (steps, intensity/length metrics, polyline).

This tool is a PURE GENERATOR. It never creates, updates, deletes, schedules or
otherwise mutates a workout, a calendar entry or any other TrainingPeaks state.
It only returns the generated structure for a caller to inspect or hand to a
separate create/update step.

Auth mirrors ``analyze.py``: the analysis API is on a different domain than the
main TP API, so we obtain a Bearer access token via the MCP's own token
exchange (``TPClient._ensure_access_token``) and make a direct ``httpx`` call.
"""

import logging
import os
import time
from typing import Any

import httpx

from tp_mcp.client import TPClient

logger = logging.getLogger("tp-mcp")

# Full endpoint URL, overridable via TP_AI_WORKOUT_GENERATE_URL (docker/mcp-tp/.env).
_DEFAULT_GENERATE_URL = (
    "https://api.peakswaresb.com/workout-analysis/v1/generate/workout"
)
GENERATE_TIMEOUT = 60.0


def _generate_url() -> str:
    value = os.environ.get("TP_AI_WORKOUT_GENERATE_URL", "").strip()
    return value or _DEFAULT_GENERATE_URL


_MAX_USER_CONTENT = 4000


def _validation_error(message: str) -> dict[str, Any]:
    return {
        "isError": True,
        "error_code": "VALIDATION_ERROR",
        "message": message,
    }


def _error_for_status(status_code: int) -> dict[str, Any] | None:
    """Map a non-200 generate-API status to our error envelope, or None for 200."""
    if status_code == 401:
        return {
            "isError": True,
            "error_code": "AUTH_EXPIRED",
            "message": "Session expired. Run 'tp-mcp auth' to re-authenticate.",
        }
    if status_code == 404:
        return {
            "isError": True,
            "error_code": "NOT_FOUND",
            "message": "Workout generator endpoint not found.",
        }
    if status_code != 200:
        return {
            "isError": True,
            "error_code": "API_ERROR",
            "message": f"Generator API error: {status_code}",
        }
    return None


async def tp_ai_generate_workout(
    workout_type_id: int,
    primary_intensity_metric: str,
    primary_length_metric: str,
    primary_intensity_target_or_range: str,
    user_content: str,
) -> dict:
    """Generate a TrainingPeaks structured workout from a natural-language spec.

    Pure generator - does not create, schedule or modify any workout.

    Returns:
        Dict with ``workoutTypeValueId``, ``title``, ``description`` and the
        nested ``structure`` object, or an ``isError`` envelope.
    """
    # --- lightweight input validation ---
    if not isinstance(workout_type_id, int) or isinstance(workout_type_id, bool) or workout_type_id <= 0:
        return _validation_error("workout_type_id must be a positive integer.")

    for label, value in (
        ("primary_intensity_metric", primary_intensity_metric),
        ("primary_length_metric", primary_length_metric),
        ("primary_intensity_target_or_range", primary_intensity_target_or_range),
        ("user_content", user_content),
    ):
        if not isinstance(value, str) or not value.strip():
            return _validation_error(f"{label} must be a non-empty string.")

    if len(user_content) > _MAX_USER_CONTENT:
        return _validation_error(
            f"user_content is too long ({len(user_content)} chars); max {_MAX_USER_CONTENT}."
        )

    started = time.monotonic()
    logger.info("tp_ai_generate_workout start (workout_type_id=%s)", workout_type_id)

    async with TPClient() as client:
        athlete_id = await client.ensure_athlete_id()
        if not athlete_id:
            return {
                "isError": True,
                "error_code": "AUTH_INVALID",
                "message": "Could not get athlete ID. Re-authenticate.",
            }

        token_result = await client._ensure_access_token()
        if not token_result.success:
            return {
                "isError": True,
                "error_code": "AUTH_INVALID",
                "message": token_result.message or "Failed to obtain access token.",
            }

        access_token = client._token_cache.access_token
        if not access_token:
            return {
                "isError": True,
                "error_code": "AUTH_INVALID",
                "message": "No access token available. Re-authenticate.",
            }

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": "https://app.trainingpeaks.com",
            "Referer": "https://app.trainingpeaks.com/",
        }
        body = {
            "workoutTypeId": workout_type_id,
            "primaryIntensityMetric": primary_intensity_metric,
            "primaryLengthMetric": primary_length_metric,
            "primaryIntensityTargetOrRange": primary_intensity_target_or_range,
            "userContent": user_content,
        }

        async with httpx.AsyncClient(timeout=GENERATE_TIMEOUT) as http_client:
            try:
                response = await http_client.post(
                    _generate_url(),
                    headers=headers,
                    json=body,
                )
            except httpx.TimeoutException:
                logger.error("tp_ai_generate_workout timed out")
                return {
                    "isError": True,
                    "error_code": "NETWORK_ERROR",
                    "message": "Workout generation request timed out.",
                }
            except httpx.RequestError:
                logger.exception("Network error during workout generation")
                return {
                    "isError": True,
                    "error_code": "NETWORK_ERROR",
                    "message": "A network error occurred.",
                }

    err = _error_for_status(response.status_code)
    if err:
        logger.error(
            "tp_ai_generate_workout failed (status=%s, code=%s)",
            response.status_code,
            err["error_code"],
        )
        return err

    try:
        data = response.json()
    except Exception:
        logger.error("tp_ai_generate_workout: failed to parse response body")
        return {
            "isError": True,
            "error_code": "API_ERROR",
            "message": "Failed to parse generator response.",
        }

    if not isinstance(data, dict) or "structure" not in data:
        logger.error("tp_ai_generate_workout: response missing 'structure'")
        return {
            "isError": True,
            "error_code": "API_ERROR",
            "message": "Generator response missing 'structure'.",
        }

    duration_ms = int((time.monotonic() - started) * 1000)
    logger.info(
        "tp_ai_generate_workout complete (workout_type_id=%s, duration_ms=%s)",
        workout_type_id,
        duration_ms,
    )

    return {
        "workoutTypeValueId": data.get("workoutTypeValueId"),
        "title": data.get("title"),
        "description": data.get("description"),
        "structure": data.get("structure"),
    }
