"""Inventory checks for the MCP tool catalog, run through a real in-memory client 🧭."""

import re
from typing import Any

import pytest
from fastmcp import Client

from gitlab_mcp import mcp

PIPELINE_TEST_REPORT_MAX_BUILDS = 20
PIPELINE_TEST_REPORT_DEFAULT_LIMIT = 20
PIPELINE_TEST_REPORT_MAX_LIMIT = 100
REVIEW_NOTE_MAX_NOTES = 20
WORK_ITEM_NOTE_BODY_CAP = 4000

# One name per line, alphabetical, so a new tool is a one-line change.
READ_ONLY_TOOLS = {
    "api_get",
    "get_current_user",
    "get_job",
    "get_job_artifact",
    "get_job_log",
    "get_merge_request",
    "get_merge_request_diff",
    "get_merge_request_threads",
    "get_merge_requests",
    "get_pipeline",
    "get_pipeline_failures",
    "get_pipeline_test_report",
    "get_work_item",
    "get_work_item_note",
    "list_job_artifacts",
    "list_labels",
    "list_merge_requests",
    "list_merge_train",
    "list_pipeline_schedules",
    "list_pipelines",
    "list_work_item_notes",
}
WRITE_TOOLS = {
    "create_issue",
    "create_merge_request",
    "create_pipeline",
    "create_work_item_note",
    "edit_merge_request_description",
    "edit_merge_request_note",
    "edit_work_item_description",
    "land_merge_request",
    "land_merge_requests",
    "post_review_notes",
    "reply_to_thread",
    "reply_to_threads",
    "resolve_open_threads",
    "retry_ci",
    "update_merge_request",
}


@pytest.fixture
async def tools(gitlab_env) -> dict[str, Any]:
    """Return the registered tools by name, as the MCP client sees them."""
    async with Client(mcp) as client:
        listed = await client.list_tools()
    return {tool.name: tool for tool in listed}


async def test_registered_tool_names_match_inventory_exactly(tools):
    """The server exposes exactly the read-only and write tools in this inventory."""
    # Act
    names = set(tools)

    # Assert
    expected = READ_ONLY_TOOLS | WRITE_TOOLS
    assert names == expected, f"missing: {sorted(expected - names)}, extra: {sorted(names - expected)}"


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


async def test_server_instructions_describe_the_ci_triage_workflow(gitlab_env):
    """CI guidance leads from pipeline discovery through job inspection to logs and metadata."""
    # Arrange / Act
    async with Client(mcp) as client:
        await client.list_tools()
        instructions = client.instructions or ""

    # Assert
    pipeline_list_position = instructions.index("list_pipelines to find a pipeline")
    pipeline_detail_position = instructions.index("get_pipeline to inspect its jobs")
    job_log_position = instructions.index("get_job_log to read a job's log")
    assert pipeline_list_position < pipeline_detail_position < job_log_position
    assert "get_job for runner-job metadata" in instructions


async def test_server_instructions_list_real_main_arguments(gitlab_env):
    """Every argument named in the instructions' main-argument summary exists on its tool."""
    # Arrange
    summary_args = {
        "get_merge_request": {
            "project",
            "iid",
            "include_threads",
            "include_description",
            "description_limit",
            "description_offset",
        },
        "get_merge_request_threads": {
            "project",
            "iid",
            "iids",
            "state",
            "view",
            "include_mr_status",
            "count_only",
            "cursor",
        },
        "edit_merge_request_note": {
            "project",
            "iid",
            "thread_id",
            "note_id",
            "expected_hash",
            "body",
            "old_text",
            "new_text",
        },
        "post_review_notes": {
            "project",
            "iid",
            "head_sha",
            "notes",
            "batch_id",
            "add_reviewers",
            "add_assignees",
            "validate_only",
        },
        "update_merge_request": {
            "project",
            "iid",
            "add_reviewers",
            "remove_reviewers",
            "add_assignees",
            "remove_assignees",
            "add_labels",
            "remove_labels",
            "add_blocked_by",
            "remove_blocked_by",
            "approve",
            "sha",
            "rebase",
        },
    }

    # Act
    async with Client(mcp) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
        instructions = client.instructions or ""

    # Assert
    for name, args in summary_args.items():
        assert f"{name}(" in instructions
        assert args <= set(tools[name].input_schema["properties"])


def _schema_property_names(schema: object) -> set[str]:
    """Collect every property name in a JSON schema, including nested item and `$defs` schemas."""
    if isinstance(schema, list):
        return {name for item in schema for name in _schema_property_names(item)}
    if not isinstance(schema, dict):
        return set()
    names = set(schema.get("properties", {}))
    return names | {name for value in schema.values() for name in _schema_property_names(value)}


async def test_server_instruction_tool_references_are_registered(gitlab_env):
    """Every named tool in the server instructions exists in the registered catalog."""
    # Arrange / Act
    async with Client(mcp) as client:
        listed = await client.list_tools()
        instructions = client.instructions

    assert instructions is not None
    registered_names = {tool.name for tool in listed}
    # Argument names such as validate_only, and nested ones such as old_line inside notes, are not tool names.
    argument_names = {name for tool in listed for name in _schema_property_names(tool.input_schema)}
    referenced_names = set(re.findall(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b", instructions)) - argument_names

    # Assert
    assert referenced_names <= registered_names, (
        f"unregistered references: {sorted(referenced_names - registered_names)}"
    )
    assert {
        "create_merge_request",
        "list_labels",
        "get_merge_request_threads",
        "reply_to_thread",
        "edit_merge_request_description",
    } <= referenced_names


async def test_thread_description_recommends_headings_before_full_view(tools):
    """Thread triage guidance starts with headings and uses selected IDs for full notes."""
    # Arrange
    description = tools["get_merge_request_threads"].description

    # Act / Assert
    headings_position = description.index('view="headings"')
    ids_position = description.index("ids=[...]")
    full_position = description.index('view="full"')
    assert headings_position < ids_position < full_position


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


@pytest.mark.parametrize("name", sorted(READ_ONLY_TOOLS))
async def test_read_only_tools_are_marked_read_only(tools, name):
    """Read tools advertise readOnlyHint and openWorldHint, since they call GitLab."""
    # Act
    annotations = tools[name].annotations

    # Assert
    assert annotations.read_only_hint is True
    assert annotations.open_world_hint is True


@pytest.mark.parametrize("name", sorted(WRITE_TOOLS))
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
        (
            "get_merge_request",
            {"project", "iid"},
            {
                "include_threads",
                "include_description",
                "description_limit",
                "description_offset",
                "include_description_hash",
            },
        ),
        ("get_merge_request_threads", {"project"}, {"iid", "iids", "include_mr_status"}),
        (
            "get_work_item",
            {"project", "iid"},
            {"scope", "include_description", "description_limit", "description_offset", "cursor"},
        ),
        ("list_work_item_notes", {"project", "iid"}, {"scope", "cursor", "max_body_chars"}),
        ("get_work_item_note", {"project", "iid", "note_id"}, {"scope", "body_offset", "body_limit"}),
        ("create_work_item_note", {"project", "iid"}, {"body", "body_file", "scope", "validate_only"}),
        (
            "edit_work_item_description",
            {"project", "iid"},
            {
                "old_text",
                "new_text",
                "scope",
                "return_diff",
                "edits",
                "table_cell",
                "expected_hash",
                "diff_limit",
            },
        ),
        ("edit_merge_request_description", {"project", "iid", "old_text", "new_text"}, set()),
        (
            "edit_merge_request_note",
            {"project", "iid", "thread_id", "note_id", "expected_hash"},
            {"body", "old_text", "new_text"},
        ),
        (
            "create_merge_request",
            {"project", "source_branch", "target_branch", "title"},
            {"description", "description_file", "labels", "remove_source_branch", "blocked_by"},
        ),
        ("reply_to_threads", {"project", "replies"}, set()),
        ("resolve_open_threads", {"project", "iid"}, set()),
        ("land_merge_requests", {"project", "items"}, {"mode", "squash", "remove_source_branch"}),
        (
            "list_merge_requests",
            set(),
            {
                "created_after",
                "updated_after",
                "target_branch",
                "include_source_branch",
                "include_labels",
                "include_created_at",
            },
        ),
        (
            "list_pipelines",
            {"project"},
            {"mr_iid", "schedule_id", "ref", "status", "source", "updated_after", "limit", "cursor"},
        ),
        (
            "get_pipeline",
            {"project", "pipeline_id"},
            {"statuses", "include_retried", "include_failure_excerpts", "limit", "cursor"},
        ),
        (
            "get_pipeline_test_report",
            {"project", "pipeline_id"},
            {"build_ids", "limit", "cursor"},
        ),
        (
            "get_pipeline_failures",
            {"project", "pipeline_id"},
            {"limit", "include_logs", "log_chars", "include_retried", "cursor"},
        ),
        ("get_job", {"project", "job_id"}, set()),
        ("list_pipeline_schedules", {"project"}, {"active", "limit", "cursor"}),
        (
            "get_job_log",
            {"project", "job_id"},
            {"view", "max_chars", "pattern", "section", "context", "scan_bytes", "byte_offset"},
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


async def test_create_work_item_note_schema_and_annotations(tools):
    """Comment posting requires its target and advertises optional content and write annotations."""
    # Arrange
    tool = tools["create_work_item_note"]
    schema = tool.input_schema
    properties = schema["properties"]
    annotations = tool.annotations

    # Act / Assert
    assert set(schema["required"]) == {"project", "iid"}
    assert set(properties) == {"project", "iid", "body", "body_file", "scope", "validate_only"}
    assert properties["body"]["default"] is None
    assert properties["body_file"]["default"] is None
    assert properties["scope"]["default"] == "project"
    assert properties["validate_only"]["default"] is False
    assert annotations.read_only_hint is False
    assert annotations.idempotent_hint is False
    assert annotations.open_world_hint is True
    assert annotations.destructive_hint is False


async def test_get_work_item_note_schema_and_annotations(tools):
    """Note reads require a stable ID and advertise bounded, read-only body paging."""
    # Arrange
    tool = tools["get_work_item_note"]
    schema = tool.input_schema
    properties = schema["properties"]

    # Act / Assert
    assert set(schema["required"]) == {"project", "iid", "note_id"}
    assert set(properties) == {"project", "iid", "note_id", "scope", "body_offset", "body_limit"}
    assert properties["scope"]["default"] == "project"
    assert properties["body_offset"]["default"] == 0
    assert properties["body_offset"]["minimum"] == 0
    assert properties["body_limit"]["default"] == WORK_ITEM_NOTE_BODY_CAP
    assert properties["body_limit"]["minimum"] == 1
    assert properties["body_limit"]["maximum"] == WORK_ITEM_NOTE_BODY_CAP
    assert tool.annotations.read_only_hint is True
    assert tool.annotations.open_world_hint is True


async def test_edit_merge_request_note_schema_and_annotations(tools):
    """Guarded edits require the target and hash and advertise destructive non-idempotent writes."""
    # Arrange
    tool = tools["edit_merge_request_note"]
    schema = tool.input_schema

    # Act / Assert
    assert set(schema["required"]) == {"project", "iid", "thread_id", "note_id", "expected_hash"}
    assert set(schema["properties"]) == {
        "project",
        "iid",
        "thread_id",
        "note_id",
        "expected_hash",
        "body",
        "old_text",
        "new_text",
    }
    assert schema["properties"]["note_id"]["exclusiveMinimum"] == 0
    assert schema["properties"]["expected_hash"]["pattern"] == r"^[0-9a-f]{64}$"
    assert tool.annotations.read_only_hint is False
    assert tool.annotations.destructive_hint is True
    assert tool.annotations.idempotent_hint is False
    assert tool.annotations.open_world_hint is True
    assert "glab api --method PUT" in tool.description
    assert "APPROVE" in tool.description


async def test_get_merge_request_has_unwrapped_object_output_schema(tools):
    """The two detail shapes share an object output schema without a result wrapper."""
    # Arrange
    schema = tools["get_merge_request"].output_schema

    # Act
    branches = {branch["title"]: branch for branch in schema["anyOf"]}
    detail = branches["MergeRequestDetail"]
    page = branches["MergeRequestDescriptionPage"]

    # Assert
    assert schema["type"] == "object"
    assert set(branches) == {"MergeRequestDetail", "MergeRequestDescriptionPage"}
    assert {"iid", "title", "state", "source_branch", "target_branch", "approved"} <= set(detail["required"])
    assert "description_hash" in detail["properties"]
    assert "description_hash" not in detail["required"]
    assert page["additionalProperties"] is False
    assert set(page["required"]) == {"iid", "description_hash"}
    assert set(page["properties"]) == {
        "iid",
        "description",
        "description_truncated",
        "next_description_offset",
        "description_hash",
    }
    assert tools["get_merge_request"].input_schema["properties"]["include_description_hash"]["default"] is False


@pytest.mark.parametrize(
    ("name", "expected_description"),
    [
        ("get_work_item", "Work item (issue) number."),
        ("get_work_item_note", "Work item (issue) number."),
        ("list_work_item_notes", "Work item (issue) number."),
        ("edit_work_item_description", "Work item (issue) number."),
        ("edit_merge_request_description", "MR number."),
    ],
)
async def test_work_item_iid_descriptions_are_scoped_to_work_item_tools(tools, name, expected_description):
    """Work-item schemas describe an issue number without changing MR schema wording."""
    # Arrange
    iid_schema = tools[name].input_schema["properties"]["iid"]

    # Act / Assert
    assert iid_schema["description"] == expected_description


def integer_schema(prop: dict[str, Any]) -> dict[str, Any]:
    """Return the integer branch of a property schema. Optional fields wrap it in anyOf with null."""
    branches = prop.get("anyOf", [prop])
    return next(branch for branch in branches if branch.get("type") == "integer")


def enum_values(prop: dict[str, Any]) -> list[str]:
    """Return the enum values of a Literal property. Optional fields wrap it in anyOf with null."""
    branches = prop.get("anyOf", [prop])
    return next(branch["enum"] for branch in branches if "enum" in branch)


@pytest.mark.parametrize(
    ("name", "field", "minimum", "maximum"),
    [
        ("list_pipelines", "mr_iid", 1, None),
        ("list_pipelines", "schedule_id", 1, None),
        ("list_pipelines", "limit", 1, 100),
        ("get_pipeline", "pipeline_id", 1, None),
        ("get_pipeline", "limit", 1, 100),
        ("get_pipeline_test_report", "pipeline_id", 1, None),
        ("get_pipeline_test_report", "limit", 1, 100),
        ("get_pipeline_failures", "pipeline_id", 1, None),
        ("get_pipeline_failures", "limit", 1, 10),
        ("get_pipeline_failures", "log_chars", 1, 1000),
        ("get_job", "job_id", 1, None),
        ("list_pipeline_schedules", "limit", 1, 100),
        ("get_job_log", "job_id", 1, None),
        ("get_job_log", "max_chars", 1, 16000),
        ("get_job_log", "scan_bytes", 1, 1048576),
        ("get_job_log", "context", 0, 5),
    ],
)
async def test_ci_ids_are_positive_and_paging_or_output_sizes_are_bounded(tools, name, field, minimum, maximum):
    """CI IDs must be positive, and limits or character counts have an upper bound in the schema."""
    # Arrange
    prop = tools[name].input_schema["properties"][field]

    # Act
    bounds = integer_schema(prop)

    # Assert
    assert bounds["minimum"] == minimum
    assert bounds.get("maximum") == maximum


async def test_get_pipeline_failures_schema_defaults_and_required_fields(tools):
    """Pipeline failure diagnosis requires its target and keeps log output bounded by default."""
    # Arrange
    schema = tools["get_pipeline_failures"].input_schema
    properties = schema["properties"]
    expected_defaults = {"limit": 5, "include_logs": True, "log_chars": 1000, "include_retried": False}

    # Act / Assert
    assert set(schema["required"]) == {"project", "pipeline_id"}
    assert {name: properties[name]["default"] for name in expected_defaults} == expected_defaults


async def test_get_pipeline_failure_excerpts_default_to_disabled(tools):
    """Pipeline detail keeps failure excerpts disabled by default."""
    # Arrange
    property_schema = tools["get_pipeline"].input_schema["properties"]["include_failure_excerpts"]

    # Act / Assert
    assert property_schema["default"] is False


async def test_status_and_source_are_separate_enums(tools):
    """`scheduled` is a pipeline state and `schedule` is a pipeline source, so each enum keeps only its own value."""
    # Arrange
    properties = tools["list_pipelines"].input_schema["properties"]

    # Act
    statuses = enum_values(properties["status"])
    sources = enum_values(properties["source"])

    # Assert
    assert "scheduled" in statuses
    assert "schedule" not in statuses
    assert "schedule" in sources
    assert "scheduled" not in sources


async def test_get_job_log_views_default_to_tail(tools):
    """The log tool offers five bounded views, and tail remains the default."""
    # Arrange
    view = tools["get_job_log"].input_schema["properties"]["view"]

    # Act / Assert
    assert enum_values(view) == ["tail", "summary", "head", "search", "section", "failures"]
    assert view["default"] == "tail"


async def test_get_job_log_pattern_schema_accepts_string_list_or_null(tools):
    """The search pattern schema advertises both input forms and documents list bounds."""
    # Arrange
    pattern = tools["get_job_log"].input_schema["properties"]["pattern"]

    # Act
    branches = pattern["anyOf"]
    accepted_types = {branch.get("type") for branch in branches}
    list_schema = next(branch for branch in branches if branch.get("type") == "array")
    list_description = pattern["description"]

    # Assert
    assert {"string", "array", "null"} <= accepted_types
    assert list_schema["items"]["type"] == "string"
    assert "1 to 10" in list_description
    assert "200" in list_description


async def test_pipeline_test_report_schema_bounds_build_ids_and_defaults_limit(tools):
    """The test report schema uses strict positive build IDs, bounded arrays, and standard paging inputs."""
    # Arrange
    properties = tools["get_pipeline_test_report"].input_schema["properties"]
    required = set(tools["get_pipeline_test_report"].input_schema.get("required", []))

    # Act
    build_ids = next(branch for branch in properties["build_ids"]["anyOf"] if branch.get("type") == "array")
    limit = integer_schema(properties["limit"])

    # Assert
    assert "build_ids" not in required
    assert "cursor" not in required
    assert build_ids["maxItems"] == PIPELINE_TEST_REPORT_MAX_BUILDS
    assert build_ids["items"]["type"] == "integer"
    assert build_ids["items"]["minimum"] == 1
    assert limit["maximum"] == PIPELINE_TEST_REPORT_MAX_LIMIT
    assert properties["limit"]["default"] == PIPELINE_TEST_REPORT_DEFAULT_LIMIT
    assert properties["cursor"]["anyOf"]


async def test_review_note_batch_schema_keeps_notes_optional_and_bounded(tools):
    """Review posting accepts either notes or a token while preserving the note-array bounds."""
    # Arrange
    schema = tools["post_review_notes"].input_schema
    properties = schema["properties"]

    # Act
    notes = next(branch for branch in properties["notes"]["anyOf"] if branch.get("type") == "array")
    required = set(schema.get("required", []))

    # Assert
    assert "notes" not in required
    assert "batch_id" not in required
    assert notes["minItems"] == 1
    assert notes["maxItems"] == REVIEW_NOTE_MAX_NOTES
    assert "batch_id" in properties


async def test_review_note_compact_schema_defaults_to_false(tools):
    """Review note results keep their existing full form unless compact output is requested."""
    # Arrange
    compact = tools["post_review_notes"].input_schema["properties"]["compact"]

    # Act / Assert
    assert compact["default"] is False


async def test_review_note_result_schema_and_recovery_guidance(tools):
    """Review output exposes bounded lifetime and unavailable-batch recovery guidance."""
    # Arrange
    tool = tools["post_review_notes"]
    properties = tool.output_schema["properties"]

    # Act / Assert
    assert integer_schema(properties["ttl_seconds"])["type"] == "integer"
    assert {branch.get("const") for branch in properties["batch_error"]["anyOf"]} == {"unavailable", None}
    assert "expires_at" not in properties
    assert "ttl_seconds" in tool.description
    assert "prior" in tool.description.lower()
    assert "APPROVE" in tool.description


CI_TOOLS = {
    "list_pipelines",
    "get_pipeline",
    "get_pipeline_failures",
    "get_pipeline_test_report",
    "get_job",
    "get_job_artifact",
    "list_job_artifacts",
    "list_pipeline_schedules",
    "get_job_log",
}


@pytest.mark.parametrize("name", sorted(CI_TOOLS))
async def test_ci_tool_descriptions_name_the_glab_equivalent(tools, name):
    """Each CI tool names the glab command or glab api call it mirrors, so agents can map it to the CLI."""
    # Act / Assert
    assert "glab" in tools[name].description
