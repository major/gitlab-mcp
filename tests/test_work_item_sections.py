"""Behavioral tests for focused work-item description reads 🧩."""

import hashlib
import json
from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client
from fastmcp.exceptions import ToolError

from gitlab_mcp.server import mcp

GRAPHQL = "https://gitlab.example.com/api/graphql"
PROJECT = "group/project"
IID = 42
SHARED_CONTENT_LIMIT = 6


def work_item(description: str | None = "A long description") -> dict[str, Any]:
    """Build a minimal GraphQL work item with no widget connections."""
    return {
        "id": "gid://gitlab/WorkItem/123",
        "iid": str(IID),
        "title": "Update platform docs",
        "description": description,
        "state": "OPEN",
        "webUrl": "https://gitlab.example.com/group/project/-/work_items/42",
        "namespace": {"__typename": "Namespace", "fullPath": PROJECT},
        "workItemType": {"name": "Issue"},
        "widgets": [],
    }


def response(description: str | None = "A long description") -> httpx.Response:
    """Wrap a work item in the expected project-scoped GraphQL response."""
    return httpx.Response(
        200,
        json={
            "data": {
                "scopeNamespace": {"__typename": "Project", "fullPath": PROJECT},
                "namespace": {"__typename": "Namespace", "fullPath": PROJECT, "workItem": work_item(description)},
            },
        },
    )


async def call_work_item(**arguments: Any) -> dict[str, Any]:
    """Call the production get_work_item tool through its in-memory MCP client."""
    async with Client(mcp) as client:
        result = await client.call_tool("get_work_item", {"project": PROJECT, "iid": IID, **arguments})
    return result.structured_content


def request_variables(route: respx.Route) -> dict[str, Any]:
    """Return the GraphQL variables from the single detail request."""
    return json.loads(route.calls.last.request.content)["variables"]


@respx.mock
async def test_multiple_sections_keep_requested_order_and_use_one_query(gitlab_env):
    """A list reads later sections in requested order from one full description fetch."""
    # Arrange
    description = "# Overview\nintro\n## WP-8: First\nfirst text\n### Nested\nsubtext\n## Second\nsecond text"
    route = respx.post(GRAPHQL).mock(return_value=response(description))

    # Act
    result = await call_work_item(section=["Second", "WP8"])

    # Assert
    assert route.call_count == 1
    assert result["sections"] == [
        {"heading": "Second", "content": "second text"},
        {"heading": "WP-8: First", "content": "first text\n### Nested\nsubtext"},
    ]
    assert "description" not in result
    assert result["description_hash"] == hashlib.sha256(description.encode()).hexdigest()


@respx.mock
async def test_full_read_keeps_default_description_and_adds_full_hash(gitlab_env):
    """The default read still returns the complete bounded description and its full hash."""
    # Arrange
    description = "# Summary\nfull description"
    route = respx.post(GRAPHQL).mock(return_value=response(description))

    # Act
    result = await call_work_item()

    # Assert
    assert request_variables(route)["includeDescription"] is True
    assert result["description"] == description
    assert result["description_hash"] == hashlib.sha256(description.encode()).hexdigest()
    assert result.get("sections") is None
    assert result.get("headings") is None


@respx.mock
async def test_single_section_uses_description_and_reports_truncation(gitlab_env):
    """A single selected section is returned through the existing description fields."""
    # Arrange
    description = "## First\nignored\n## Target\n123456789\n## Last\nignored too"
    respx.post(GRAPHQL).mock(return_value=response(description))

    # Act
    result = await call_work_item(section="target", description_limit=4)

    # Assert
    assert result["description"] == "1234"
    assert result["description_truncated"] is True
    assert result["description_hash"] == hashlib.sha256(description.encode()).hexdigest()
    assert "sections" not in result


@pytest.mark.parametrize(
    ("description", "include_description", "expected_hash"),
    [
        (None, True, hashlib.sha256(b"").hexdigest()),
        ("private content", False, None),
    ],
)
@respx.mock
async def test_hash_covers_null_description_but_is_omitted_when_skipped(
    gitlab_env, description, include_description, expected_hash
):
    """Fetched null descriptions hash as empty text, while skipped descriptions have no hash."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=response(description))

    # Act
    result = await call_work_item(include_description=include_description)

    # Assert
    assert request_variables(route)["includeDescription"] is include_description
    assert result.get("description_hash") == expected_hash
    if description is None:
        assert "description" not in result
    if not include_description:
        assert "description_hash" not in result


@respx.mock
async def test_headings_only_ignores_fenced_code_headings(gitlab_env):
    """Heading-like lines inside backtick and tilde fences are not returned as headings."""
    # Arrange
    description = "# Real\n```md\n## Fake backtick\n```\n~~~\n## Fake tilde\n~~~\n## Also real\ntext"
    route = respx.post(GRAPHQL).mock(return_value=response(description))

    # Act
    result = await call_work_item(headings_only=True)

    # Assert
    assert route.call_count == 1
    assert request_variables(route)["includeDescription"] is True
    assert result["headings"] == ["Real", "Also real"]
    assert "description" not in result
    assert result["description_hash"] == hashlib.sha256(description.encode()).hexdigest()


@respx.mock
async def test_multi_section_limit_is_shared_and_marks_truncated_content(gitlab_env):
    """The list content limit is shared in selector order and marks the cut section."""
    # Arrange
    description = "## A\n1234\n## B\n5678"
    respx.post(GRAPHQL).mock(return_value=response(description))

    # Act
    result = await call_work_item(section=["A", "B"], description_limit=6)

    # Assert
    assert result["sections"] == [
        {"heading": "A", "content": "1234"},
        {"heading": "B", "content": "56", "truncated": True},
    ]
    assert sum(len(section["content"]) for section in result["sections"]) == SHARED_CONTENT_LIMIT
    assert result["description_hash"] == hashlib.sha256(description.encode()).hexdigest()


@respx.mock
async def test_ambiguous_normalized_ids_report_all_candidates(gitlab_env):
    """IDs that normalize to the same selector fail rather than choosing one."""
    # Arrange
    description = "## WP-8: One\none\n## WP_8: Two\ntwo"
    route = respx.post(GRAPHQL).mock(return_value=response(description))

    # Act / Assert
    with pytest.raises(ToolError, match="ambiguous") as error:
        await call_work_item(section="WP8")
    assert "WP-8: One" in str(error.value)
    assert "WP_8: Two" in str(error.value)
    assert route.call_count == 1


@respx.mock
async def test_distinct_exact_heading_names_with_normalized_spellings_keep_order(gitlab_env):
    """Exact headings that differ by separators remain selectable in request order."""
    # Arrange
    description = "## Pre-work\nhyphen\n## Pre work\nspace\n## Pre_work\nunderscore"
    respx.post(GRAPHQL).mock(return_value=response(description))

    # Act
    result = await call_work_item(section=["Pre_work", "Pre-work", "Pre work"])

    # Assert
    assert result["sections"] == [
        {"heading": "Pre_work", "content": "underscore"},
        {"heading": "Pre-work", "content": "hyphen"},
        {"heading": "Pre work", "content": "space"},
    ]


@respx.mock
async def test_different_aliases_for_same_resolved_heading_are_rejected(gitlab_env):
    """Different selectors cannot return the same resolved heading twice."""
    # Arrange
    description = "## WP-8: First\ncontent"
    route = respx.post(GRAPHQL).mock(return_value=response(description))

    # Act / Assert
    with pytest.raises(ToolError, match="same heading more than once"):
        await call_work_item(section=["WP8", "WP-8"])
    assert route.call_count == 1


@respx.mock
async def test_complete_id_match_does_not_select_a_longer_id(gitlab_env):
    """WP-8 selects its complete ID rather than the longer WP-80 ID."""
    # Arrange
    description = "## WP-80: Other\nother content\n## WP-8: Exact\nexact content"
    respx.post(GRAPHQL).mock(return_value=response(description))

    # Act
    result = await call_work_item(section="WP-8")

    # Assert
    assert result["description"] == "exact content"


@respx.mock
async def test_near_id_does_not_match(gitlab_env):
    """A selector for WP-8 does not fall through to a WP-80 heading."""
    # Arrange
    description = "## WP-80: Other\nother content"
    route = respx.post(GRAPHQL).mock(return_value=response(description))

    # Act / Assert
    with pytest.raises(ToolError, match="was not found") as error:
        await call_work_item(section="WP-8")
    assert "WP-80: Other" in str(error.value)
    assert route.call_count == 1


@pytest.mark.parametrize(
    ("description", "selector", "heading"),
    [
        ("## WP-8: One\ncontent", "WP8 unrelated words", "WP-8: One"),
        ("## Deployment plan\ncontent", "Deployment plan updated", "Deployment plan"),
    ],
)
@respx.mock
async def test_unmatched_full_selector_does_not_use_partial_match(gitlab_env, description, selector, heading):
    """Non-ID selectors must exactly match a heading instead of matching its prefix."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=response(description))

    # Act / Assert
    with pytest.raises(ToolError, match="was not found") as error:
        await call_work_item(section=selector)
    assert heading in str(error.value)
    assert route.call_count == 1


@respx.mock
async def test_missing_list_selector_reports_every_heading_without_partial_result(gitlab_env):
    """A missing selector fails the whole selection and lists more than eight headings."""
    # Arrange
    description = "\n".join(f"## Heading {index}\ncontent" for index in range(10))
    route = respx.post(GRAPHQL).mock(return_value=response(description))

    # Act / Assert
    with pytest.raises(ToolError, match="was not found") as error:
        await call_work_item(section=["Heading 1", "Missing"])
    for index in range(10):
        assert f"Heading {index}" in str(error.value)
    assert route.call_count == 1


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"section": []}, "must not be empty"),
        ({"section": " "}, "non-blank"),
        ({"section": ["A", "a"]}, "duplicates"),
        ({"section": "A", "headings_only": True}, "cannot be used together"),
        ({"section": "A", "include_description": False}, "require include_description"),
        ({"section": "A", "description_limit": 0}, "require description_limit"),
        ({"section": "A", "cursor": "resume"}, "cannot be used with"),
    ],
)
@respx.mock
async def test_invalid_focused_options_fail_before_query(gitlab_env, arguments, message):
    """Invalid focused options are rejected before contacting GitLab."""
    # Arrange
    route = respx.post(GRAPHQL).mock(return_value=response())

    # Act / Assert
    with pytest.raises(ToolError, match=message):
        await call_work_item(**arguments)
    assert route.call_count == 0
