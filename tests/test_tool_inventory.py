"""Inventory checks for the MCP tool catalog, run through a real in-memory client 🧭."""

from typing import Any

import pytest
from fastmcp import Client

from gitlab_mcp.server import mcp

EXISTING_READ_ONLY_TOOLS = {
    "get_current_user",
    "list_merge_requests",
    "list_merge_train",
    "get_merge_request",
    "get_merge_requests",
    "get_merge_request_threads",
    "get_merge_request_diff",
}
EXISTING_WRITE_TOOLS = {
    "reply_to_thread",
    "post_review_notes",
    "land_merge_request",
    "update_merge_request",
}
NEW_READ_ONLY_TOOLS = {"get_work_item", "list_work_item_notes"}
NEW_WRITE_TOOLS = {"edit_work_item_description", "edit_merge_request_description", "create_merge_request"}

EXPECTED_READ_ONLY_TOOLS = EXISTING_READ_ONLY_TOOLS | NEW_READ_ONLY_TOOLS
EXPECTED_WRITE_TOOLS = EXISTING_WRITE_TOOLS | NEW_WRITE_TOOLS
EXPECTED_TOOLS = EXPECTED_READ_ONLY_TOOLS | EXPECTED_WRITE_TOOLS

# Counts are kept as names so a drift shows up as a readable set difference.
EXISTING_TOOL_COUNT = 11
NEW_TOOL_COUNT = 5
TOTAL_TOOL_COUNT = 16


@pytest.fixture
async def tools(gitlab_env) -> dict[str, Any]:
    """Return the registered tools by name, as the MCP client sees them."""
    async with Client(mcp) as client:
        listed = await client.list_tools()
    return {tool.name: tool for tool in listed}


def test_expected_counts_add_up():
    """The inventory counts in this file match the planned tool groups."""
    # Arrange / Act / Assert
    assert len(EXISTING_READ_ONLY_TOOLS | EXISTING_WRITE_TOOLS) == EXISTING_TOOL_COUNT
    assert len(NEW_READ_ONLY_TOOLS | NEW_WRITE_TOOLS) == NEW_TOOL_COUNT
    assert len(EXPECTED_TOOLS) == TOTAL_TOOL_COUNT


async def test_registered_tool_names_match_inventory_exactly(tools):
    """The server exposes exactly the 11 existing tools plus the 5 planned tools."""
    # Act
    names = set(tools)

    # Assert
    assert names == EXPECTED_TOOLS, (
        f"missing: {sorted(EXPECTED_TOOLS - names)}, extra: {sorted(names - EXPECTED_TOOLS)}"
    )
    assert len(tools) == TOTAL_TOOL_COUNT


async def test_post_review_notes_is_registered(tools):
    """The review-note tool stays in the catalog, so older catalogs must be reloaded."""
    # Act / Assert
    assert "post_review_notes" in tools


async def test_server_instructions_describe_the_review_workflow(gitlab_env):
    """The MCP instructions name the review tools and require validation before approval and posting."""
    # Arrange / Act
    async with Client(mcp) as client:
        await client.list_tools()
        instructions = client.instructions

    # Assert
    assert instructions is not None
    for name in ("get_merge_request", "get_merge_request_diff", "post_review_notes", "update_merge_request"):
        assert name in instructions
    assert "post_review_notes(validate_only=true)" in instructions
    assert instructions.index("validate_only=true") < instructions.index("APPROVE")
    assert "explicit APPROVE before posting" in instructions


@pytest.mark.parametrize(
    ("name", "search_terms"),
    [
        ("get_merge_request", ("review",)),
        ("get_merge_request_diff", ("review", "inline comment")),
        ("post_review_notes", ("review", "inline comment")),
        ("update_merge_request", ("review", "reviewer and assignee")),
    ],
)
async def test_review_tools_have_searchable_first_line_descriptions(tools, name, search_terms):
    """Review tool first lines include search terms surfaced by the MCP inventory."""
    # Arrange
    first_line = tools[name].description.splitlines()[0].lower()

    # Act / Assert
    assert all(term in first_line for term in search_terms)
    if name == "update_merge_request":
        assert "glab mr update" in tools[name].description


@pytest.mark.parametrize("name", sorted(EXPECTED_READ_ONLY_TOOLS))
async def test_read_only_tools_are_marked_read_only(tools, name):
    """Read tools advertise readOnlyHint and openWorldHint, since they call GitLab."""
    # Act
    annotations = tools[name].annotations

    # Assert
    assert annotations.read_only_hint is True
    assert annotations.open_world_hint is True


@pytest.mark.parametrize("name", sorted(EXPECTED_WRITE_TOOLS))
async def test_write_tools_are_marked_as_writes(tools, name):
    """Write tools are not read-only, never claim idempotence, and call an external system."""
    # Act
    annotations = tools[name].annotations

    # Assert
    assert annotations.read_only_hint is False
    assert annotations.idempotent_hint is not True
    assert annotations.open_world_hint is True


@pytest.mark.parametrize(
    ("name", "required", "optional"),
    [
        ("get_work_item", {"project", "iid"}, {"scope", "include_description", "description_limit", "cursor"}),
        ("list_work_item_notes", {"project", "iid"}, {"scope", "cursor", "max_body_chars"}),
        (
            "edit_work_item_description",
            {"project", "iid", "old_text", "new_text"},
            {"scope"},
        ),
        ("edit_merge_request_description", {"project", "iid", "old_text", "new_text"}, set()),
        (
            "create_merge_request",
            {"project", "source_branch", "target_branch", "title"},
            {"description", "description_file", "labels", "remove_source_branch", "blocked_by"},
        ),
        (
            "list_merge_requests",
            set(),
            {"created_after", "updated_after", "target_branch", "include_source_branch", "include_labels"},
        ),
    ],
)
async def test_new_and_extended_options_are_in_the_schema(tools, name, required, optional):
    """Each planned option appears in the input schema, and the required fields are enforced."""
    # Arrange
    schema = tools[name].input_schema

    # Act
    properties = set(schema.get("properties", {}))
    required_fields = set(schema.get("required", []))

    # Assert
    assert required <= required_fields
    assert required | optional <= properties
