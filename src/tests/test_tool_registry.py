# pyright: reportMissingParameterType=false

from team_harness.tools.registry import ToolRegistry


def _schema(name: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": name,
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    }


async def _noop(**kwargs: object) -> str:
    return "ok"


def test_get_all_schemas_preserves_registration_order():
    registry = ToolRegistry()
    names = ["spawn_agent", "agent_status", "read_agent_output", "list_agents"]
    for name in names:
        registry.register(schema=_schema(name), fn=_noop)

    schemas = registry.get_all_schemas()
    assert [schema["function"]["name"] for schema in schemas] == names


def test_get_all_schemas_is_stable_across_turns():
    registry = ToolRegistry()
    for name in ["a", "b", "c", "d"]:
        registry.register(schema=_schema(name), fn=_noop)

    first = registry.get_all_schemas()
    second = registry.get_all_schemas()

    # Deterministic ordering keeps the request prefix (tool schemas) byte-stable
    # across turns, which is what OpenAI-family server-side caching relies on.
    assert first == second
    assert [s["function"]["name"] for s in first] == ["a", "b", "c", "d"]
