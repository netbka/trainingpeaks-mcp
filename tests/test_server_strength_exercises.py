"""Server-registry coverage for custom strength exercise tools."""

import json
from unittest.mock import AsyncMock, patch

import pytest

from tp_mcp import server
import tp_mcp.server_strength_exercises as extension


@pytest.fixture(autouse=True)
def restore_server_registry():
    tools = list(server.TOOLS)
    handlers = dict(server._TOOL_HANDLERS)
    by_name = dict(server._TOOLS_BY_NAME)
    installed = extension._INSTALLED
    extension._INSTALLED = False
    try:
        yield
    finally:
        server.TOOLS[:] = tools
        server._TOOL_HANDLERS.clear()
        server._TOOL_HANDLERS.update(handlers)
        server._TOOLS_BY_NAME.clear()
        server._TOOLS_BY_NAME.update(by_name)
        extension._INSTALLED = installed


def test_installer_registers_tools_and_updates_search_description():
    extension.install_strength_exercise_tools()
    names = {tool.name for tool in server.TOOLS}
    assert "tp_get_exercise" in names
    assert "tp_create_custom_exercise" in names
    assert "tp_update_custom_exercise" in names

    get_tool = server._TOOLS_BY_NAME["tp_get_exercise"]
    create_tool = server._TOOLS_BY_NAME["tp_create_custom_exercise"]
    update_tool = server._TOOLS_BY_NAME["tp_update_custom_exercise"]
    assert get_tool.annotations.read_only_hint is True
    assert create_tool.annotations.read_only_hint is False
    assert create_tool.annotations.idempotent_hint is False
    assert update_tool.annotations.idempotent_hint is True
    assert "live" in server._TOOLS_BY_NAME["tp_search_exercises"].description.lower()


@pytest.mark.asyncio
async def test_registered_create_tool_dispatches_without_athlete_parameter():
    extension.install_strength_exercise_tools()
    tool = server._TOOLS_BY_NAME["tp_create_custom_exercise"]
    assert "athlete" not in tool.input_schema["properties"]
    assert tool.input_schema["required"] == ["title"]

    mocked = AsyncMock(return_value={"exercise_id": "628875", "title": "жим платформы"})
    with patch("tp_mcp.server_strength_exercises.tp_create_custom_exercise", new=mocked):
        result = await server.call_tool("tp_create_custom_exercise", {"title": "жим платформы"})

    payload = json.loads(result[0].text)
    assert payload["exercise_id"] == "628875"
    mocked.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_required_arg_uses_normal_server_error_envelope():
    extension.install_strength_exercise_tools()
    result = await server.call_tool("tp_create_custom_exercise", {})
    payload = json.loads(result[0].text)
    assert payload["error_code"] == "INVALID_ARGS"
    assert "title" in payload["message"]
