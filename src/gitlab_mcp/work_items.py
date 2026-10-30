"""GraphQL helpers for GitLab work items and their notes 🧩."""

import base64
import binascii
import hashlib
import json
import re
from typing import Any, Literal, NoReturn
from urllib.parse import quote

from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field

from gitlab_mcp.client import GitLabAPIError, GitLabConnectionError, GitLabGraphQLError
from gitlab_mcp.common import (
    HTTP_REQUEST_TIMEOUT,
    HTTP_SERVER_ERROR,
    REVIEW_NOTE_BODY_CAP,
    TRUNCATION_MARKER_ERROR,
    has_truncation_marker,
    page_text,
    read_server_local_utf8_file,
)
from gitlab_mcp.description_sections import DescriptionSection, focused_description, validate_focus_options
lazy from gitlab_mcp.client import GitLabClient

WorkItemScope = Literal["project", "group"]
DetailCursorBinding = tuple[WorkItemScope, str, int, bool, int]
DESCRIPTION_CAP = 4000
NOTE_BODY_CAP = 4000
NOTE_BODY_BUDGET = 30000

_DETAIL_QUERY = """
query(
  $fullPath: ID!, $iid: String!, $includeDescription: Boolean!,
  $includeLabels: Boolean!, $includeAssignees: Boolean!, $includeLinkedItems: Boolean!, $includeChildren: Boolean!,
  $labelsAfter: String, $assigneesAfter: String, $linkedItemsAfter: String, $childrenAfter: String
) {
  __SCOPE_LOOKUP__
  namespace(fullPath: $fullPath) {
    __typename fullPath
    workItem(iid: $iid) {
      id iid title description @include(if: $includeDescription) state webUrl
      namespace { __typename fullPath }
      workItemType { name }
      widgets {
        __typename
        ... on WorkItemWidgetLabels @include(if: $includeLabels) {
          labels(first: 100, after: $labelsAfter) {
            nodes { title }
            pageInfo { hasNextPage endCursor }
          }
        }
        ... on WorkItemWidgetAssignees @include(if: $includeAssignees) {
          assignees(first: 100, after: $assigneesAfter) {
            nodes { username }
            pageInfo { hasNextPage endCursor }
          }
        }
        ... on WorkItemWidgetLinkedItems @include(if: $includeLinkedItems) {
          linkedItems(first: 100, after: $linkedItemsAfter) {
            nodes {
              linkType
              workItem {
                id iid title webUrl
                namespace { __typename fullPath }
                workItemType { name }
              }
            }
            pageInfo { hasNextPage endCursor }
          }
        }
        ... on WorkItemWidgetHierarchy {
          parent {
            id iid title webUrl
            namespace { __typename fullPath }
            workItemType { name }
          }
          children(first: 100, after: $childrenAfter) @include(if: $includeChildren) {
            nodes {
              id iid title webUrl
              namespace { __typename fullPath }
              workItemType { name }
            }
            pageInfo { hasNextPage endCursor }
          }
        }
      }
    }
  }
}
"""

_EDIT_SOURCE_QUERY = """
query($fullPath: ID!, $iid: String!) {
  __SCOPE_LOOKUP__
  namespace(fullPath: $fullPath) {
    __typename fullPath
    workItem(iid: $iid) { id iid description }
  }
}
"""

_NOTE_TARGET_QUERY = """
query($fullPath: ID!, $iid: String!) {
  __SCOPE_LOOKUP__
  namespace(fullPath: $fullPath) {
    __typename fullPath
    workItem(iid: $iid) { id iid }
  }
}
"""

_NOTE_READ_QUERY = """
query($fullPath: ID!, $iid: String!, $noteId: NoteID!) {
  __SCOPE_LOOKUP__
  namespace(fullPath: $fullPath) {
    __typename fullPath
    workItem(iid: $iid) { id iid }
  }
  note(id: $noteId) {
    id system body createdAt author { username }
    discussion { noteable { __typename ... on Issue { id } } }
  }
}
"""

_CREATE_NOTE_MUTATION = """
mutation($noteableId: NoteableID!, $body: String!) {
  createNote(input: {noteableId: $noteableId, body: $body}) {
    note { id discussion { id } }
    errors
  }
}
"""

_NOTES_QUERY = """
query($fullPath: ID!, $iid: String!, $after: String) {
  __SCOPE_LOOKUP__
  namespace(fullPath: $fullPath) {
    __typename fullPath
    workItem(iid: $iid) {
      iid
      widgets {
        __typename
        ... on WorkItemWidgetNotes {
          discussions(first: 20, after: $after) {
            pageInfo { hasNextPage endCursor }
            nodes {
              notes(first: 50) {
                pageInfo { hasNextPage }
                nodes { id system body createdAt author { username } }
              }
            }
          }
        }
      }
    }
  }
}
"""

_SCOPE_LOOKUPS = {
    "project": "scopeNamespace: project(fullPath: $fullPath) { __typename fullPath }",
    "group": "scopeNamespace: group(fullPath: $fullPath) { __typename fullPath }",
}
_DETAIL_CONNECTIONS = {
    "labels": ("WorkItemWidgetLabels", "labels", "includeLabels", "labelsAfter"),
    "assignees": ("WorkItemWidgetAssignees", "assignees", "includeAssignees", "assigneesAfter"),
    "linked_items": ("WorkItemWidgetLinkedItems", "linkedItems", "includeLinkedItems", "linkedItemsAfter"),
    "children": ("WorkItemWidgetHierarchy", "children", "includeChildren", "childrenAfter"),
}


class WorkItemReference(BaseModel):
    """A compact reference to a work item in any namespace."""

    id: str
    iid: int
    title: str
    type: str = Field(default="", exclude_if=lambda value: not value)
    namespace_path: str
    url: str


class LinkedWorkItem(WorkItemReference):
    """A work item and the relationship connecting it to the current item."""

    link_type: str


class WorkItemDetail(BaseModel):
    """One work item's key fields and bounded widget data."""

    id: str
    iid: int
    title: str
    type: str
    state: str
    namespace_path: str
    url: str
    description: str | None = Field(default=None, exclude_if=lambda value: value is None)
    description_truncated: bool = Field(default=False, exclude_if=lambda value: not value)
    description_hash: str | None = Field(default=None, exclude_if=lambda value: value is None)
    next_description_offset: int | None = Field(default=None, exclude_if=lambda value: value is None)
    headings: list[str] | None = Field(default=None, exclude_if=lambda value: value is None)
    sections: list[DescriptionSection] | None = Field(default=None, exclude_if=lambda value: value is None)
    labels: list[str] = Field(default_factory=list, exclude_if=lambda value: not value)
    labels_complete: bool
    assignees: list[str] = Field(default_factory=list, exclude_if=lambda value: not value)
    assignees_complete: bool
    linked_items: list[LinkedWorkItem] = Field(default_factory=list, exclude_if=lambda value: not value)
    linked_items_complete: bool
    parent: WorkItemReference | None = Field(default=None, exclude_if=lambda value: value is None)
    children: list[WorkItemReference] = Field(default_factory=list, exclude_if=lambda value: not value)
    children_complete: bool
    missing_widgets: list[str] = Field(default_factory=list, exclude_if=lambda value: not value)
    next_cursor: str = Field(default="", exclude_if=lambda value: not value)


class WorkItemEditSource(BaseModel):
    """The exact global ID and complete description needed by a future edit helper."""

    id: str
    description: str


class WorkItemNote(BaseModel):
    """One non-system note with a bounded body."""

    note_id: str
    by: str = Field(default="", exclude_if=lambda value: not value)
    body: str
    at: str
    body_truncated: bool = Field(default=False, exclude_if=lambda value: not value)


class WorkItemNotePage(BaseModel):
    """One bounded note body page and the full-body hash."""

    note_id: str
    by: str = Field(default="", exclude_if=lambda value: not value)
    body: str
    at: str
    body_hash: str
    next_body_offset: int | None = Field(default=None, exclude_if=lambda value: value is None)


class WorkItemNotes(BaseModel):
    """One bounded page of human work-item notes."""

    notes: list[WorkItemNote] = Field(default_factory=list, exclude_if=lambda value: not value)
    next_cursor: str = Field(default="", exclude_if=lambda value: not value)
    discussions_complete: bool
    notes_truncated: bool = False


class WorkItemNoteCreateResult(BaseModel):
    """The confirmed, validated, or uncertain outcome of creating one note."""

    status: Literal["validated", "created", "unknown"]
    note_id: str | None = Field(default=None, exclude_if=lambda value: value is None)
    discussion_id: str | None = Field(default=None, exclude_if=lambda value: value is None)
    message: str | None = Field(default=None, exclude_if=lambda value: value is None)


class WorkItemNotFoundError(ToolError):
    """The requested work item or namespace does not exist or is not visible."""

    def __init__(self, scope: WorkItemScope, path: str, iid: int) -> None:
        """Name the namespace, scope, and IID that could not be found."""
        super().__init__(f"Work item #{iid} not found in {scope} {path!r}, or the token cannot see it.")


class WorkItemScopeError(ToolError):
    """The resolved namespace does not match the requested project or group scope."""

    def __init__(self, path: str, expected: WorkItemScope, actual: str) -> None:
        """Explain that scope is explicit and no other namespace type was queried."""
        super().__init__(f"Namespace {path!r} is {actual}, not the requested {expected} scope.")


class WorkItemDataError(ToolError):
    """GitLab returned a work-item response missing fields needed to read it safely."""


class InvalidWorkItemCursorError(ToolError):
    """A notes cursor is malformed or belongs to different call arguments."""

    def __init__(self, tool: str = "read_work_item_notes") -> None:
        """Tell the caller to reuse an unchanged cursor with its original tool."""
        super().__init__(f"Invalid cursor. Pass next_cursor from an earlier {tool} call unchanged.")


def _raise_tool_error(message: str) -> NoReturn:
    """Raise one caller-facing argument validation error."""
    raise ToolError(message)


def _raise_data_error(message: str, *, cause: BaseException | None = None) -> NoReturn:
    """Raise one caller-facing error for an unexpected GraphQL response shape."""
    if cause is None:
        raise WorkItemDataError(message)
    raise WorkItemDataError(message) from cause


def _validate_arguments(project: str, iid: int) -> None:
    """Validate helper arguments before sending any request.

    The tool schema already checks `scope`, limits, and types. These checks cover what the schema allows. 🛡️
    """
    if not project.strip():
        _raise_tool_error("project must not be blank.")
    if type(iid) is not int or iid < 1:
        _raise_tool_error("iid must be a positive integer.")


def _validate_section_options(
    section: str | list[str] | None,
    headings_only: bool,
    include_description: bool,
    description_limit: int,
    cursor: str | None,
) -> None:
    """Validate focused-description options before any GraphQL request."""
    focused = validate_focus_options(section, headings_only, include_description, description_limit)
    if focused and cursor is not None:
        _raise_tool_error("cursor cannot be used with section or headings_only modes.")


def _description_result(  # noqa: PLR0913, PLR0917 - options mirror the public detail reader.
    item: dict[str, Any],
    include_description: bool,
    description_limit: int,
    section: str | list[str] | None,
    headings_only: bool,
    description_offset: int,
) -> tuple[str | None, bool, str | None, list[str] | None, list[DescriptionSection] | None]:
    """Build the bounded description, hash, and optional focused section result."""
    if not include_description:
        return None, False, None, None, None
    raw_description = item.get("description")
    if raw_description is not None and not isinstance(raw_description, str):
        _raise_data_error("GitLab returned an invalid work item description.")
    full_description = raw_description or ""
    description_hash = hashlib.sha256(full_description.encode("utf-8")).hexdigest()
    if section is not None or headings_only:
        description, truncated, headings, sections = focused_description(
            full_description, section, headings_only, description_limit
        )
        return description, truncated, description_hash, headings, sections
    if raw_description is None:
        return None, False, description_hash, None, None
    # The detail reader derives next_description_offset from this page's length, so the helper's offset is unused.
    page, truncated, _ = page_text(raw_description, description_limit, description_offset)
    return page, truncated, description_hash, None, None


def _item_from_data(
    data: dict[str, Any],
    scope: WorkItemScope,
    project: str,
    iid: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve the explicitly scoped namespace and its requested work item."""
    scope_namespace = data.get("scopeNamespace")
    if scope_namespace is None:
        raise WorkItemNotFoundError(scope, project, iid)
    if not isinstance(scope_namespace, dict):
        _raise_data_error("GitLab returned an invalid project or group scope response.")
    expected_type = "Project" if scope == "project" else "Group"
    actual_type = scope_namespace.get("__typename", "")
    if actual_type != expected_type:
        raise WorkItemScopeError(project, scope, str(actual_type or "unknown namespace"))
    if scope_namespace.get("fullPath") != project:
        _raise_data_error("GitLab resolved a different full path than requested.")

    namespace = data.get("namespace")
    if namespace is None:
        raise WorkItemNotFoundError(scope, project, iid)
    if not isinstance(namespace, dict):
        _raise_data_error("GitLab returned an invalid namespace response for the work item.")
    item = namespace.get("workItem")
    if item is None:
        raise WorkItemNotFoundError(scope, project, iid)
    if not isinstance(item, dict):
        _raise_data_error("GitLab returned an invalid work item response.")
    return namespace, item


def _query_for_scope(query: str, scope: WorkItemScope) -> str:
    """Insert the project or group root lookup without falling back between scopes."""
    return query.replace("__SCOPE_LOOKUP__", _SCOPE_LOOKUPS[scope], 1)


def _connection(
    widget: dict[str, Any] | None,
    key: str,
    typename: str,
    *,
    requested: bool = True,
) -> tuple[list[Any], bool, str]:
    """Read one widget connection and return its nodes, completeness, and end cursor."""
    if widget is None:
        return [], False, ""
    if not requested:
        return [], True, ""
    connection = widget.get(key)
    if not isinstance(connection, dict) or not isinstance(connection.get("nodes"), list):
        _raise_data_error(f"GitLab returned an invalid {typename}.{key} connection.")
    page_info = connection.get("pageInfo")
    if not isinstance(page_info, dict) or not isinstance(page_info.get("hasNextPage"), bool):
        _raise_data_error(f"GitLab returned invalid pagination data for {typename}.{key}.")
    has_next = page_info["hasNextPage"]
    end_cursor = page_info.get("endCursor") or ""
    if has_next and not isinstance(end_cursor, str):
        _raise_data_error(f"GitLab returned an invalid cursor for {typename}.{key}.")
    if has_next and not end_cursor:
        _raise_data_error(f"GitLab reported more {typename}.{key} but did not return a cursor.")
    return connection["nodes"], not has_next, end_cursor if has_next else ""


def _detail_connection_pages(
    widget_map: dict[str, dict[str, Any]],
    state: dict[str, dict[str, Any]],
) -> tuple[dict[str, tuple[list[Any], bool, str]], list[str]]:
    """Read active detail connection pages and report unavailable widgets."""
    pages: dict[str, tuple[list[Any], bool, str]] = {}
    missing_widgets: list[str] = []
    for key, (typename, field, _, _) in _DETAIL_CONNECTIONS.items():
        widget = widget_map.get(typename)
        if widget is None:
            missing_widgets.append(typename)
        pages[key] = _connection(widget, field, typename, requested=state[key]["include"])
    return pages, missing_widgets


def _next_detail_state(
    pages: dict[str, tuple[list[Any], bool, str]],
) -> dict[str, dict[str, Any]]:
    """Return the after cursors and active flags for detail connections with more pages."""
    return {key: {"after": after or None, "include": bool(after)} for key, (_, _, after) in pages.items()}


def _detail_cursor(
    state: dict[str, dict[str, Any]],
    binding: DetailCursorBinding,
) -> str:
    """Pack the scoped detail pagination state when any connection has another page."""
    if not any(connection["include"] for connection in state.values()):
        return ""
    scope, project, iid, include_description, description_limit = binding
    return _pack_cursor(
        {
            "version": 1,
            "scope": scope,
            "project": project,
            "iid": iid,
            "include_description": include_description,
            "description_limit": description_limit,
            "connections": state,
        }
    )


def _detail_state() -> dict[str, dict[str, Any]]:
    """Return the initial pagination state for each detail connection."""
    return {key: {"after": None, "include": True} for key in _DETAIL_CONNECTIONS}


def _detail_variables(state: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Map detail pagination state to the query's per-connection arguments."""
    variables: dict[str, Any] = {}
    for connection, (_, _, include_variable, after_variable) in _DETAIL_CONNECTIONS.items():
        variables[include_variable] = state[connection]["include"]
        variables[after_variable] = state[connection]["after"]
    return variables


def _widgets(item: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Index the requested work-item widgets by their GraphQL type name."""
    widgets = item.get("widgets")
    if not isinstance(widgets, list):
        _raise_data_error("GitLab returned a work item without a valid widgets list.")
    indexed: dict[str, dict[str, Any]] = {}
    for widget in widgets:
        if not isinstance(widget, dict) or not isinstance(widget.get("__typename"), str):
            _raise_data_error("GitLab returned a work-item widget without a GraphQL type name.")
        typename = widget["__typename"]
        if typename in indexed:
            _raise_data_error(f"GitLab returned duplicate {typename} widgets.")
        indexed[typename] = widget
    return indexed


def _string_connection_values(nodes: list[Any], key: str, typename: str) -> list[str]:
    """Validate and map the string-valued nodes of a simple widget connection."""
    values: list[str] = []
    for node in nodes:
        if not isinstance(node, dict) or not isinstance(node.get(key), str):
            _raise_data_error(f"GitLab returned an invalid {typename} entry.")
        values.append(node[key])
    return values


def _reference(item: dict[str, Any]) -> WorkItemReference:
    """Map a linked, parent, or child work item to a namespace-qualified reference."""
    namespace = item.get("namespace")
    if not isinstance(namespace, dict):
        _raise_data_error("GitLab returned a work-item reference without a namespace.")
    namespace_path = namespace.get("fullPath")
    url = item.get("webUrl")
    if not isinstance(namespace_path, str) or not namespace_path or not isinstance(url, str) or not url:
        _raise_data_error("GitLab returned a work-item reference without its namespace path or web URL.")
    item_iid = _positive_graphql_iid(item.get("iid"))
    work_item_type = item.get("workItemType")
    return WorkItemReference(
        id=_required_string(item, "id"),
        iid=item_iid,
        title=_required_string(item, "title"),
        type=work_item_type.get("name", "") if isinstance(work_item_type, dict) else "",
        namespace_path=namespace_path,
        url=url,
    )


def _required_string(item: dict[str, Any], key: str) -> str:
    """Return a required string field or raise a clear response-shape error."""
    value = item.get(key)
    if not isinstance(value, str):
        _raise_data_error(f"GitLab returned a work item without a valid {key} field.")
    return value


def _positive_graphql_iid(value: Any, *, context: str | None = None) -> int:
    """Convert GraphQL's string IID to a positive integer."""
    context_suffix = f" ({context})" if context else ""
    try:
        iid = int(value)
    except (TypeError, ValueError) as exc:
        _raise_data_error(f"GitLab returned a work item without a valid IID{context_suffix}.", cause=exc)
    if iid < 1:
        _raise_data_error(f"GitLab returned a work item without a positive IID{context_suffix}.")
    return iid


def _pack_cursor(payload: dict[str, Any]) -> str:
    """Encode a notes resume position as URL-safe JSON."""
    return base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()


def _decode_cursor_payload(cursor: str, tool: str) -> dict[str, Any]:
    """Decode one opaque cursor or report that it is malformed."""
    try:
        raw = base64.b64decode(cursor.encode("ascii"), altchars=b"-_", validate=True)
        payload = json.loads(raw)
    except (UnicodeEncodeError, ValueError, binascii.Error) as exc:
        raise InvalidWorkItemCursorError(tool) from exc
    if not isinstance(payload, dict):
        raise InvalidWorkItemCursorError(tool)
    return payload


def _unpack_cursor(
    cursor: str,
    scope: WorkItemScope,
    project: str,
    iid: int,
    max_body_chars: int,
) -> str:
    """Validate a notes cursor against every argument that affects its result."""
    payload = _decode_cursor_payload(cursor, "read_work_item_notes")
    expected = {
        "version": 1,
        "scope": scope,
        "project": project,
        "iid": iid,
        "max_body_chars": max_body_chars,
    }
    required_keys = {*expected, "after"}
    if (
        set(payload) != required_keys
        or type(payload.get("version")) is not int
        or any(payload.get(key) != value for key, value in expected.items())
    ):
        raise InvalidWorkItemCursorError("read_work_item_notes")
    after = payload.get("after")
    if not isinstance(after, str) or not after:
        raise InvalidWorkItemCursorError("read_work_item_notes")
    return after


def _unpack_detail_cursor(
    cursor: str,
    binding: DetailCursorBinding,
) -> dict[str, dict[str, Any]]:
    """Validate a details cursor against its namespace, item, and description arguments."""
    tool = "read_work_item"
    payload = _decode_cursor_payload(cursor, tool)
    scope, project, iid, include_description, description_limit = binding
    expected = {
        "version": 1,
        "scope": scope,
        "project": project,
        "iid": iid,
        "include_description": include_description,
        "description_limit": description_limit,
    }
    connections = payload.get("connections")
    if (
        set(payload) != {*expected, "connections"}
        or type(payload.get("version")) is not int
        or any(payload.get(key) != value for key, value in expected.items())
        or not isinstance(connections, dict)
        or set(connections) != set(_DETAIL_CONNECTIONS)
    ):
        raise InvalidWorkItemCursorError(tool)
    state: dict[str, dict[str, Any]] = {}
    for key, connection in connections.items():
        if (
            not isinstance(connection, dict)
            or set(connection) != {"after", "include"}
            or type(connection.get("include")) is not bool
            or not (connection.get("after") is None or isinstance(connection.get("after"), str))
            or (connection.get("include") and not connection.get("after"))
        ):
            raise InvalidWorkItemCursorError(tool)
        state[key] = {"after": connection["after"], "include": connection["include"]}
    return state


async def _resolved_item(
    client: GitLabClient,
    lookup: tuple[str, dict[str, Any]],
    scope: WorkItemScope,
    project: str,
    iid: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run a namespace-scoped lookup and validate its result."""
    query, extra_variables = lookup
    data = await client.graphql(
        _query_for_scope(query, scope),
        {"fullPath": project, "iid": str(iid), **extra_variables},
    )
    return _item_from_data(data, scope, project, iid)


async def read_work_item(  # noqa: PLR0913, PLR0917 - signature is part of the parent tool API.
    client: GitLabClient,
    project: str,
    iid: int,
    scope: WorkItemScope = "project",
    include_description: bool = True,
    description_limit: int = DESCRIPTION_CAP,
    cursor: str | None = None,
    section: str | list[str] | None = None,
    headings_only: bool = False,
    description_offset: int = 0,
) -> WorkItemDetail:
    """Read one project's or group's work item and bounded detail widgets.

    Widget connections fetch at most 100 entries each. Continue with `next_cursor`
    to fetch incomplete connections. Merge each list across pages; completed
    connections are omitted from later GraphQL requests. Scope is explicit, with
    no fallback to the other namespace type. Focused section reads share the description
    limit and cannot be combined with a widget cursor. Full-description pages use
    Unicode character offsets and repeat the full-description hash for change detection.
    """
    _validate_arguments(project, iid)
    if type(description_offset) is not int or description_offset < 0:
        _raise_tool_error("description_offset must be a nonnegative integer.")
    if description_offset and (
        section is not None or headings_only or cursor is not None or not include_description or description_limit <= 0
    ):
        _raise_tool_error(
            "Positive description_offset requires a full description and a positive description_limit, "
            "without section, headings_only, or cursor."
        )
    _validate_section_options(section, headings_only, include_description, description_limit, cursor)
    cursor_include_description = include_description
    include_description = include_description and description_limit > 0
    cursor_binding = (scope, project, iid, cursor_include_description, description_limit)
    state = _unpack_detail_cursor(cursor, cursor_binding) if cursor is not None else _detail_state()
    namespace, item = await _resolved_item(
        client,
        (_DETAIL_QUERY, {"includeDescription": include_description, **_detail_variables(state)}),
        scope,
        project,
        iid,
    )
    work_item_iid = _positive_graphql_iid(item.get("iid"))
    if work_item_iid != iid:
        _raise_data_error("GitLab returned a different work item IID than requested.")
    namespace_path = namespace.get("fullPath") or project
    namespace_path = str(namespace_path)
    path = quote(namespace_path, safe="/")
    if scope == "group":
        path = f"groups/{path}"
    url = item.get("webUrl") or f"{client.settings.url}/{path}/-/work_items/{iid}"
    widget_map = _widgets(item)
    pages, missing_widgets = _detail_connection_pages(widget_map, state)
    next_state = _next_detail_state(pages)
    next_cursor = _detail_cursor(next_state, cursor_binding)
    labels, labels_complete, _ = pages["labels"]
    assignees, assignees_complete, _ = pages["assignees"]
    linked_nodes, linked_complete, _ = pages["linked_items"]
    children_nodes, children_complete, _ = pages["children"]
    linked_items: list[LinkedWorkItem] = []
    for linked_node in linked_nodes:
        if not isinstance(linked_node, dict) or not isinstance(linked_node.get("workItem"), dict):
            _raise_data_error("GitLab returned an invalid linked work item.")
        linked_items.append(
            LinkedWorkItem(
                **_reference(linked_node["workItem"]).model_dump(),
                link_type=str(linked_node.get("linkType") or ""),
            )
        )
    hierarchy_widget = widget_map.get(_DETAIL_CONNECTIONS["children"][0])
    parent_node = hierarchy_widget.get("parent") if hierarchy_widget else None
    parent = _reference(parent_node) if isinstance(parent_node, dict) else None
    children = [_reference(child) for child in children_nodes if isinstance(child, dict)]
    if len(children) != len(children_nodes):
        _raise_data_error("GitLab returned an invalid child work item.")
    if parent_node is not None and not isinstance(parent_node, dict):
        _raise_data_error("GitLab returned an invalid parent work item.")

    description, description_truncated, description_hash, headings, selected_sections = _description_result(
        item,
        include_description,
        description_limit,
        section,
        headings_only,
        description_offset,
    )
    work_item_type = item.get("workItemType")
    return WorkItemDetail(
        id=_required_string(item, "id"),
        iid=work_item_iid,
        title=_required_string(item, "title"),
        type=work_item_type.get("name", "") if isinstance(work_item_type, dict) else "",
        state=str(item.get("state") or ""),
        namespace_path=namespace_path,
        url=str(url),
        description=description,
        description_truncated=description_truncated,
        description_hash=description_hash,
        next_description_offset=(
            description_offset + len(description)
            if description_truncated and section is None and not headings_only and description is not None
            else None
        ),
        headings=headings,
        sections=selected_sections,
        labels=_string_connection_values(labels, "title", "label widget"),
        labels_complete=labels_complete,
        assignees=_string_connection_values(assignees, "username", "assignee widget"),
        assignees_complete=assignees_complete,
        linked_items=linked_items,
        linked_items_complete=linked_complete,
        parent=parent,
        children=children,
        children_complete=children_complete,
        missing_widgets=missing_widgets,
        next_cursor=next_cursor,
    )


async def read_work_item_for_edit(
    client: GitLabClient,
    project: str,
    iid: int,
    scope: WorkItemScope = "project",
) -> WorkItemEditSource:
    """Read the exact work-item global ID and full description before an edit.

    Unlike the compact detail reader, this helper never truncates description
    text. It is intended for future edit tools that need a safe read-modify-write
    sequence.
    """
    _validate_arguments(project, iid)
    _, item = await _resolved_item(client, (_EDIT_SOURCE_QUERY, {}), scope, project, iid)
    if _positive_graphql_iid(item.get("iid")) != iid:
        _raise_data_error("GitLab returned a different work item IID than requested.")
    description = item.get("description")
    if description is None:
        description = ""
    elif not isinstance(description, str):
        _raise_data_error("GitLab returned an invalid full work item description.")
    return WorkItemEditSource(id=_required_string(item, "id"), description=description)


def _note_body(body: str | None, body_file: str | None) -> str:
    """Validate and read exactly one note body source before any GitLab request."""
    if (body is None) == (body_file is None):
        _raise_tool_error("Provide exactly one of body or body_file.")
    if body_file is not None:
        try:
            body = read_server_local_utf8_file(
                body_file, path_field="body_file", size_field="body", byte_limit=REVIEW_NOTE_BODY_CAP
            )
        except OSError:
            _raise_tool_error("body_file could not be read as a server-local file.")
        except ValueError as exc:
            _raise_tool_error(str(exc))
    if body is None or not body.strip():
        _raise_tool_error("body must not be blank.")
    try:
        size = len(body.encode("utf-8"))
    except UnicodeEncodeError:
        _raise_tool_error("body must be valid UTF-8.")
    if size > REVIEW_NOTE_BODY_CAP:
        _raise_tool_error(f"body exceeds {REVIEW_NOTE_BODY_CAP} UTF-8 bytes.")
    if has_truncation_marker(body):
        _raise_tool_error(f"{TRUNCATION_MARKER_ERROR} Use body_file for a full server-local file.")
    return body


def _valid_global_id(value: Any, typename: str) -> bool:
    """Check that an ID is a positive numeric GitLab GID of the expected type."""
    return isinstance(value, str) and re.fullmatch(rf"gid://gitlab/{typename}/[1-9][0-9]*", value) is not None


def _verified_note(data: dict[str, Any], note_id: str, item_id: str) -> dict[str, Any]:
    """Check the returned human note and its parent without exposing note content."""
    note = data.get("note")
    if note is None:
        _raise_data_error("Work-item note not found, or the token cannot see it.")
    if not isinstance(note, dict):
        _raise_data_error("GitLab returned an invalid work-item note.")
    returned_id = note.get("id")
    if not _valid_global_id(returned_id, "Note"):
        _raise_data_error("GitLab returned an invalid work-item note global ID.")
    if returned_id != note_id:
        _raise_data_error("GitLab returned a different work-item note than requested.")
    if note.get("system") is True:
        _raise_data_error("GitLab returned a system note, not a human note.")
    if note.get("system") is not False:
        _raise_data_error("GitLab returned an invalid work-item note system flag.")
    discussion = note.get("discussion")
    parent = discussion.get("noteable") if isinstance(discussion, dict) else None
    parent_id = parent.get("id") if isinstance(parent, dict) else None
    if (
        not isinstance(parent, dict)
        or parent.get("__typename") != "Issue"
        or not isinstance(parent_id, str)
        or not (_valid_global_id(parent_id, "Issue") or _valid_global_id(parent_id, "WorkItem"))
        # GitLab WorkItem inherits Issue and uses the same issues table row ID.
        or parent_id.rsplit("/", 1)[-1] != item_id.rsplit("/", 1)[-1]
    ):
        _raise_data_error("GitLab returned an invalid or different work-item note parent.")
    return note


def _note_page(note: dict[str, Any], note_id: str, body_offset: int, body_limit: int) -> WorkItemNotePage:
    """Build a bounded body page and hash the full original UTF-8 body."""
    body = note.get("body")
    if not isinstance(body, str):
        _raise_data_error("GitLab returned an invalid work-item note body.")
    try:
        body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    except UnicodeEncodeError as exc:
        _raise_data_error("GitLab returned an invalid UTF-8 work-item note body.", cause=exc)
    created_at = note.get("createdAt") or ""
    if not isinstance(created_at, str):
        _raise_data_error("GitLab returned an invalid work-item note timestamp.")
    author = note.get("author")
    username = author.get("username", "") if isinstance(author, dict) else ""
    end = body_offset + body_limit
    return WorkItemNotePage(
        note_id=note_id,
        by=username if isinstance(username, str) else "",
        body=body[body_offset:end],
        at=created_at[:10],
        body_hash=body_hash,
        next_body_offset=end if end < len(body) else None,
    )


async def read_work_item_note(  # noqa: PLR0913, PLR0917 - signature mirrors the tool API.
    client: GitLabClient,
    project: str,
    iid: int,
    note_id: str,
    scope: WorkItemScope = "project",
    body_offset: int = 0,
    body_limit: int = NOTE_BODY_CAP,
) -> WorkItemNotePage:
    """Read one verified human note as a bounded body page with a full-body hash."""
    _validate_arguments(project, iid)
    if not _valid_global_id(note_id, "Note"):
        _raise_tool_error("note_id must be a canonical positive GitLab Note global ID.")
    if type(body_offset) is not int or body_offset < 0:
        _raise_tool_error("body_offset must be a nonnegative integer.")
    if type(body_limit) is not int or not 1 <= body_limit <= NOTE_BODY_CAP:
        _raise_tool_error(f"body_limit must be an integer from 1 to {NOTE_BODY_CAP}.")

    data = await client.graphql(
        _query_for_scope(_NOTE_READ_QUERY, scope),
        {"fullPath": project, "iid": str(iid), "noteId": note_id},
    )
    _, item = _item_from_data(data, scope, project, iid)
    resolved_iid = item.get("iid")
    if type(resolved_iid) not in (str, int) or _positive_graphql_iid(resolved_iid) != iid:
        _raise_data_error("GitLab returned a different work item IID than requested.")
    item_id = item.get("id")
    if not isinstance(item_id, str) or not _valid_global_id(item_id, "WorkItem"):
        _raise_data_error("GitLab returned an invalid work item global ID.")
    return _note_page(_verified_note(data, note_id, item_id), note_id, body_offset, body_limit)


def _note_mutation_result(data: Any) -> WorkItemNoteCreateResult:
    """Classify a mutation payload without returning body or server-provided error text."""
    unknown = WorkItemNoteCreateResult(status="unknown", message="Check GitLab before retrying.")
    if not isinstance(data, dict) or not isinstance(data.get("createNote"), dict):
        return unknown
    payload = data["createNote"]
    errors = payload.get("errors")
    note = payload.get("note")
    if "note" not in payload or not isinstance(errors, list) or any(not isinstance(error, str) for error in errors):
        return unknown
    if errors:
        if note is None:
            _raise_tool_error("GitLab rejected the note creation.")
        return unknown
    # A null note can mean a quick action ran without saving a note.
    if not isinstance(note, dict) or not _valid_global_id(note.get("id"), "Note"):
        return unknown
    discussion = note.get("discussion")
    discussion_id = discussion.get("id") if isinstance(discussion, dict) else None
    if not isinstance(discussion_id, str) or not discussion_id.strip():
        discussion_id = None
    return WorkItemNoteCreateResult(status="created", note_id=note["id"], discussion_id=discussion_id)


async def post_work_item_note(  # noqa: PLR0913, PLR0917 - signature mirrors the tool API.
    client: GitLabClient,
    project: str,
    iid: int,
    body: str | None = None,
    body_file: str | None = None,
    scope: WorkItemScope = "project",
    validate_only: bool = False,
) -> WorkItemNoteCreateResult:
    """Validate the input and scoped target, then create one note without retrying."""
    _validate_arguments(project, iid)
    note_body = _note_body(body, body_file)
    _, item = await _resolved_item(client, (_NOTE_TARGET_QUERY, {}), scope, project, iid)
    resolved_iid = item.get("iid")
    if type(resolved_iid) not in (str, int) or _positive_graphql_iid(resolved_iid) != iid:
        _raise_data_error("GitLab returned a different work item IID than requested.")
    item_id = item.get("id")
    if not _valid_global_id(item_id, "WorkItem"):
        _raise_data_error("GitLab returned an invalid work item global ID.")
    if validate_only:
        return WorkItemNoteCreateResult(status="validated", message="Validation does not guarantee write permission.")
    try:
        data = await client.graphql(_CREATE_NOTE_MUTATION, {"noteableId": item_id, "body": note_body})
    except GitLabAPIError as exc:
        if exc.status != HTTP_REQUEST_TIMEOUT and exc.status < HTTP_SERVER_ERROR:
            raise ToolError(f"GitLab rejected the note creation with HTTP {exc.status}.") from None  # noqa: TRY003
    except GitLabConnectionError, GitLabGraphQLError, KeyError, TypeError, AttributeError, ValueError, RecursionError:
        pass
    else:
        return _note_mutation_result(data)
    return WorkItemNoteCreateResult(status="unknown", message="Check GitLab before retrying.")


def _note_widget(item: dict[str, Any]) -> dict[str, Any]:
    """Find the notes widget or report its absence instead of returning a false empty page."""
    widget = _widgets(item).get("WorkItemWidgetNotes")
    if widget is None:
        _raise_data_error("GitLab did not return the work item's notes widget.")
    return widget


def _discussion_page(widget: dict[str, Any]) -> tuple[list[Any], bool, str]:
    """Validate the discussion connection and return its page state."""
    discussions = widget.get("discussions")
    if not isinstance(discussions, dict) or not isinstance(discussions.get("nodes"), list):
        _raise_data_error("GitLab returned an invalid work-item discussions connection.")
    page_info = discussions.get("pageInfo")
    if not isinstance(page_info, dict) or not isinstance(page_info.get("hasNextPage"), bool):
        _raise_data_error("GitLab returned invalid work-item discussion pagination data.")
    has_next = page_info["hasNextPage"]
    end_cursor = page_info.get("endCursor") or ""
    if has_next and (not isinstance(end_cursor, str) or not end_cursor):
        _raise_data_error("GitLab reported more discussions but did not return a cursor.")
    return discussions["nodes"], has_next, end_cursor


def _notes_in_discussion(discussion: Any) -> tuple[list[Any], bool]:
    """Validate one discussion's bounded notes connection."""
    if not isinstance(discussion, dict) or not isinstance(discussion.get("notes"), dict):
        _raise_data_error("GitLab returned an invalid work-item discussion.")
    connection = discussion["notes"]
    nodes = connection.get("nodes")
    page_info = connection.get("pageInfo")
    if not isinstance(nodes, list) or not isinstance(page_info, dict):
        _raise_data_error("GitLab returned an invalid notes connection.")
    has_next = page_info.get("hasNextPage")
    if not isinstance(has_next, bool):
        _raise_data_error("GitLab returned invalid notes pagination data.")
    return nodes, has_next


def _map_human_note(
    note: Any,
    max_body_chars: int,
    remaining_budget: int,
) -> tuple[WorkItemNote | None, int, bool]:
    """Map a non-system note and apply per-body and response-wide character limits."""
    if not isinstance(note, dict):
        _raise_data_error("GitLab returned an invalid work-item note.")
    if note.get("system") is True:
        return None, remaining_budget, False
    if note.get("system") is not False:
        _raise_data_error("GitLab returned an invalid work-item note system flag.")
    note_id = note.get("id")
    if not _valid_global_id(note_id, "Note"):
        _raise_data_error("GitLab returned an invalid work-item note global ID.")
    body = note.get("body") or ""
    if not isinstance(body, str):
        _raise_data_error("GitLab returned an invalid work-item note body.")
    allowed = min(max_body_chars, remaining_budget)
    output_body = body[:allowed]
    body_truncated = len(body) > allowed
    author = note.get("author")
    username = author.get("username", "") if isinstance(author, dict) else ""
    created_at = note.get("createdAt") or ""
    if not isinstance(created_at, str):
        _raise_data_error("GitLab returned an invalid work-item note timestamp.")
    mapped_note = WorkItemNote(
        note_id=note_id,
        by=username if isinstance(username, str) else "",
        body=output_body,
        at=created_at[:10],
        body_truncated=body_truncated,
    )
    return mapped_note, remaining_budget - len(output_body), body_truncated


def _map_human_notes(
    discussions: list[Any],
    max_body_chars: int,
) -> tuple[list[WorkItemNote], bool]:
    """Filter system notes, cap note bodies, and report any omitted content."""
    result: list[WorkItemNote] = []
    remaining_budget = NOTE_BODY_BUDGET
    notes_truncated = False
    for discussion_index, discussion in enumerate(discussions):
        note_nodes, note_page_has_next = _notes_in_discussion(discussion)
        notes_truncated = notes_truncated or note_page_has_next
        for note_index, note in enumerate(note_nodes):
            mapped_note, remaining_budget, body_truncated = _map_human_note(
                note,
                max_body_chars,
                remaining_budget,
            )
            if mapped_note is not None:
                result.append(mapped_note)
            notes_truncated = notes_truncated or body_truncated
            if remaining_budget == 0:
                has_later_content = note_index < len(note_nodes) - 1 or discussion_index < len(discussions) - 1
                notes_truncated = notes_truncated or has_later_content
                return result, notes_truncated
    return result, notes_truncated


async def read_work_item_notes(  # noqa: PLR0913, PLR0917 - helper signature is part of the parent tool API.
    client: GitLabClient,
    project: str,
    iid: int,
    scope: WorkItemScope = "project",
    cursor: str | None = None,
    max_body_chars: int = NOTE_BODY_CAP,
) -> WorkItemNotes:
    """Read one bounded page of human notes, excluding system notes.

    Discussions are paginated with an opaque cursor bound to this namespace,
    IID, scope, and body limit. Each discussion reads at most 50 notes. If one
    discussion has additional notes, or the response body budget is reached,
    `notes_truncated` is true rather than silently hiding that fact.
    """
    _validate_arguments(project, iid)
    after = _unpack_cursor(cursor, scope, project, iid, max_body_chars) if cursor is not None else None
    data = await client.graphql(
        _query_for_scope(_NOTES_QUERY, scope),
        {"fullPath": project, "iid": str(iid), "after": after},
    )
    _, item = _item_from_data(data, scope, project, iid)
    if _positive_graphql_iid(item.get("iid"), context="notes query field") != iid:
        _raise_data_error("GitLab returned a different work item IID than requested.")
    notes_widget = _note_widget(item)
    discussion_nodes, has_next, end_cursor = _discussion_page(notes_widget)
    next_cursor = (
        _pack_cursor(
            {
                "version": 1,
                "scope": scope,
                "project": project,
                "iid": iid,
                "max_body_chars": max_body_chars,
                "after": end_cursor,
            }
        )
        if has_next
        else ""
    )

    result_notes, notes_truncated = _map_human_notes(discussion_nodes, max_body_chars)
    return WorkItemNotes(
        notes=result_notes,
        next_cursor=next_cursor,
        discussions_complete=not has_next,
        notes_truncated=notes_truncated,
    )
