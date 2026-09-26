"""Structured strength / gym workouts via the Peaksware strength API.

Endurance workouts (`tp_create_workout`) use the main TrainingPeaks fitness API
with power/HR/pace interval structures. Strength workouts are a completely
different model — `workoutType: "StructuredStrength"` posted to the Peaksware
strength API (`api.peakswaresb.com`, same Bearer token we already use for
`tp_analyze_workout`). Exercise discovery prefers the live combined Strength
Builder library (built-in + caller-owned custom exercises); the baked
`tp_mcp/data/exercises.json` catalogue remains a built-in fallback.

Verified against the live API:
  • create  → POST   /rx/activity/v1/workouts/save        (returns numeric id)
  • update  → POST   /rx/activity/v1/workouts/save        (SAME endpoint — it is an
              UPSERT keyed on the numeric `id`; posting a document that carries an
              existing id edits in place and returns that same id, with no duplicate
              appearing on the calendar)
  • list    → GET    /rx/activity/v1/workouts/calendar/{calendarId}/{start}/{end}
              (bare JSON array of workout summaries — the ONLY discovery route;
              strength workouts never appear in the /fitness/v6 endpoints)
  • detail  → GET    /rx/activity/v1/workouts/{id}         (full blocks/sets, {data} wrapper)
  • summary → GET    /rx/activity/v1/workouts/{id}/summary
  • delete  → DELETE /rx/activity/v1/workouts/{id}
  • a prescription must declare its own `parameters` (the prescribed columns),
    distinct from the exercise's library parameters and each set's values;
  • parameter metadata may be minimal (`{parameter, inputFormat}`);
  • Superset/Circuit blocks require an equal number of sets across exercises.

Update semantics (probed 2026-08-02, live API):
  • PARTIAL PAYLOADS ARE REJECTED. Posting `{id, blocks}` alone returns 400 with
    `calendarId`, `workoutType` and `prescribedDate` all "required". An update
    must therefore GET the full document, mutate it, and post the whole thing
    back — which is also what makes updates non-destructive.
  • A full-document round-trip preserves EVERYTHING the server owns. Verified on
    a Garmin-synced workout: `completedTss`, `completedTssSource`,
    `completedIntensityFactor`, `completionSource`, `startDateTime`,
    `completedDateTime`, `executedDurationInSeconds` and the attached FIT `files`
    all survived untouched; `lastUpdatedAt` was the only field the server changed.
    This is why editing a device-synced workout must never be done as
    delete-then-recreate: the exercise detail is reconstructible, the HR-derived
    training load is not.
  • Completion is flagged with `isComplete` (on blocks, prescriptions AND sets) —
    NOT `completed`, which the server accepts with a 200 and silently ignores,
    leaving `complianceState: "NoCompletion"`. Set `isComplete` at all three
    levels plus each parameter's `executedValue`, and the server recomputes
    compliance correctly (`Compliant` / 100%).
  • Sets carry an undocumented `setOrigin` field; it round-trips harmlessly.

Execution evidence (read live, 2026-09-25):
  • Since about July 2026 device-recorded (Garmin) strength sessions land here as
    `complianceState: "Unplanned"` with no blocks, `completionSource: "DeviceFile"`,
    attached `files`, and heart-rate TSS. Earlier ones came through /fitness/v6
    as Strength workouts, which are separate objects with their own ids. One real
    session can exist in both APIs; their start times overlap.
  • `completionSource` values seen: "DeviceFile", "MobileExecution",
    "CompletedAsPlanned" (executed values copied from the plan), and null (web
    completion). It is present on the detail document only, not in the list.
  • `executedDurationInSeconds` is a real duration only for "DeviceFile".

This tool is intentionally unit-agnostic: it passes through whatever weight
parameter the caller supplies (`WeightKg`, `WeightLb`, `WeightPercentage`, …).
Choosing a default unit (e.g. kg) is the caller's concern, not the connector's.
"""

import json
import logging
import uuid
from functools import lru_cache
from typing import Any

import httpx

from tp_mcp.auth import get_credential
from tp_mcp.client import TPClient
from tp_mcp.tools.strength_exercises import fetch_live_library_content

logger = logging.getLogger("tp-mcp")

STRENGTH_API_BASE = "https://api.peakswaresb.com"
STRENGTH_TIMEOUT = 30.0

# The full parameter catalogue (per exercise sets). Integer-format ones are
# whole counts; everything else is decimal. Anything outside this set is
# rejected so a typo can't silently produce an empty column. RIR is part of the
# current TrainingPeaks Strength Builder parameter catalogue.
_INTEGER_PARAMS = {"Reps", "RepsPerSide", "Cals"}
_KNOWN_PARAMS = _INTEGER_PARAMS | {
    "WeightKg", "WeightLb", "WeightPerSideKg", "WeightPerSideLb", "WeightPercentage",
    "Duration", "DistanceMeters", "DistanceKm", "DistanceFt", "DistanceYd",
    "DistanceMiles", "HeightCm", "HeightM", "HeightIn", "HeightFt",
    "RPE", "RIR", "Watts", "VelocityMetersPerSec",
}
_BLOCK_TYPES = {"WarmUp", "SingleExercise", "Superset", "Circuit", "CoolDown"}
# Blocks where every exercise must share the same number of sets (verified
# server constraint — otherwise save returns 400).
_EQUAL_SET_BLOCKS = {"Superset", "Circuit"}


def _input_format(parameter: str) -> str:
    return "Integer" if parameter in _INTEGER_PARAMS else "Decimal"


def _err(code: str, message: str) -> dict[str, Any]:
    return {"isError": True, "error_code": code, "message": message}


# ── Exercise catalogue (live + baked fallback) ──────────────────────────────
#
# PROVENANCE of `tp_mcp/data/exercises.json` (944 exercises):
#   This remains a built-in fallback for unauthenticated/offline search and for
#   existing workout authoring. The current Strength Builder also exposes a live
#   combined library via `/rx/activity/v1/libraryContent`; that live source is
#   required to discover account-specific custom exercises.


@lru_cache(maxsize=1)
def _catalogue() -> dict[str, dict[str, Any]]:
    """The baked built-in exercise fallback, keyed by string id."""
    try:
        from importlib.resources import files

        text = files("tp_mcp").joinpath("data/exercises.json").read_text(encoding="utf-8")
    except Exception:  # dev / editable install fallback
        from pathlib import Path

        path = Path(__file__).resolve().parent.parent / "data" / "exercises.json"
        text = path.read_text(encoding="utf-8")
    return json.loads(text)


def _merge_live_with_baked(live: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Use live membership/search metadata, enriching built-ins from the snapshot."""
    baked = _catalogue()
    merged: dict[str, dict[str, Any]] = {}
    for eid, row in live.items():
        base = dict(baked.get(eid) or {})
        base.update(row)
        if eid in baked:
            if not row.get("videoUrl"):
                base["videoUrl"] = baked[eid].get("videoUrl")
            if not row.get("parameters"):
                base["parameters"] = baked[eid].get("parameters") or []
        merged[eid] = base
    return merged


def _search_catalogue(
    catalogue: dict[str, dict[str, Any]],
    query: str,
    muscle_group: str,
    limit: int,
    *,
    source: str,
) -> list[dict[str, Any]]:
    q = query.lower()
    mg = muscle_group.lower()
    out: list[dict[str, Any]] = []
    for ex in catalogue.values():
        title = str(ex.get("title") or "")
        alternates = [str(v) for v in (ex.get("alternateTitles") or [])]
        search_text = str(ex.get("searchText") or "")
        haystack = " ".join([title, search_text, *alternates]).lower()
        if q and q not in haystack:
            continue
        primary = [str(v) for v in (ex.get("primaryMuscleGroups") or [])]
        secondary = [str(v) for v in (ex.get("secondaryMuscleGroups") or [])]
        groups = " ".join(primary + secondary).lower()
        if mg and mg not in groups:
            continue
        params = ex.get("parameters") or []
        out.append(
            {
                "id": str(ex.get("id") or ex.get("exerciseId") or ""),
                "title": title,
                "video_url": ex.get("videoUrl"),
                "muscle_groups": primary,
                "parameters": [
                    p.get("parameter")
                    for p in params
                    if isinstance(p, dict) and p.get("parameter")
                ],
                "custom": ex.get("canEdit") is True and ex.get("ownerId") is not None,
                "can_edit": ex.get("canEdit") is True,
                "owner_id": ex.get("ownerId"),
                "source": source,
            }
        )
    if q:
        out.sort(
            key=lambda e: (
                e["title"].lower() != q,
                not e["title"].lower().startswith(q),
                e["title"].lower(),
            )
        )
    else:
        out.sort(key=lambda e: e["title"].lower())
    return out[:limit]


async def tp_search_exercises(
    query: str,
    limit: int = 20,
    muscle_group: str | None = None,
) -> dict[str, Any]:
    """Search the current TrainingPeaks strength exercise library.

    Authenticated calls prefer the live combined library so caller-owned custom
    exercises appear immediately. If live discovery is unavailable, search
    falls back to the baked built-in snapshot and reports that custom exercises
    are unavailable in that result.

    Args:
        query: Substring to match against title/search aliases (case-insensitive).
            Empty query with a muscle_group returns exercises for that muscle.
        limit: Max results (1-100).
        muscle_group: Optional filter on primary/secondary muscle group
            (case-insensitive substring, e.g. "glute", "ham").

    Returns:
        Dict with source metadata plus `exercises`. Full custom exercise details
        can be loaded with `tp_get_exercise`.
    """
    q = (query or "").strip()
    mg = (muscle_group or "").strip()
    limit = max(1, min(int(limit or 20), 100))
    if not q and not mg:
        return _err("VALIDATION_ERROR", "Provide a search query or a muscle_group.")

    cred = get_credential()
    if cred.success and cred.cookie:
        try:
            async with TPClient() as client:
                _, access, auth_err = await _access(client)
                if not auth_err and access:
                    async with httpx.AsyncClient(timeout=STRENGTH_TIMEOUT) as h:
                        live = await fetch_live_library_content(access, h)
                    catalogue = _merge_live_with_baked(live["exercises"])
                    results = _search_catalogue(catalogue, q, mg, limit, source="live")
                    return {
                        "count": len(results),
                        "source": "live",
                        "custom_exercises_available": True,
                        "exercises": results,
                    }
        except (httpx.HTTPError, RuntimeError, ValueError):
            logger.warning(
                "Live strength exercise library unavailable; using baked fallback",
                exc_info=True,
            )

    results = _search_catalogue(_catalogue(), q, mg, limit, source="baked_fallback")
    return {
        "count": len(results),
        "source": "baked_fallback",
        "custom_exercises_available": False,
        "exercises": results,
    }


# ── Payload construction ────────────────────────────────────────────────────


def _validate_blocks(
    blocks: list[dict[str, Any]],
    catalogue: dict[str, dict[str, Any]] | None = None,
    *,
    allow_unknown_exercises: bool = False,
) -> str | None:
    """Return an error string if the blocks are invalid, else None."""
    if not blocks:
        return "At least one block with one exercise is required."
    catalogue = catalogue or _catalogue()
    for bi, block in enumerate(blocks):
        btype = block.get("type", "SingleExercise")
        if btype not in _BLOCK_TYPES:
            return f"block[{bi}].type {btype!r} is invalid. Allowed: {sorted(_BLOCK_TYPES)}."
        exercises = block.get("exercises") or []
        if not exercises:
            return f"block[{bi}] ({btype}) has no exercises."
        set_counts = []
        for ei, ex in enumerate(exercises):
            eid = str(ex.get("id", "")).strip()
            if not eid.isdigit() or int(eid) <= 0:
                return f"block[{bi}].exercise[{ei}] id {eid!r} not in the exercise library."
            if eid not in catalogue and not allow_unknown_exercises:
                return f"block[{bi}].exercise[{ei}] id {eid!r} not in the exercise library."
            sets = ex.get("sets") or []
            if not sets:
                label = (catalogue.get(eid) or {}).get("title") or eid
                return f"block[{bi}].exercise[{ei}] ({label}) has no sets."
            set_counts.append(len(sets))
            for si, s in enumerate(sets):
                if not isinstance(s, dict) or not s:
                    return (
                        f"block[{bi}].exercise[{ei}].set[{si}] must be a non-empty "
                        "map of parameter→value."
                    )
                bad = [p for p in s if p not in _KNOWN_PARAMS]
                if bad:
                    return (
                        f"block[{bi}].exercise[{ei}].set[{si}] has unknown parameter(s) {bad}. "
                        f"Allowed: {sorted(_KNOWN_PARAMS)}."
                    )
        if btype in _EQUAL_SET_BLOCKS and len(set(set_counts)) > 1:
            return (
                f"block[{bi}] ({btype}) requires the same number of sets for every "
                f"exercise (got {set_counts})."
            )
    return None


def _missing_exercise_ids(
    blocks: list[dict[str, Any]],
    catalogue: dict[str, dict[str, Any]],
) -> set[str]:
    return {
        str(ex.get("id", "")).strip()
        for block in blocks
        for ex in (block.get("exercises") or [])
        if str(ex.get("id", "")).strip() not in catalogue
    }


async def _catalogue_for_blocks(
    access: str,
    h: httpx.AsyncClient,
    blocks: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Resolve custom/new positive exercise ids against the live account library."""
    baked = _catalogue()
    if not _missing_exercise_ids(blocks, baked):
        return baked
    try:
        live = await fetch_live_library_content(access, h)
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        raise RuntimeError(
            "TrainingPeaks live exercise library is unavailable; "
            "custom/new exercise ids cannot be validated"
        ) from exc
    return _merge_live_with_baked(live["exercises"])


def _u() -> str:
    return str(uuid.uuid4())


def _build_prescription(ex: dict[str, Any], catalogue: dict[str, Any]) -> dict[str, Any]:
    eid = str(ex["id"])
    meta = catalogue[eid]
    sets_in = ex.get("sets") or []
    # Prescribed columns = union of parameters used across this exercise's sets,
    # in first-seen order.
    columns: list[str] = []
    for s in sets_in:
        for p in s:
            if p not in columns:
                columns.append(p)
    sets_out = []
    for s in sets_in:
        values = [
            {
                "id": _u(),
                "parameter": p,
                "prescribedValue": str(v),
                "executedValue": None,
                "inputFormat": _input_format(p),
            }
            for p, v in s.items()
        ]
        sets_out.append({"id": _u(), "parameterValues": values})
    return {
        "id": _u(),
        # Send only id + title; the server enriches the exercise's parameter
        # metadata from its own library. (Our baked catalogue flattens `unit`
        # to a string for search, which the save API rejects.)
        "exercise": {"id": eid, "title": meta["title"], "parameters": []},
        "parameters": [{"parameter": p, "inputFormat": _input_format(p)} for p in columns],
        "sets": sets_out,
        "coachNotes": ex.get("notes"),
        "setSummaryTemplate": None,
    }


def _build_payload(
    athlete_id: int,
    date: str,
    title: str,
    blocks: list[dict[str, Any]],
    instructions: str | None,
    catalogue: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    catalogue = catalogue or _catalogue()
    blocks_out = []
    total_sets = 0
    for block in blocks:
        prescs = [_build_prescription(ex, catalogue) for ex in block["exercises"]]
        total_sets += sum(len(p["sets"]) for p in prescs)
        blocks_out.append(
            {
                "id": _u(),
                "blockType": block.get("type", "SingleExercise"),
                "title": block.get("title"),
                "coachNotes": block.get("notes"),
                "prescriptions": prescs,
            }
        )
    return {
        "id": _u(),
        "workoutType": "StructuredStrength",
        "calendarId": athlete_id,
        "title": title,
        "prescribedDate": date,
        "instructions": instructions,
        "blocks": blocks_out,
        "snapshot": {
            "totalBlocks": len(blocks_out),
            "completedBlocks": 0,
            "totalSets": total_sets,
            "completedSets": 0,
        },
    }


# ── Auth boilerplate (mirrors analyze.py — strength API is a different host) ──


async def _access(client: TPClient) -> tuple[int | None, str | None, dict[str, Any] | None]:
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


def _headers(access: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {access}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://app.trainingpeaks.com",
        "Referer": "https://app.trainingpeaks.com/",
    }


def _map_status(status: int, body: str) -> dict[str, Any]:
    if status == 401:
        return _err("AUTH_EXPIRED", "Session expired. Run 'tp-mcp auth' to re-authenticate.")
    if status == 403:
        return _err("AUTH_INVALID", "Access denied. Check permissions or re-authenticate.")
    if status == 404:
        return _err("NOT_FOUND", "Strength workout not found.")
    if status == 429:
        return _err("RATE_LIMITED", "Rate limited. Please wait before retrying.")
    return _err("API_ERROR", f"Strength API error: {status} {body[:200]}")


# ── Tools ───────────────────────────────────────────────────────────────────


async def tp_create_strength_workout(
    date: str,
    title: str,
    blocks: list[dict[str, Any]],
    instructions: str | None = None,
) -> dict[str, Any]:
    """Create a structured strength (gym) workout on the athlete's calendar.

    Args:
        date: Planned date, YYYY-MM-DD.
        title: Workout title (e.g. "Upper Body").
        blocks: Ordered list of blocks. Each block is
            {"type": "WarmUp"|"SingleExercise"|"Superset"|"Circuit"|"CoolDown",
             "title": optional, "notes": optional,
             "exercises": [{"id": "<library id from tp_search_exercises>",
                            "notes": optional,
                            "sets": [{"Reps": "10", "WeightKg": "60"}, ...]}]}.
            Set values are a map of parameter name → value (strings or numbers).
            Weight unit is the caller's choice (WeightKg / WeightLb / …).
            For Superset / Circuit blocks every exercise must have the same
            number of sets.
        instructions: Optional free-text instructions for the whole session.

    Returns:
        Dict with the created `workout_id`, date, title, and block/set counts.
    """
    if not str(date).strip():
        return _err("VALIDATION_ERROR", "date is required (YYYY-MM-DD).")
    if not str(title).strip():
        return _err("VALIDATION_ERROR", "title is required.")
    if not isinstance(blocks, list):
        return _err("VALIDATION_ERROR", "blocks must be a list.")
    invalid = _validate_blocks(blocks, allow_unknown_exercises=True)
    if invalid:
        return _err("VALIDATION_ERROR", invalid)

    async with TPClient() as client:
        athlete_id, access, err = await _access(client)
        if err:
            return err
        try:
            async with httpx.AsyncClient(timeout=STRENGTH_TIMEOUT) as h:
                try:
                    catalogue = await _catalogue_for_blocks(access, h, blocks)
                except RuntimeError as exc:
                    return _err("API_ERROR", str(exc))
                invalid = _validate_blocks(blocks, catalogue)
                if invalid:
                    return _err("VALIDATION_ERROR", invalid)
                payload = _build_payload(
                    athlete_id,
                    date.strip(),
                    title.strip(),
                    blocks,
                    instructions,
                    catalogue,
                )
                r = await h.post(
                    f"{STRENGTH_API_BASE}/rx/activity/v1/workouts/save",
                    headers=_headers(access),
                    json=payload,
                )
        except httpx.TimeoutException:
            return _err("NETWORK_ERROR", "Strength create timed out.")
        except httpx.RequestError:
            logger.exception("Network error creating strength workout")
            return _err("NETWORK_ERROR", "A network error occurred.")

        if r.status_code != 200:
            body = r.text
            # Surface the server's field-level validation verbatim — it is precise.
            try:
                errs = r.json().get("errors")
                if errs:
                    return _err("API_ERROR", f"Strength API rejected the workout: {errs}")
            except Exception:
                pass
            return _map_status(r.status_code, body)

        data = r.json().get("data", {})
        snap = data.get("snapshot") or payload["snapshot"]
        return {
            "workout_id": str(data.get("id")),
            "date": date.strip(),
            "title": title.strip(),
            "total_blocks": snap.get("totalBlocks"),
            "total_sets": snap.get("totalSets"),
        }


def _recount(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    """Recompute the snapshot counters from the blocks themselves.

    Counts actual `isComplete` flags rather than assuming, so the snapshot stays
    honest when a partially-completed workout is edited or appended to.
    """
    total_sets = completed_sets = 0
    total_pres = completed_pres = 0
    completed_blocks = 0
    for b in blocks:
        block_pres = b.get("prescriptions") or []
        total_pres += len(block_pres)
        block_sets = 0
        block_done = 0
        for p in block_pres:
            sets = p.get("sets") or []
            done = sum(1 for s in sets if s.get("isComplete"))
            total_sets += len(sets)
            completed_sets += done
            block_sets += len(sets)
            block_done += done
            if sets and done == len(sets):
                completed_pres += 1
        if block_sets and block_done == block_sets:
            completed_blocks += 1
    return {
        "totalBlocks": len(blocks),
        "completedBlocks": completed_blocks,
        "totalSets": total_sets,
        "completedSets": completed_sets,
        "totalPrescriptions": total_pres,
        "completedPrescriptions": completed_pres,
    }


def _mark_complete(blocks: list[dict[str, Any]]) -> None:
    """Mark every block/prescription/set complete, mirroring prescribed→executed.

    In-place. Used when logging a session that has already been performed, so
    TrainingPeaks shows executed sets/reps/volume rather than an unstarted plan.
    An existing `executedValue` is never overwritten — only blanks are filled.
    """
    for b in blocks:
        b["isComplete"] = True
        for p in b.get("prescriptions") or []:
            for s in p.get("sets") or []:
                s["isComplete"] = True
                for pv in s.get("parameterValues") or []:
                    if pv.get("executedValue") is None:
                        pv["executedValue"] = pv.get("prescribedValue")


async def tp_update_strength_workout(
    workout_id: str,
    blocks: list[dict[str, Any]] | None = None,
    title: str | None = None,
    instructions: str | None = None,
    mode: str = "replace",
    mark_complete: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Update an existing strength workout in place (blocks, title, instructions).

    Fetches the full workout, applies the requested changes, and posts the whole
    document back to the upsert endpoint. Everything not explicitly changed is
    preserved — including Garmin-derived training load and the attached FIT file
    — which makes this the correct way to fill in a device-synced workout that
    arrived with an empty structure. Never delete-and-recreate for that: the
    exercise detail can be rebuilt, the HR-derived TSS cannot.

    Args:
        workout_id: The strength workout ID (from tp_get_strength_workouts).
        blocks: Optional replacement/additional blocks, same shape as
            tp_create_strength_workout. Omit to leave the structure alone (e.g.
            when only retitling, or only marking an existing plan complete).
        title: Optional new title.
        instructions: Optional new session instructions.
        mode: "replace" (default) swaps the block list for `blocks`; "append"
            adds them after the existing blocks.
        mark_complete: Mark every set complete and copy prescribed values into
            executed ones, so the workout reports real volume and compliance.
            Use when logging a session already performed. Existing executed
            values are left untouched.
        dry_run: Validate and build the update, report what would change, but
            send nothing.

    Returns:
        Dict with the workout_id, before/after block and set counts, the fields
        changed, and (for dry runs) `dry_run: True` with nothing written.
    """
    wid = str(workout_id).strip()
    if not wid:
        return _err("VALIDATION_ERROR", "workout_id is required.")
    if mode not in ("replace", "append"):
        return _err("VALIDATION_ERROR", f"mode must be 'replace' or 'append', got {mode!r}.")
    if blocks is not None:
        if not isinstance(blocks, list):
            return _err("VALIDATION_ERROR", "blocks must be a list.")
        invalid = _validate_blocks(blocks, allow_unknown_exercises=True)
        if invalid:
            return _err("VALIDATION_ERROR", invalid)
    if blocks is None and title is None and instructions is None and not mark_complete:
        return _err(
            "VALIDATION_ERROR",
            "Nothing to update: provide blocks, title, instructions or mark_complete.",
        )

    async with TPClient() as client:
        _, access, err = await _access(client)
        if err:
            return err
        try:
            async with httpx.AsyncClient(timeout=STRENGTH_TIMEOUT) as h:
                r = await h.get(
                    f"{STRENGTH_API_BASE}/rx/activity/v1/workouts/{wid}",
                    headers=_headers(access),
                )
                if r.status_code != 200:
                    return _map_status(r.status_code, r.text)
                doc = r.json().get("data") or {}
                if not doc:
                    return _err("NOT_FOUND", "Strength workout not found.")

                before = dict(doc.get("snapshot") or {})
                changed: list[str] = []

                if blocks is not None:
                    try:
                        catalogue = await _catalogue_for_blocks(access, h, blocks)
                    except RuntimeError as exc:
                        return _err("API_ERROR", str(exc))
                    invalid = _validate_blocks(blocks, catalogue)
                    if invalid:
                        return _err("VALIDATION_ERROR", invalid)
                    new_blocks = [
                        {
                            "id": _u(),
                            "blockType": b.get("type", "SingleExercise"),
                            "title": b.get("title"),
                            "coachNotes": b.get("notes"),
                            "prescriptions": [
                                _build_prescription(ex, catalogue) for ex in b["exercises"]
                            ],
                        }
                        for b in blocks
                    ]
                    if mode == "append":
                        doc["blocks"] = (doc.get("blocks") or []) + new_blocks
                    else:
                        doc["blocks"] = new_blocks
                    changed.append(f"blocks ({mode})")

                if title is not None:
                    doc["title"] = str(title).strip()
                    changed.append("title")
                if instructions is not None:
                    doc["instructions"] = instructions
                    changed.append("instructions")
                if mark_complete:
                    _mark_complete(doc.get("blocks") or [])
                    changed.append("mark_complete")

                doc["snapshot"] = _recount(doc.get("blocks") or [])

                if dry_run:
                    return {
                        "workout_id": wid,
                        "dry_run": True,
                        "changed": changed,
                        "sets_before": before.get("totalSets"),
                        "sets_after": doc["snapshot"]["totalSets"],
                        "blocks_before": before.get("totalBlocks"),
                        "blocks_after": doc["snapshot"]["totalBlocks"],
                    }

                r = await h.post(
                    f"{STRENGTH_API_BASE}/rx/activity/v1/workouts/save",
                    headers=_headers(access),
                    json=doc,
                )
        except httpx.TimeoutException:
            return _err("NETWORK_ERROR", "Strength update timed out.")
        except httpx.RequestError:
            logger.exception("Network error updating strength workout")
            return _err("NETWORK_ERROR", "A network error occurred.")

        if r.status_code != 200:
            try:
                errs = r.json().get("errors")
                if errs:
                    return _err("API_ERROR", f"Strength API rejected the update: {errs}")
            except Exception:
                pass
            return _map_status(r.status_code, r.text)

        data = r.json().get("data") or {}
        snap = data.get("snapshot") or doc["snapshot"]
        returned = str(data.get("id")) if data.get("id") is not None else wid
        result = {
            "workout_id": returned,
            "title": doc.get("title"),
            "changed": changed,
            "blocks_before": before.get("totalBlocks"),
            "blocks_after": snap.get("totalBlocks"),
            "sets_before": before.get("totalSets"),
            "sets_after": snap.get("totalSets"),
            "completed_sets": snap.get("completedSets"),
        }
        # The upsert must edit in place; a different id means a duplicate was
        # created and the caller needs to know immediately.
        if returned != wid:
            result["warning"] = (
                f"Server returned id {returned}, expected {wid} — a duplicate may "
                f"have been created. Check the calendar for that date."
            )
        return result


async def tp_get_strength_summary(workout_id: str) -> dict[str, Any]:
    """Get a strength workout's compliance summary (blocks / sets completed).

    Args:
        workout_id: The strength workout ID (from tp_create_strength_workout).

    Returns:
        Dict with compliance state/percent and block/prescription/set totals.
    """
    wid = str(workout_id).strip()
    if not wid:
        return _err("VALIDATION_ERROR", "workout_id is required.")
    async with TPClient() as client:
        _, access, err = await _access(client)
        if err:
            return err
        try:
            async with httpx.AsyncClient(timeout=STRENGTH_TIMEOUT) as h:
                r = await h.get(
                    f"{STRENGTH_API_BASE}/rx/activity/v1/workouts/{wid}/summary",
                    headers=_headers(access),
                )
        except httpx.TimeoutException:
            return _err("NETWORK_ERROR", "Strength summary timed out.")
        except httpx.RequestError:
            logger.exception("Network error reading strength summary")
            return _err("NETWORK_ERROR", "A network error occurred.")

        if r.status_code != 200:
            return _map_status(r.status_code, r.text)
        d = r.json().get("data", {})
        return {
            "workout_id": wid,
            "compliance_state": d.get("complianceState"),
            "compliance_percent": d.get("compliancePercent"),
            "total_blocks": d.get("totalBlocks"),
            "completed_blocks": d.get("completedBlocks"),
            "total_prescriptions": d.get("totalPrescriptions"),
            "completed_prescriptions": d.get("completedPrescriptions"),
            "total_sets": d.get("totalSets"),
            "completed_sets": d.get("completedSets"),
            "rpe": d.get("rpe"),
            "feel": d.get("feel"),
        }


def _min(seconds: Any) -> float | None:
    """Seconds → minutes (1 dp), or None."""
    if seconds is None:
        return None
    try:
        return round(float(seconds) / 60.0, 1)
    except (TypeError, ValueError):
        return None


def _fmt_set(s: dict[str, Any]) -> dict[str, Any]:
    """Flatten one set's parameterValues into prescribed/executed maps."""
    prescribed: dict[str, Any] = {}
    executed: dict[str, Any] = {}
    for pv in s.get("parameterValues") or []:
        p = pv.get("parameter")
        if not p:
            continue
        if pv.get("prescribedValue") is not None:
            prescribed[p] = pv["prescribedValue"]
        if pv.get("executedValue") is not None:
            executed[p] = pv["executedValue"]
    return {
        "set_id": _str_id(s.get("id")),
        "origin": s.get("setOrigin"),
        "prescribed": prescribed,
        "executed": executed,
        "complete": bool(s.get("isComplete")),
    }


def _str_id(value: Any) -> str | None:
    return None if value is None else str(value)


def _fmt_prescription(p: dict[str, Any]) -> dict[str, Any]:
    """One prescription = one exercise + its sets."""
    ex = p.get("exercise") or {}
    return {
        "prescription_id": _str_id(p.get("id")),
        "exercise_id": _str_id(ex.get("id")),
        "exercise": ex.get("title"),
        "primary_muscle_groups": list(ex.get("primaryMuscleGroups") or []),
        "video_url": ex.get("videoUrl"),
        "notes": p.get("coachNotes"),
        "compliance_state": p.get("complianceState"),
        "compliance_percent": p.get("compliancePercent"),
        "sets": [_fmt_set(s) for s in (p.get("sets") or [])],
    }


def _fmt_device_files(files: Any) -> list[dict[str, Any]]:
    """Attached activity files, without storage keys."""
    return [
        {
            "file_name": f.get("fileName"),
            "is_garmin": f.get("isGarmin"),
            "device_make": f.get("deviceMake"),
            "device_model": f.get("deviceModel"),
            "uploaded_at": f.get("dateUploaded"),
        }
        for f in (files or [])
        if isinstance(f, dict)
    ]


def _execution_fields(d: dict[str, Any]) -> dict[str, Any]:
    """Execution evidence shared by list and detail payloads.

    Times are athlete-local without an offset. `executed_duration_sec` is only a
    real activity duration when `completion_source` is "DeviceFile"; for manual
    or mobile completion it is the gap between start and complete taps.
    """
    return {
        "start_datetime": d.get("startDateTime"),
        "completed_datetime": d.get("completedDateTime"),
        "executed_duration_sec": d.get("executedDurationInSeconds"),
        "completed_tss": d.get("completedTss"),
        "completed_tss_source": d.get("completedTssSource"),
        "completed_intensity_factor": d.get("completedIntensityFactor"),
        "order_on_day": d.get("orderOnDay"),
        "workout_subtype_id": d.get("workoutSubTypeId"),
        "last_updated_at": d.get("lastUpdatedAt"),
    }


def _fmt_workout_detail(d: dict[str, Any]) -> dict[str, Any]:
    """Project a full strength workout detail payload to the fields tools expose."""
    blocks_out = []
    for b in d.get("blocks") or []:
        blocks_out.append(
            {
                "block_id": _str_id(b.get("id")),
                "type": b.get("blockType"),
                "complete": bool(b.get("isComplete")),
                "title": b.get("title"),
                "notes": b.get("coachNotes"),
                "compliance_percent": b.get("compliancePercent"),
                "exercises": [_fmt_prescription(p) for p in (b.get("prescriptions") or [])],
            }
        )
    snap = d.get("snapshot") or {}
    return {
        "workout_id": str(d.get("id")),
        "date": d.get("prescribedDate"),
        "title": d.get("title"),
        "workout_type": d.get("workoutType"),
        "instructions": d.get("instructions"),
        "prescribed_duration_min": _min(d.get("prescribedDurationInSeconds")),
        "executed_duration_min": _min(d.get("executedDurationInSeconds")),
        "compliance_state": d.get("complianceState"),
        "compliance_percent": d.get("compliancePercent"),
        "rpe": d.get("rpe"),
        "feel": d.get("feel"),
        "total_sets": snap.get("totalSets"),
        "completed_sets": snap.get("completedSets"),
        "completion_source": d.get("completionSource"),
        **_execution_fields(d),
        "device_files": _fmt_device_files(d.get("files")),
        "blocks": blocks_out,
    }


async def tp_get_strength_workouts(start_date: str, end_date: str) -> dict[str, Any]:
    """List structured strength (gym) workouts on the athlete's calendar in a date range.

    Strength workouts from TrainingPeaks' strength builder live on a separate API
    from endurance workouts and do NOT appear in `tp_get_workouts`. Use this to
    discover them (and their IDs), then `tp_get_strength_workout` for full detail.

    Args:
        start_date: Range start, YYYY-MM-DD.
        end_date: Range end, YYYY-MM-DD (inclusive).

    Returns:
        Dict with `count`, `date_range`, and `workouts` — each with workout_id,
        date, title, workout_type, planned duration, compliance, set totals,
        execution evidence (start/completed time, executed duration, completed
        TSS, has_file_data, last_updated_at), and an ordered `exercises` preview
        (from the workout's sequence summary).
    """
    start = str(start_date).strip()
    end = str(end_date).strip()
    if not start or not end:
        return _err("VALIDATION_ERROR", "start_date and end_date are required (YYYY-MM-DD).")

    async with TPClient() as client:
        athlete_id, access, err = await _access(client)
        if err:
            return err
        try:
            async with httpx.AsyncClient(timeout=STRENGTH_TIMEOUT) as h:
                r = await h.get(
                    f"{STRENGTH_API_BASE}/rx/activity/v1/workouts/calendar/{athlete_id}/{start}/{end}",
                    headers=_headers(access),
                )
        except httpx.TimeoutException:
            return _err("NETWORK_ERROR", "Strength list timed out.")
        except httpx.RequestError:
            logger.exception("Network error listing strength workouts")
            return _err("NETWORK_ERROR", "A network error occurred.")

        if r.status_code != 200:
            return _map_status(r.status_code, r.text)

        # This endpoint returns a bare JSON array of workout summaries.
        items = r.json()
        if not isinstance(items, list):
            items = items.get("data") or []
        workouts = [
            {
                "workout_id": str(w.get("id")),
                "date": w.get("prescribedDate"),
                "title": w.get("title"),
                "workout_type": w.get("workoutType"),
                "prescribed_duration_min": _min(w.get("prescribedDurationInSeconds")),
                "compliance_state": w.get("complianceState"),
                "compliance_percent": w.get("compliancePercent"),
                "total_sets": w.get("totalSets"),
                "completed_sets": w.get("completedSets"),
                # The list carries no completionSource. hasFileData is its
                # device-recording signal.
                "has_file_data": w.get("hasFileData"),
                "has_prescribed_data": w.get("hasPrescribedData"),
                "rpe": w.get("rpe"),
                "feel": w.get("feel"),
                **_execution_fields(w),
                "exercises": [
                    s.get("title") for s in (w.get("sequenceSummary") or []) if s.get("title")
                ],
            }
            for w in items
        ]
        workouts.sort(key=lambda w: w["date"] or "")
        return {
            "count": len(workouts),
            "date_range": {"start": start, "end": end},
            "workouts": workouts,
        }


async def tp_get_strength_workout(workout_id: str) -> dict[str, Any]:
    """Get a strength workout's full detail: blocks, exercises, sets, weights.

    Args:
        workout_id: The strength workout ID (from tp_get_strength_workouts).

    Returns:
        Dict with the workout metadata (date, title, duration, compliance, RPE,
        feel), execution evidence (completion source, start/completed time,
        completed TSS and its source, attached device files) and `blocks` →
        `exercises` → `sets` with stable ids, each set giving prescribed vs
        executed parameter values (Reps, WeightKg, …) and a completion flag.
    """
    wid = str(workout_id).strip()
    if not wid:
        return _err("VALIDATION_ERROR", "workout_id is required.")
    async with TPClient() as client:
        _, access, err = await _access(client)
        if err:
            return err
        try:
            async with httpx.AsyncClient(timeout=STRENGTH_TIMEOUT) as h:
                r = await h.get(
                    f"{STRENGTH_API_BASE}/rx/activity/v1/workouts/{wid}",
                    headers=_headers(access),
                )
        except httpx.TimeoutException:
            return _err("NETWORK_ERROR", "Strength detail timed out.")
        except httpx.RequestError:
            logger.exception("Network error reading strength workout")
            return _err("NETWORK_ERROR", "A network error occurred.")

        if r.status_code != 200:
            return _map_status(r.status_code, r.text)
        d = r.json().get("data") or {}
        if not d:
            return _err("NOT_FOUND", "Strength workout not found.")
        return _fmt_workout_detail(d)


async def tp_delete_strength_workout(workout_id: str) -> dict[str, Any]:
    """Delete a strength workout.

    Args:
        workout_id: The strength workout ID to delete.

    Returns:
        Dict confirming deletion.
    """
    wid = str(workout_id).strip()
    if not wid:
        return _err("VALIDATION_ERROR", "workout_id is required.")
    async with TPClient() as client:
        _, access, err = await _access(client)
        if err:
            return err
        try:
            async with httpx.AsyncClient(timeout=STRENGTH_TIMEOUT) as h:
                r = await h.delete(
                    f"{STRENGTH_API_BASE}/rx/activity/v1/workouts/{wid}",
                    headers=_headers(access),
                )
        except httpx.TimeoutException:
            return _err("NETWORK_ERROR", "Strength delete timed out.")
        except httpx.RequestError:
            logger.exception("Network error deleting strength workout")
            return _err("NETWORK_ERROR", "A network error occurred.")

        if r.status_code not in (200, 204):
            return _map_status(r.status_code, r.text)
        return {"deleted": True, "workout_id": wid}
