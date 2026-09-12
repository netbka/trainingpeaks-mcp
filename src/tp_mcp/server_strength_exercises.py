"""Runtime registration for the live/custom Strength Builder exercise tools.

The main MCP server is intentionally kept as the stable registry for the large
existing tool surface. This narrow installer adds the three new exercise tools
and updates the existing strength-search description before the CLI starts the
server. The server callbacks read the module-level TOOLS/handler maps at call
time, so extending those maps before ``run_server`` is sufficient and avoids a
large unrelated rewrite of server.py.
"""

from __future__ import annotations

from typing import Any

from mcp.types import Tool, ToolAnnotations

from tp_mcp.tools.strength_exercises import (
    tp_create_custom_exercise,
    tp_get_exercise,
    tp_update_custom_exercise,
)

_INSTALLED = False


def _tool(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str],
) -> Tool:
    tool = Tool(
        name=name,
        description=description,
        input_schema={"type": "object", "properties": properties, "required": required},
    )
    read_only = name.startswith("tp_get_")
    tool.title = name.removeprefix("tp_").replace("_", " ").capitalize()
    tool.annotations = ToolAnnotations(
        read_only_hint=read_only,
        destructive_hint=False,
        idempotent_hint=name != "tp_create_custom_exercise",
        open_world_hint=True,
    )
    return tool


def install_strength_exercise_tools() -> None:
    """Install the new tools into ``tp_mcp.server`` exactly once."""

    global _INSTALLED
    if _INSTALLED:
        return

    from tp_mcp import server as base

    # The live library belongs to the authenticated account/coach, not a
    # targeted athlete, so these tools intentionally do not receive the server's
    # injected ``athlete`` selector.
    tools = [
        _tool(
            "tp_get_exercise",
            (
                "Get one TrainingPeaks Strength Builder exercise in full by numeric id, "
                "including video, instructions, native parameters, muscle groups, "
                "ownership and editability."
            ),
            {
                "exercise_id": {
                    "type": "string",
                    "description": (
                        "Numeric TrainingPeaks strength exercise id from "
                        "tp_search_exercises."
                    ),
                }
            },
            ["exercise_id"],
        ),
        _tool(
            "tp_create_custom_exercise",
            (
                "Create a reusable custom Strength Builder exercise in the "
                "authenticated TrainingPeaks account. This is a real external "
                "mutation. TrainingPeaks assigns the permanent numeric exercise id."
            ),
            {
                "title": {"type": "string", "description": "Exercise title."},
                "parameters": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "TrainingPeaks exercise parameters, e.g. Reps, WeightKg, "
                        "RepsPerSide, Duration, RPE, RIR. Omit to keep the "
                        "scaffold defaults."
                    ),
                },
                "video_url": {
                    "type": "string",
                    "description": "Optional YouTube/Vimeo/demo URL.",
                },
                "instructions": {
                    "type": "string",
                    "description": "Optional technique/instruction text.",
                },
                "primary_muscle_groups": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Primary TrainingPeaks muscle groups, e.g. Quads, Glute, "
                        "Hamstrings."
                    ),
                },
                "secondary_muscle_groups": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Secondary TrainingPeaks muscle groups.",
                },
            },
            ["title"],
        ),
        _tool(
            "tp_update_custom_exercise",
            (
                "Update a caller-owned custom Strength Builder exercise. "
                "Built-in/read-only exercises are rejected. Only supplied fields "
                "change; parameter metadata is validated against TrainingPeaks' "
                "live parameter catalogue."
            ),
            {
                "exercise_id": {
                    "type": "string",
                    "description": "Permanent numeric custom exercise id.",
                },
                "title": {"type": "string"},
                "parameters": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Replacement parameter list. Omit to leave parameters "
                        "unchanged."
                    ),
                },
                "video_url": {"type": "string"},
                "instructions": {"type": "string"},
                "primary_muscle_groups": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "secondary_muscle_groups": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            ["exercise_id"],
        ),
    ]

    async def _h_get(args: dict[str, Any]):
        return await tp_get_exercise(exercise_id=args["exercise_id"])

    async def _h_create(args: dict[str, Any]):
        return await tp_create_custom_exercise(
            title=args["title"],
            parameters=args.get("parameters"),
            video_url=args.get("video_url"),
            instructions=args.get("instructions"),
            primary_muscle_groups=args.get("primary_muscle_groups"),
            secondary_muscle_groups=args.get("secondary_muscle_groups"),
        )

    async def _h_update(args: dict[str, Any]):
        return await tp_update_custom_exercise(
            exercise_id=args["exercise_id"],
            title=args.get("title"),
            parameters=args.get("parameters"),
            video_url=args.get("video_url"),
            instructions=args.get("instructions"),
            primary_muscle_groups=args.get("primary_muscle_groups"),
            secondary_muscle_groups=args.get("secondary_muscle_groups"),
        )

    handlers = {
        "tp_get_exercise": _h_get,
        "tp_create_custom_exercise": _h_create,
        "tp_update_custom_exercise": _h_update,
    }

    for tool in tools:
        if tool.name not in base._TOOLS_BY_NAME:
            base.TOOLS.append(tool)
        base._TOOLS_BY_NAME[tool.name] = tool
        base._TOOL_HANDLERS[tool.name] = handlers[tool.name]

    # Correct the old offline-only public description. The implementation now
    # prefers live libraryContent and reports when it had to fall back.
    search_tool = base._TOOLS_BY_NAME.get("tp_search_exercises")
    if search_tool is not None:
        search_tool.description = (
            "Search the current TrainingPeaks Strength Builder exercise library, "
            "including caller-owned custom exercises. Uses the live combined "
            "library when authenticated and falls back to the baked built-in "
            "snapshot if live discovery is unavailable. Use tp_get_exercise when "
            "full instructions/video/native parameter metadata is needed."
        )

    create_workout_tool = base._TOOLS_BY_NAME.get("tp_create_strength_workout")
    if create_workout_tool is not None:
        create_workout_tool.description = (
            "Create a structured strength/gym workout on the athlete's calendar "
            "using built-in or caller-owned custom exercise ids from "
            "tp_search_exercises. Blocks contain sets and parameters such as Reps, "
            "WeightKg, Duration, RPE or RIR."
        )
        blocks = create_workout_tool.input_schema.get("properties", {}).get("blocks")
        if isinstance(blocks, dict):
            blocks["description"] = (
                "Ordered blocks. Each block: {type: WarmUp|SingleExercise|Superset|"
                "Circuit|CoolDown, title?, notes?, exercises: [{id: '<library id>', "
                "notes?, sets: [{<param>: <value>}, ...]}]}. Parameters include "
                "Reps, RepsPerSide, WeightKg/WeightLb, WeightPerSideKg/WeightPerSideLb, "
                "WeightPercentage, Duration, distance/height variants, RPE, RIR, Watts, "
                "VelocityMetersPerSec and Cals. Superset/Circuit blocks require the "
                "same number of sets for every exercise."
            )

    _INSTALLED = True


def run_server() -> int:
    """Install the extension and delegate to the normal MCP server runner."""

    install_strength_exercise_tools()
    from tp_mcp.server import run_server as base_run_server

    return base_run_server()
