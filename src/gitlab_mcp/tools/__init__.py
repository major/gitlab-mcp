"""GitLab tool modules. Importing this package registers every tool it contains 🧩."""

from gitlab_mcp.tools import (
    api,
    ci,
    ci_write,
    issues,
    labels,
    mr_create,
    mr_diff,
    mr_land,
    mr_notes,
    mr_read,
    mr_review,
    mr_threads,
    mr_update,
    snippets,
    users,
    work_items,
)

__all__ = [
    "api",
    "ci",
    "ci_write",
    "issues",
    "labels",
    "mr_create",
    "mr_diff",
    "mr_land",
    "mr_notes",
    "mr_read",
    "mr_review",
    "mr_threads",
    "mr_update",
    "snippets",
    "users",
    "work_items",
]
