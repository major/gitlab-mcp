"""Behavioral tests for safe Markdown table-cell edits in work-item descriptions 🧾."""

import hashlib
import json
from typing import Any

import httpx
import pytest
import respx
from fastmcp.exceptions import ToolError

from mcp_helpers import call_tool as call_mcp_tool

GRAPHQL = "https://gitlab.example.com/api/graphql"
PROJECT = "group/project"
IID = 7
WORK_ITEM_ID = "gid://gitlab/WorkItem/123"


@pytest.fixture
def router(gitlab_env):
    """Provide a fresh mocked GitLab endpoint for every behavior test."""
    with respx.mock(assert_all_called=False) as mock_router:
        yield mock_router


async def call_tool(**arguments: Any) -> dict[str, Any]:
    """Call the work-item edit tool with keyword arguments and return its structured content."""
    return await call_mcp_tool("edit_work_item_description", arguments)


def graphql_operation(request: httpx.Request) -> str:
    """Return the GraphQL operation text from one captured request."""
    return json.loads(request.content)["query"]


def work_item_response(description: str) -> dict[str, Any]:
    """Build the project-scoped work-item response used by the edit tool."""
    item = {"id": WORK_ITEM_ID, "iid": str(IID), "description": description}
    return {
        "data": {
            "scopeNamespace": {"__typename": "Project", "fullPath": PROJECT},
            "namespace": {"__typename": "Namespace", "fullPath": PROJECT, "workItem": item},
        }
    }


def serve_work_item(router: respx.MockRouter, description: str) -> respx.Route:
    """Serve a description read and capture any work-item update mutation."""

    def handler(request: httpx.Request) -> httpx.Response:
        """Return the work item for reads and an accepted payload for updates."""
        if "workItemUpdate" in graphql_operation(request):
            return httpx.Response(
                200,
                json={"data": {"workItemUpdate": {"errors": [], "workItem": {"id": WORK_ITEM_ID}}}},
            )
        return httpx.Response(200, json=work_item_response(description))

    return router.post(GRAPHQL).mock(side_effect=handler)


def mutations(route: respx.Route) -> list[dict[str, Any]]:
    """Return captured variables for every work-item update request."""
    return [
        json.loads(call.request.content)["variables"]
        for call in route.calls
        if "workItemUpdate" in graphql_operation(call.request)
    ]


async def assert_table_edit_rejected_without_write(
    router: respx.MockRouter, description: str, table_cell: dict[str, Any], message: str | None = None
) -> None:
    """Assert a table selector fails safely before the work-item mutation."""
    route = serve_work_item(router, description)
    try:
        with pytest.raises(ToolError, match=message):
            await call_tool(project=PROJECT, iid=IID, table_cell=table_cell)
    finally:
        assert mutations(route) == []


async def test_cell_edit_preserves_pipe_padding_and_other_cells(router):
    """A unique keyed cell changes while pipes, spacing, and neighboring cells remain exact."""
    # Arrange
    before = "| Key | Status | Note |\n| --- | :---: | ---: |\n|  A  |  todo  | keep  |\n"
    route = serve_work_item(router, before)

    # Act
    result = await call_tool(
        project=PROJECT,
        iid=IID,
        table_cell={"row_key": "A", "column": "Status", "new_text": "done"},
    )

    # Assert
    after = "| Key | Status | Note |\n| --- | :---: | ---: |\n|  A  |  done  | keep  |\n"
    assert result["status"] == "applied"
    assert result["description_hash"] == hashlib.sha256(after.encode()).hexdigest()
    assert mutations(route) == [{"input": {"id": WORK_ITEM_ID, "descriptionWidget": {"description": after}}}]


async def test_selectors_are_trimmed_and_empty_cell_values_are_allowed(router):
    """Trimmed row and column selectors match source text, and an empty value clears a cell."""
    # Arrange
    route = serve_work_item(router, "| Key | State |\n| --- | --- |\n| A | old |")

    # Act
    result = await call_tool(
        project=PROJECT,
        iid=IID,
        table_cell={"row_key": "  A  ", "column": " State ", "new_text": ""},
    )

    # Assert
    assert result["status"] == "applied"
    assert mutations(route)[0]["input"]["descriptionWidget"]["description"] == (
        "| Key | State |\n| --- | --- |\n| A |  |"
    )


async def test_same_row_key_can_be_selected_in_a_named_section(router):
    """A section narrows a repeated first-column key to its own heading range."""
    # Arrange
    before = (
        "# Plan\n| Key | State |\n| --- | --- |\n| A | old |\n\n# Release\n| Key | State |\n| --- | --- |\n| A | old |"
    )
    route = serve_work_item(router, before)

    # Act
    await call_tool(
        project=PROJECT,
        iid=IID,
        table_cell={"row_key": "A", "column": "State", "new_text": "shipped", "section": "Release"},
    )

    # Assert
    expected = (
        "# Plan\n| Key | State |\n| --- | --- |\n| A | old |\n\n"
        "# Release\n| Key | State |\n| --- | --- |\n| A | shipped |"
    )
    assert mutations(route)[0]["input"]["descriptionWidget"]["description"] == expected


async def test_section_includes_deeper_headings_until_same_or_lower_heading(router):
    """A section includes nested headings and stops before the next same-level heading."""
    # Arrange
    before = (
        "## Plan\n### Details\n| Key | State |\n| --- | --- |\n| A | old |\n"
        "# Next\n| Key | State |\n| --- | --- |\n| A | old |"
    )
    route = serve_work_item(router, before)

    # Act
    await call_tool(
        project=PROJECT,
        iid=IID,
        table_cell={"row_key": "A", "column": "State", "new_text": "done", "section": "Plan"},
    )

    # Assert
    expected = before.replace("| A | old |", "| A | done |", 1)
    assert mutations(route)[0]["input"]["descriptionWidget"]["description"] == expected


@pytest.mark.parametrize(
    ("description", "table_cell", "message"),
    [
        pytest.param(
            "| Key | State |\n| --- | --- |\n| A | old |\n| A | old |",
            {"row_key": "A", "column": "State", "new_text": "done"},
            "more than once|multiple|ambiguous",
            id="duplicate-row-key",
        ),
        pytest.param(
            "| Key | State |\n| --- | --- |\n| A | old |",
            {"row_key": "missing", "column": "State", "new_text": "done"},
            "(?i)not found|no matching|missing",
            id="missing-row-key",
        ),
        pytest.param(
            "| Key | State |\n| --- | --- |\n| A | old |",
            {"row_key": "A", "column": "Missing", "new_text": "done"},
            "(?i)column|not found|missing",
            id="missing-column",
        ),
        pytest.param(
            "# Plan\n| Key | State |\n| --- | --- |\n| A | old |",
            {"row_key": "A", "column": "State", "new_text": "done", "section": "plan"},
            "(?i)section|not found|missing",
            id="section-case-sensitive",
        ),
        pytest.param(
            "# Plan\n| Key | State |\n| --- | --- |\n| A | old |",
            {"row_key": "A", "column": "State", "new_text": "done", "section": "# Plan"},
            "(?i)section|not found|missing",
            id="section-omits-heading-marker",
        ),
        pytest.param(
            "# Plan\n| Key | State |\n| --- | --- |\n| A | old |\n# Plan\n| Key | State |\n| --- | --- |\n| A | old |",
            {"row_key": "A", "column": "State", "new_text": "done", "section": "Plan"},
            "section|more than once|multiple|ambiguous",
            id="duplicate-section-heading",
        ),
    ],
)
async def test_missing_or_ambiguous_selector_fails_without_write(router, description, table_cell, message):
    """Missing and non-unique row, column, or section selectors never choose a first match."""
    # Arrange / Act / Assert
    await assert_table_edit_rejected_without_write(router, description, table_cell, message)


@pytest.mark.parametrize(
    "description",
    [
        pytest.param(
            "| Key | State |\n| --- | --- |\n| A | old |\n\n| Key | State |\n| --- | --- |\n| A | old |",
            id="duplicate-table-cell-match",
        ),
        pytest.param(
            "| Key | State |\n| --- | --- |\n| A | old |\n| A | old |",
            id="duplicate-row-in-one-table",
        ),
    ],
)
async def test_multiple_cell_intersections_fail_without_write(router, description):
    """Multiple row-and-column intersections across the scope are rejected."""
    await assert_table_edit_rejected_without_write(
        router,
        description,
        {"row_key": "A", "column": "State", "new_text": "done"},
        "more than once|multiple|ambiguous",
    )


@pytest.mark.parametrize(
    "description",
    [
        pytest.param(
            "| Key | State | State |\n| --- | --- | --- |\n| A | old | old |",
            id="duplicate-header",
        ),
        pytest.param(
            "| Key |  |\n| --- | --- |\n| A | old |",
            id="empty-header",
        ),
        pytest.param(
            "| Key | State |\n| --- | --- |\n| A | old | extra |",
            id="malformed-selected-row-width",
        ),
        pytest.param(
            "| Key | State |\n| -- | -- |\n| A | old |",
            id="malformed-separator-width",
        ),
    ],
)
async def test_malformed_selected_table_fails_without_write(router, description):
    """Duplicate or empty headers, invalid separators, and mismatched row widths are not edited."""
    await assert_table_edit_rejected_without_write(
        router,
        description,
        {"row_key": "A", "column": "State", "new_text": "done"},
        "(?i)table|header|separator|malformed|column|not found",
    )


@pytest.mark.parametrize(
    "description",
    [
        pytest.param(
            "```markdown\n| Key | State |\n| --- | --- |\n| A | fenced |\n```\n"
            "| Key | State |\n| --- | --- |\n| A | live |",
            id="fenced-table-ignored",
        ),
        pytest.param(
            "~~~\n| Key | State |\n| --- | --- |\n| A | fenced |\n~~~\n| Key | State |\n| --- | --- |\n| A | live |",
            id="tilde-fenced-table-ignored",
        ),
    ],
)
async def test_tables_in_fenced_code_are_ignored(router, description):
    """Tables inside fenced code do not count as matches or get modified."""
    # Arrange
    route = serve_work_item(router, description)

    # Act
    await call_tool(
        project=PROJECT,
        iid=IID,
        table_cell={"row_key": "A", "column": "State", "new_text": "done"},
    )

    # Assert
    assert mutations(route)[0]["input"]["descriptionWidget"]["description"] == description.replace("live", "done")


async def test_unclosed_fence_protects_the_rest_of_the_description(router):
    """An unclosed code fence protects all later table-looking text from edits."""
    # Arrange / Act / Assert
    await assert_table_edit_rejected_without_write(
        router,
        "Intro\n```\n| Key | State |\n| --- | --- |\n| A | fenced |",
        {"row_key": "A", "column": "State", "new_text": "done"},
        "(?i)not found|no matching|missing",
    )


async def test_heading_inside_fenced_code_does_not_duplicate_a_section(router):
    """An ATX-looking heading inside a fence does not affect a real section's range."""
    # Arrange
    before = (
        "```markdown\n# Release\n| Key | State |\n| --- | --- |\n| A | code |\n```\n"
        "# Release\n| Key | State |\n| --- | --- |\n| A | live |"
    )
    route = serve_work_item(router, before)

    # Act
    await call_tool(
        project=PROJECT,
        iid=IID,
        table_cell={"row_key": "A", "column": "State", "new_text": "done", "section": "Release"},
    )

    # Assert
    expected = before.replace("| A | live |", "| A | done |")
    assert mutations(route)[0]["input"]["descriptionWidget"]["description"] == expected


@pytest.mark.parametrize(
    ("description", "table_cell"),
    [
        pytest.param(
            "<!--\n| Key | State |\n| --- | --- |\n| A | old |\n-->",
            {"row_key": "A", "column": "State", "new_text": "done"},
            id="html-comment-table",
        ),
        pytest.param(
            "<pre>\n| Key | State |\n| --- | --- |\n| A | old |\n</pre>",
            {"row_key": "A", "column": "State", "new_text": "done"},
            id="pre-container-table",
        ),
        pytest.param(
            "<script>\n| Key | State |\n| --- | --- |\n| A | old |\n</script>",
            {"row_key": "A", "column": "State", "new_text": "done"},
            id="script-container-table",
        ),
        pytest.param(
            "<!--\n# Release\n| Key | State |\n| --- | --- |\n| A | old |\n-->",
            {"row_key": "A", "column": "State", "new_text": "done", "section": "Release"},
            id="section-heading-inside-comment",
        ),
        pytest.param(
            "<pre>\n# Release\n| Key | State |\n| --- | --- |\n| A | old |\n</pre>",
            {"row_key": "A", "column": "State", "new_text": "done", "section": "Release"},
            id="section-heading-inside-pre-container",
        ),
        pytest.param(
            "<script>\n# Release\n| Key | State |\n| --- | --- |\n| A | old |\n</script>",
            {"row_key": "A", "column": "State", "new_text": "done", "section": "Release"},
            id="section-heading-inside-script-container",
        ),
    ],
)
async def test_html_markup_outside_fences_is_rejected_before_section_selection(router, description, table_cell):
    """Table mode refuses HTML-like content before selecting a section or table."""
    await assert_table_edit_rejected_without_write(router, description, table_cell)


async def test_html_markup_inside_fenced_code_is_ignored(router):
    """HTML-like text inside a fenced code block does not block an outside table edit."""
    # Arrange
    before = (
        "```html\n<!--\n# Release\n<pre>table text</pre>\n<script>script text</script>\n-->\n```\n\n"
        "| Key | State |\n| --- | --- |\n| A | live |"
    )
    route = serve_work_item(router, before)

    # Act
    await call_tool(
        project=PROJECT,
        iid=IID,
        table_cell={"row_key": "A", "column": "State", "new_text": "done"},
    )

    # Assert
    expected = before.replace("| A | live |", "| A | done |")
    assert mutations(route)[0]["input"]["descriptionWidget"]["description"] == expected


@pytest.mark.parametrize(
    "description",
    [
        pytest.param("| Key | State |\n| --- | --- |\n| A | old\\x |", id="backslash-row"),
        pytest.param("| Key | State |\n| --- | --- |\n| A | `old` |", id="backtick-row"),
    ],
)
async def test_unsupported_escape_characters_in_candidate_rows_fail_safely(router, description):
    """Backslashes and backticks in candidate table rows are rejected instead of parsed ambiguously."""
    await assert_table_edit_rejected_without_write(
        router,
        description,
        {"row_key": "A", "column": "State", "new_text": "done"},
        "backslash|backtick|unsupported|table|malformed",
    )


@pytest.mark.parametrize(
    ("description", "table_cell"),
    [
        pytest.param(
            "| Key | State |\n| --- | --- |\n| **A** | old |",
            {"row_key": "A", "column": "State", "new_text": "done"},
            id="row-key-is-source-text",
        ),
        pytest.param(
            "| Key | **State** |\n| --- | --- |\n| A | old |",
            {"row_key": "A", "column": "State", "new_text": "done"},
            id="header-is-source-text",
        ),
    ],
)
async def test_selectors_match_source_markdown_not_rendered_text(router, description, table_cell):
    """Markdown formatting in headers and first-column keys is not stripped or rendered for matching."""
    await assert_table_edit_rejected_without_write(router, description, table_cell, "(?i)not found|missing|column")


@pytest.mark.parametrize(
    "description",
    [
        pytest.param("Key | State\n--- | ---\nA | old", id="no-leading-pipe"),
        pytest.param("| Key | State\n| --- | ---\n| A | old", id="no-trailing-pipe"),
    ],
)
async def test_non_standalone_pipe_rows_are_not_selected(router, description):
    """Only rows with standalone leading and trailing pipes are part of supported tables."""
    await assert_table_edit_rejected_without_write(
        router,
        description,
        {"row_key": "A", "column": "State", "new_text": "done"},
        "(?i)not found|missing|table",
    )


async def test_nonpipe_continuation_after_table_rejects_candidate_without_write(router):
    """A nonblank continuation after a table prevents partial edits of its pipe block."""
    await assert_table_edit_rejected_without_write(
        router,
        "| Key | State |\n| --- | --- |\n| A | old |\nA",
        {"row_key": "A", "column": "State", "new_text": "done"},
    )


async def test_prose_without_blank_boundary_before_table_rejects_candidate(router):
    """Prose directly before a pipe block makes it unsupported rather than a free-standing table."""
    await assert_table_edit_rejected_without_write(
        router,
        "Intro\n| Key | State |\n| --- | --- |\n| A | old |",
        {"row_key": "A", "column": "State", "new_text": "done"},
    )


async def test_blank_separated_prose_around_table_is_supported(router):
    """Blank lines separate prose from a table so only the selected table cell changes."""
    # Arrange
    before = "Intro prose\n\n| Key | State |\n| --- | --- |\n| A | old |\n\nTail prose"
    route = serve_work_item(router, before)

    # Act
    await call_tool(
        project=PROJECT,
        iid=IID,
        table_cell={"row_key": "A", "column": "State", "new_text": "done"},
    )

    # Assert
    expected = before.replace("| A | old |", "| A | done |")
    assert mutations(route)[0]["input"]["descriptionWidget"]["description"] == expected


@pytest.mark.parametrize(
    "new_text",
    [
        pytest.param("a|b", id="pipe"),
        pytest.param("first\nsecond", id="line-feed"),
        pytest.param("first\rsecond", id="carriage-return"),
        pytest.param("a\\b", id="backslash"),
        pytest.param("`code`", id="backtick"),
    ],
)
async def test_unsafe_new_cell_text_is_rejected_before_fetch(router, new_text):
    """Replacement text cannot inject another cell, line, or unsupported table syntax."""
    # Arrange
    serve_work_item(router, "| Key | State |\n| --- | --- |\n| A | old |")

    # Act / Assert
    with pytest.raises(ToolError):
        await call_tool(
            project=PROJECT,
            iid=IID,
            table_cell={"row_key": "A", "column": "State", "new_text": new_text},
        )
    assert len(router.calls) == 0


@pytest.mark.parametrize(
    ("row_key", "column"),
    [
        pytest.param("  ", "State", id="blank-row-key"),
        pytest.param("A", " \t ", id="blank-column"),
    ],
)
async def test_blank_selectors_are_rejected_before_fetch(router, row_key, column):
    """A blank row key or column is invalid and does not trigger a GitLab request."""
    # Arrange
    serve_work_item(router, "| Key | State |\n| --- | --- |\n| A | old |")

    # Act / Assert
    with pytest.raises(ToolError):
        await call_tool(
            project=PROJECT,
            iid=IID,
            table_cell={"row_key": row_key, "column": column, "new_text": "done"},
        )
    assert len(router.calls) == 0


async def test_table_edit_preserves_crlf_final_newline_and_uses_hash_and_diff(router):
    """A table edit keeps untouched CRLF bytes and final newline, and reports normal edit metadata."""
    # Arrange
    before = "Header\r\n\r\n| Key | State |\r\n| --- | --- |\r\n| A | old |\r\n\r\nTail\r\n"
    after = "Header\r\n\r\n| Key | State |\r\n| --- | --- |\r\n| A | done |\r\n\r\nTail\r\n"
    route = serve_work_item(router, before)

    # Act
    result = await call_tool(
        project=PROJECT,
        iid=IID,
        table_cell={"row_key": "A", "column": "State", "new_text": "done"},
        expected_hash=hashlib.sha256(before.encode()).hexdigest(),
    )

    # Assert
    assert result["status"] == "applied"
    assert result["description_hash"] == hashlib.sha256(after.encode()).hexdigest()
    assert result["changed_lines"] == "@@ -5 +5 @@\n-| A | old |\r\n+| A | done |\r"
    assert result["truncated"] is False
    assert mutations(route)[0]["input"]["descriptionWidget"]["description"] == after


async def test_table_diff_limit_zero_skips_diff_but_writes_final_text(router):
    """The ordinary diff limit applies to a table cell write, including a zero-character limit."""
    # Arrange
    before = "| Key | State |\n| --- | --- |\n| A | old |"
    route = serve_work_item(router, before)

    # Act
    result = await call_tool(
        project=PROJECT,
        iid=IID,
        table_cell={"row_key": "A", "column": "State", "new_text": "done"},
        diff_limit=0,
    )

    # Assert
    assert result["status"] == "applied"
    assert "changed_lines" not in result
    assert result["truncated"] is False
    assert mutations(route)[0]["input"]["descriptionWidget"]["description"] == before.replace("old", "done")


async def test_table_cell_mode_is_exclusive_with_legacy_and_batch_modes(router):
    """A table-cell edit cannot be combined with either legacy replacement or batch edits."""
    # Arrange
    serve_work_item(router, "| Key | State |\n| --- | --- |\n| A | old |")
    cell = {"row_key": "A", "column": "State", "new_text": "done"}

    # Act / Assert
    for arguments in (
        {"old_text": "old", "new_text": "done", "table_cell": cell},
        {"edits": [{"old_text": "old", "new_text": "done"}], "table_cell": cell},
    ):
        with pytest.raises(ToolError):
            await call_tool(project=PROJECT, iid=IID, **arguments)
    assert len(router.calls) == 0


async def test_table_cell_model_forbids_extra_fields_before_fetch(router):
    """The table-cell input model rejects unknown fields before any request is sent."""
    # Arrange
    serve_work_item(router, "| Key | State |\n| --- | --- |\n| A | old |")

    # Act / Assert
    with pytest.raises(ToolError):
        await call_tool(
            project=PROJECT,
            iid=IID,
            table_cell={"row_key": "A", "column": "State", "new_text": "done", "ignored": True},
        )
    assert len(router.calls) == 0


async def test_expected_hash_mismatch_rejects_table_edit_without_write(router):
    """The shared expected-hash guard is checked before editing a selected cell."""
    # Arrange
    route = serve_work_item(router, "| Key | State |\n| --- | --- |\n| A | old |")

    # Act / Assert
    with pytest.raises(ToolError, match="does not match the current description"):
        await call_tool(
            project=PROJECT,
            iid=IID,
            table_cell={"row_key": "A", "column": "State", "new_text": "done"},
            expected_hash=hashlib.sha256(b"stale").hexdigest(),
        )
    assert mutations(route) == []
