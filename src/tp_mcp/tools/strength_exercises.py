"""TrainingPeaks strength exercise library and custom-exercise tools.

The Strength Builder exposes a live combined exercise library at
``/rx/activity/v1/libraryContent``. It contains both TrainingPeaks-owned
exercises and exercises created by the authenticated user. Custom exercises use
an explicit two-step authoring flow verified from the current web client on
2026-09-12:

1. ``POST /rx/activity/v1/exercises`` creates an editable scaffold with a UUID.
2. ``PUT /rx/activity/v1/exercises`` saves the completed document and returns a
   permanent numeric exercise id.

The selectable parameter definitions come from
``GET /rx/activity/v1/parameters/exercise``. Keep these contracts here instead
of duplicating UI-captured units/categories/ranges in callers.
"""

from __future__ import annotations

import logging
from copy import deepcopy
from typing import Any

import httpx

from tp_mcp.client import TPClient

logger = logging.getLogger("tp-mcp")

STRENGTH_API_BASE = "https://api.peakswaresb.com"
STRENGTH_TIMEOUT = 30.0


def _err(code: str, message: str) -> dict[str, Any]:
    return {"isError": True, "error_code": code, "message": message}


def _headers(access: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {access}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://app.trainingpeaks.com",
        "Referer": "https://app.trainingpeaks.com/",
    }


def _map_status(status: int, body: str, *, noun: str = "Exercise") -> dict[str, Any]:
    if status == 401:
        return _err("AUTH_EXPIRED", "Session expired. Run 'tp-mcp auth' to re-authenticate.")
    if status == 403:
        return _err("AUTH_INVALID", "Access denied. Check permissions or re-authenticate.")
    if status == 404:
        return _err("NOT_FOUND", f"{noun} not found.")
    if status == 429:
        return _err("RATE_LIMITED", "Rate limited. Please wait before retrying.")
    return _err("API_ERROR", f"Strength API error: {status} {body[:200]}")


async def _access(client: TPClient) -> tuple[int | None, str | None, dict[str, Any] | None]:
    """Resolve the authenticated caller and Peaksware bearer token.

    The exercise library belongs to the authenticated account/coach, not to a
    targeted athlete. ``ensure_athlete_id`` is retained as the same auth sanity
    check used by the existing strength tools; the returned id is not used as
    an exercise owner id (ownerId is always taken from TrainingPeaks responses).
    """

    athlete_id = await client.ensure_athlete_id()
    if not athlete_id:
        return None, None, _err("AUTH_INVALID", "Could not get athlete ID. Re-authenticate.")
    token = await client._ensure_access_token()
    if not token.success:
        return None, None, _err("AUTH_INVALID", token.message or "Failed to obtain access token.")
    access = client._token_cache.access_token
    if not access:
        return None, None, _err("AUTH_INVALID", "No access token available. Re-authenticate.")
    return athlete_id, access, None


def _unwrap_data(payload: Any) -> Any:
    if isinstance(payload, dict) and "data" in payload:
        return payload.get("data")
    return payload


def normalize_library_content(payload: Any) -> dict[str, Any]:
    """Normalize the live ``libraryContent`` response without losing search data."""

    data = _unwrap_data(payload)
    if not isinstance(data, dict):
        raise ValueError("TrainingPeaks libraryContent response is not an object")

    exercises_raw = data.get("exercises") or []
    if not isinstance(exercises_raw, list):
        raise ValueError("TrainingPeaks libraryContent.exercises is not a list")

    exercises: dict[str, dict[str, Any]] = {}
    for item in exercises_raw:
        if not isinstance(item, dict):
            continue
        exercise_id = str(item.get("exerciseId") or item.get("id") or "").strip()
        title = str(item.get("title") or "").strip()
        if not exercise_id or not title:
            continue
        attrs = (
            item.get("searchAttributes")
            if isinstance(item.get("searchAttributes"), dict)
            else {}
        )
        exercises[exercise_id] = {
            "id": exercise_id,
            "exerciseId": exercise_id,
            "title": title,
            "searchText": str(item.get("searchText") or "").strip() or None,
            "alternateTitles": [
                str(v) for v in (attrs.get("alternateTitles") or []) if str(v).strip()
            ],
            "primaryMuscleGroups": [
                str(v) for v in (attrs.get("primaryMuscleGroups") or []) if str(v).strip()
            ],
            "secondaryMuscleGroups": [
                str(v) for v in (attrs.get("secondaryMuscleGroups") or []) if str(v).strip()
            ],
            "canEdit": item.get("canEdit") is True,
            "ownerId": item.get("ownerId"),
            # Slim libraryContent rows do not contain these. Call
            # tp_get_exercise when full metadata is required.
            "videoUrl": None,
            "parameters": [],
        }

    def _titles(rows: Any, key: str) -> list[str]:
        out: list[str] = []
        if not isinstance(rows, list):
            return out
        for row in rows:
            if isinstance(row, str):
                value = row.strip()
            elif isinstance(row, dict):
                value = str(row.get(key) or row.get("title") or "").strip()
            else:
                value = ""
            if value:
                out.append(value)
        return out

    return {
        "exercises": exercises,
        "muscle_groups": _titles(data.get("muscleGroups"), "title"),
        "block_types": _titles(data.get("blocks"), "blockType"),
    }


async def fetch_live_library_content(access: str, h: httpx.AsyncClient) -> dict[str, Any]:
    r = await h.get(
        f"{STRENGTH_API_BASE}/rx/activity/v1/libraryContent",
        headers=_headers(access),
    )
    if r.status_code != 200:
        raise RuntimeError(f"libraryContent returned {r.status_code}: {r.text[:200]}")
    return normalize_library_content(r.json())


def normalize_parameter_catalog(payload: Any) -> dict[str, dict[str, Any]]:
    """Normalize ``GET /parameters/exercise`` to parameter-name -> definition."""

    data = _unwrap_data(payload)
    if isinstance(data, dict):
        # Be tolerant if the API ever wraps the list under a descriptive key.
        data = data.get("parameters") or data.get("exerciseParameters") or []
    if not isinstance(data, list):
        raise ValueError("TrainingPeaks exercise parameter response is not a list")

    out: dict[str, dict[str, Any]] = {}
    for row in data:
        if not isinstance(row, dict):
            continue
        name = str(row.get("parameter") or "").strip()
        if name:
            out[name] = deepcopy(row)
    return out


async def fetch_exercise_parameter_catalog(
    access: str,
    h: httpx.AsyncClient,
) -> dict[str, dict[str, Any]]:
    r = await h.get(
        f"{STRENGTH_API_BASE}/rx/activity/v1/parameters/exercise",
        headers=_headers(access),
    )
    if r.status_code != 200:
        raise RuntimeError(f"exercise parameters returned {r.status_code}: {r.text[:200]}")
    return normalize_parameter_catalog(r.json())


def _project_exercise(exercise: dict[str, Any]) -> dict[str, Any]:
    params = exercise.get("parameters") or []
    return {
        "exercise_id": str(exercise.get("id") or exercise.get("exerciseId") or ""),
        "title": exercise.get("title"),
        "video_url": exercise.get("videoUrl"),
        "instructions": exercise.get("instructions"),
        "parameters": [
            p.get("parameter") for p in params if isinstance(p, dict) and p.get("parameter")
        ],
        "primary_muscle_groups": exercise.get("primaryMuscleGroups") or [],
        "secondary_muscle_groups": exercise.get("secondaryMuscleGroups") or [],
        "can_edit": exercise.get("canEdit") is True,
        "owner_id": exercise.get("ownerId"),
        "custom": exercise.get("canEdit") is True and exercise.get("ownerId") is not None,
    }


def _validate_parameter_names(
    names: list[str],
    catalog: dict[str, dict[str, Any]],
) -> tuple[list[str], dict[str, Any] | None]:
    cleaned: list[str] = []
    for raw in names:
        name = str(raw).strip()
        if name and name not in cleaned:
            cleaned.append(name)
    if not cleaned:
        return [], _err("VALIDATION_ERROR", "A custom exercise must have at least one parameter.")
    unknown = [name for name in cleaned if name not in catalog]
    if unknown:
        return [], _err(
            "VALIDATION_ERROR",
            f"Unknown TrainingPeaks exercise parameter(s): {unknown}. Allowed: {sorted(catalog)}",
        )
    return cleaned, None


def _materialize_parameters(
    names: list[str],
    catalog: dict[str, dict[str, Any]],
    existing: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build PUT parameter definitions, preserving ids already owned by the doc.

    The current web client sends newly selected definitions without an ``id``;
    TrainingPeaks assigns permanent numeric parameter ids when the custom
    exercise is saved. Existing scaffold/custom parameter ids are preserved.
    """

    by_name = {
        str(row.get("parameter")): row
        for row in existing
        if isinstance(row, dict) and row.get("parameter")
    }
    out: list[dict[str, Any]] = []
    for name in names:
        item = deepcopy(catalog[name])
        prior = by_name.get(name)
        if prior and prior.get("id") is not None:
            item["id"] = prior["id"]
        else:
            item.pop("id", None)
        out.append(item)
    return out


def _validate_muscle_groups(
    groups: list[str] | None,
    allowed: list[str],
    field: str,
) -> dict[str, Any] | None:
    if groups is None or not allowed:
        return None
    bad = [str(group) for group in groups if str(group) not in allowed]
    if bad:
        return _err(
            "VALIDATION_ERROR",
            f"Unknown {field} muscle group(s): {bad}. Allowed: {allowed}",
        )
    return None


async def tp_get_exercise(exercise_id: str) -> dict[str, Any]:
    """Get one TrainingPeaks strength exercise in full by numeric library id."""

    eid = str(exercise_id).strip()
    if not eid or not eid.isdigit():
        return _err(
            "VALIDATION_ERROR",
            "exercise_id must be a numeric TrainingPeaks exercise id.",
        )

    async with TPClient() as client:
        _, access, err = await _access(client)
        if err:
            return err
        try:
            async with httpx.AsyncClient(timeout=STRENGTH_TIMEOUT) as h:
                r = await h.get(
                    f"{STRENGTH_API_BASE}/rx/activity/v1/exercises/{eid}",
                    headers=_headers(access),
                )
        except httpx.TimeoutException:
            return _err("NETWORK_ERROR", "Exercise detail timed out.")
        except httpx.RequestError:
            logger.exception("Network error reading strength exercise")
            return _err("NETWORK_ERROR", "A network error occurred.")

        if r.status_code != 200:
            return _map_status(r.status_code, r.text)
        data = _unwrap_data(r.json())
        if not isinstance(data, dict) or not data:
            return _err("NOT_FOUND", "Exercise not found.")
        return _project_exercise(data)


async def tp_create_custom_exercise(
    title: str,
    parameters: list[str] | None = None,
    video_url: str | None = None,
    instructions: str | None = None,
    primary_muscle_groups: list[str] | None = None,
    secondary_muscle_groups: list[str] | None = None,
) -> dict[str, Any]:
    """Create a reusable custom Strength Builder exercise.

    This is a real TrainingPeaks mutation. The caller supplies human-level
    fields only; owner ids, scaffold ids, parameter metadata and permanent ids
    come from TrainingPeaks itself.
    """

    clean_title = str(title).strip()
    if not clean_title:
        return _err("VALIDATION_ERROR", "title is required.")

    async with TPClient() as client:
        _, access, err = await _access(client)
        if err:
            return err
        try:
            async with httpx.AsyncClient(timeout=STRENGTH_TIMEOUT) as h:
                parameter_catalog = await fetch_exercise_parameter_catalog(access, h)
                library = await fetch_live_library_content(access, h)

                for groups, field in (
                    (primary_muscle_groups, "primary"),
                    (secondary_muscle_groups, "secondary"),
                ):
                    group_err = _validate_muscle_groups(groups, library["muscle_groups"], field)
                    if group_err:
                        return group_err

                # A POST creates a real server-side scaffold. Validate explicit
                # caller parameters before that mutation whenever possible.
                requested: list[str] | None = None
                if parameters is not None:
                    requested, parameter_err = _validate_parameter_names(
                        parameters,
                        parameter_catalog,
                    )
                    if parameter_err:
                        return parameter_err

                scaffold_response = await h.post(
                    f"{STRENGTH_API_BASE}/rx/activity/v1/exercises",
                    headers=_headers(access),
                )
                if scaffold_response.status_code != 200:
                    return _map_status(scaffold_response.status_code, scaffold_response.text)
                scaffold = _unwrap_data(scaffold_response.json())
                if not isinstance(scaffold, dict) or not scaffold.get("id"):
                    return _err(
                        "API_ERROR",
                        "TrainingPeaks returned an invalid custom-exercise scaffold.",
                    )

                if requested is None:
                    scaffold_parameters = [
                        str(p.get("parameter"))
                        for p in (scaffold.get("parameters") or [])
                        if isinstance(p, dict) and p.get("parameter")
                    ]
                    requested, parameter_err = _validate_parameter_names(
                        scaffold_parameters,
                        parameter_catalog,
                    )
                    if parameter_err:
                        return parameter_err

                payload = deepcopy(scaffold)
                payload["title"] = clean_title
                if video_url is not None:
                    payload["videoUrl"] = str(video_url).strip()
                if instructions is not None:
                    payload["instructions"] = str(instructions)
                if primary_muscle_groups is not None:
                    payload["primaryMuscleGroups"] = list(primary_muscle_groups)
                if secondary_muscle_groups is not None:
                    payload["secondaryMuscleGroups"] = list(secondary_muscle_groups)
                payload["parameters"] = _materialize_parameters(
                    requested,
                    parameter_catalog,
                    [p for p in (scaffold.get("parameters") or []) if isinstance(p, dict)],
                )
                payload["canEdit"] = True

                saved_response = await h.put(
                    f"{STRENGTH_API_BASE}/rx/activity/v1/exercises",
                    headers=_headers(access),
                    json=payload,
                )
        except httpx.TimeoutException:
            return _err("NETWORK_ERROR", "Custom exercise create timed out.")
        except httpx.RequestError:
            logger.exception("Network error creating custom strength exercise")
            return _err("NETWORK_ERROR", "A network error occurred.")
        except (RuntimeError, ValueError) as exc:
            return _err("API_ERROR", str(exc))

        if saved_response.status_code != 200:
            return _map_status(saved_response.status_code, saved_response.text)
        saved = _unwrap_data(saved_response.json())
        if not isinstance(saved, dict) or not saved.get("id"):
            return _err(
                "API_ERROR",
                "TrainingPeaks did not return a permanent custom exercise id.",
            )
        return _project_exercise(saved)


async def tp_update_custom_exercise(
    exercise_id: str,
    title: str | None = None,
    parameters: list[str] | None = None,
    video_url: str | None = None,
    instructions: str | None = None,
    primary_muscle_groups: list[str] | None = None,
    secondary_muscle_groups: list[str] | None = None,
) -> dict[str, Any]:
    """Update one caller-owned custom exercise by full-document PUT."""

    eid = str(exercise_id).strip()
    if not eid or not eid.isdigit():
        return _err(
            "VALIDATION_ERROR",
            "exercise_id must be a numeric TrainingPeaks exercise id.",
        )
    if all(
        value is None
        for value in (
            title,
            parameters,
            video_url,
            instructions,
            primary_muscle_groups,
            secondary_muscle_groups,
        )
    ):
        return _err("VALIDATION_ERROR", "Nothing to update.")
    if title is not None and not str(title).strip():
        return _err("VALIDATION_ERROR", "title cannot be empty.")

    async with TPClient() as client:
        _, access, err = await _access(client)
        if err:
            return err
        try:
            async with httpx.AsyncClient(timeout=STRENGTH_TIMEOUT) as h:
                current_response = await h.get(
                    f"{STRENGTH_API_BASE}/rx/activity/v1/exercises/{eid}",
                    headers=_headers(access),
                )
                if current_response.status_code != 200:
                    return _map_status(current_response.status_code, current_response.text)
                current = _unwrap_data(current_response.json())
                if not isinstance(current, dict) or not current:
                    return _err("NOT_FOUND", "Exercise not found.")
                if current.get("canEdit") is not True or current.get("ownerId") is None:
                    return _err(
                        "READ_ONLY",
                        "Only caller-owned custom exercises can be updated.",
                    )

                library = await fetch_live_library_content(access, h)
                library_row = library["exercises"].get(eid)
                if library_row is not None:
                    if library_row.get("canEdit") is not True:
                        return _err(
                            "READ_ONLY",
                            "This TrainingPeaks exercise is not editable.",
                        )
                    live_owner = library_row.get("ownerId")
                    if live_owner is not None and str(live_owner) != str(current.get("ownerId")):
                        return _err(
                            "READ_ONLY",
                            "Exercise ownership does not match the current library entry.",
                        )

                for groups, field in (
                    (primary_muscle_groups, "primary"),
                    (secondary_muscle_groups, "secondary"),
                ):
                    group_err = _validate_muscle_groups(groups, library["muscle_groups"], field)
                    if group_err:
                        return group_err

                payload = deepcopy(current)
                if title is not None:
                    payload["title"] = str(title).strip()
                if video_url is not None:
                    payload["videoUrl"] = str(video_url).strip()
                if instructions is not None:
                    payload["instructions"] = str(instructions)
                if primary_muscle_groups is not None:
                    payload["primaryMuscleGroups"] = list(primary_muscle_groups)
                if secondary_muscle_groups is not None:
                    payload["secondaryMuscleGroups"] = list(secondary_muscle_groups)

                if parameters is not None:
                    parameter_catalog = await fetch_exercise_parameter_catalog(access, h)
                    requested, parameter_err = _validate_parameter_names(
                        parameters,
                        parameter_catalog,
                    )
                    if parameter_err:
                        return parameter_err
                    payload["parameters"] = _materialize_parameters(
                        requested,
                        parameter_catalog,
                        [
                            p
                            for p in (current.get("parameters") or [])
                            if isinstance(p, dict)
                        ],
                    )

                saved_response = await h.put(
                    f"{STRENGTH_API_BASE}/rx/activity/v1/exercises",
                    headers=_headers(access),
                    json=payload,
                )
        except httpx.TimeoutException:
            return _err("NETWORK_ERROR", "Custom exercise update timed out.")
        except httpx.RequestError:
            logger.exception("Network error updating custom strength exercise")
            return _err("NETWORK_ERROR", "A network error occurred.")
        except (RuntimeError, ValueError) as exc:
            return _err("API_ERROR", str(exc))

        if saved_response.status_code != 200:
            return _map_status(saved_response.status_code, saved_response.text)
        saved = _unwrap_data(saved_response.json())
        if not isinstance(saved, dict) or not saved:
            return _err("API_ERROR", "TrainingPeaks returned an invalid updated exercise.")
        return _project_exercise(saved)
